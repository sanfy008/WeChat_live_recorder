"""NAS 无头服务端任务调度器（流录制 + 流式分段 ASR + 可选 LLM 后处理）。

设计目标：
1. 与 HTTP 框架（FastAPI）解耦，便于单元测试和本地/Docker 复用。
2. 零 Windows 依赖：仅使用 ffmpeg（流拉取/转码）+ SenseVoice/ONNX + soundfile + postprocess。
3. 崩溃安全与内存克制：
   - 任务状态持久化到 `<data_dir>/jobs.json`，重启后自动恢复中断任务为 `interrupted`。
   - 音频按 300s 块流式读盘转写，不在内存中一次性加载整场录音。
   - 单 ASR Worker 串行消费转写队列，防止并发推理把 7.6GB NAS 内存撑爆。
"""

from __future__ import annotations

import json
import os
import queue
import re
import shutil
import threading
import time
import uuid
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import IO, Any, Callable

import numpy as np
import soundfile as sf

from . import artifacts, lexicon, postprocess, retention
from .config import (
    DEFAULT_REASONING_EFFORT,
    DEFAULT_TEXT_MODEL,
    OUTPUT_DIR,
    validate_reasoning_effort,
)
from .livesilence import LiveSilenceMonitor, read_since, read_tail_window
from .resample import StreamResampler
from .session import Session
from .stream_capture import AUDIO_EXTS, StreamRecorder, extract_url, find_ffmpeg, is_url
from . import winproc

SUPPORTED_MEDIA_EXTS = set(AUDIO_EXTS) | {".mp4", ".flv", ".mkv", ".webm", ".ts"}
ACTIVE_STATUSES = {"recording", "queued", "converting", "transcribing", "postprocessing"}
CHUNK_SECONDS = 300


def _mask_secret(secret: str) -> str:
    s = (secret or "").strip()
    if not s:
        return ""
    if len(s) <= 8:
        return "*" * len(s)
    return f"{s[:4]}****{s[-4:]}"


def _safe_stem(name: str, fallback: str = "live") -> str:
    cleaned = "".join(c for c in (name or "").strip() if c.isalnum() or c in "-_.")
    return cleaned.strip("._-") or fallback


_PLACEHOLDER_TITLE_RE = re.compile(
    r"^(?:直播_\d{8}_\d{6}|音频_\d{8}_\d{6}|orig_\d+(?:_orig)?|stream|upload|live|audio)$",
    re.IGNORECASE,
)


def is_placeholder_title(title: str) -> bool:
    """判断任务标题是否只是时间占位名或抓流原始数字 ID（适合 AI 自动重命名）。"""
    t = (title or "").strip()
    return (not t) or bool(_PLACEHOLDER_TITLE_RE.match(t))


def _atomic_write_json(path: Path, data: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    tmp.replace(path)


@dataclass
class NasSettings:
    """NAS 运行时可持久化配置。"""

    local_threads: int = 4
    use_lexicon: bool = True
    enable_llm: bool = False
    llm_endpoint: str = ""
    llm_api_key: str = ""
    llm_model: str = DEFAULT_TEXT_MODEL
    llm_reasoning_effort: str = DEFAULT_REASONING_EFFORT
    auto_transcribe: bool = True
    keep_audio: bool = True
    # ---- 直播静音自动停止（2026-11-23 新增）----
    # 直播结束后流不断（CDN 还挂着），不判停就会一直录到地址过期。
    # 40 秒是本地 GUI 实测值：2026-11-17 那场末尾正好 40.0 秒静音。
    auto_stop_on_silence: bool = True
    silence_stop_seconds: int = 40
    # 阈值沿用本地实测：16k mono 归一化 float 的 RMS < 0.002 算静音
    silence_rms_threshold: float = 0.002
    # ---- 磁盘保留策略（2026-11-23 新增）----
    # **默认 0 = 不自动删**（对立性审查 C2 修订）。
    #
    # 原设计默认 30 天，理由是"一场 2 小时直播音频约 220MB，每天一场 6.6GB/月"。
    # 但"新装即生效、第 31 天自动删、不可撤销、无回收站"是不可接受的默认值：
    # 删除是破坏性的，必须由人显式开启并知道后果。
    # 需要自动清理时在网页「⚙ 设置」里填天数即可，或设环境变量 WXLIVE_RETENTION_DAYS。
    retention_days: int = 0
    # 是否连逐字稿一起清。默认 False：文字体积小、价值高，只清音频就够了。
    cleanup_delete_transcripts: bool = False
    # 自动清理的检查间隔（小时）。默认 24 = 每天一次。
    cleanup_interval_hours: int = 24
    # ---- 录制上限（对立性审查 H2）----
    # 永不结束且有声音的流会无限增长，磁盘写满后转写/日志/jobs.json 全挂。
    # 0 = 不限制。默认 8 小时：比常见直播长得多，但能兜住异常。
    max_record_hours: int = 8
    # 单个任务音频上限（GB）。0 = 不限制。8h@16k mono ≈ 0.88GB，留足余量。
    max_record_gb: int = 2
    # ---- 最短录制保护（2026-11-23 新增）----
    # 开播前 N 秒内即使出现连续静音（如主播调试设备、短暂离开）也不自动停录。
    # 默认 300 秒（5 分钟）；0 = 不保护。
    min_record_seconds: int = 300
    # ---- AI 自动识别直播名称 ----
    # 当任务名称未填（或仅为 orig_xxx_orig / 直播_时间戳 等占位符）且启用了 LLM 时，
    # 转写整理完成后自动用 AI 从内容/要点中提炼一场好记的标题。
    auto_title: bool = True

    @classmethod
    def from_env(cls) -> NasSettings:
        endpoint = os.environ.get("WXLIVE_LLM_ENDPOINT", "").strip()
        api_key = os.environ.get("WXLIVE_LLM_API_KEY", "").strip()
        model = os.environ.get("WXLIVE_LLM_MODEL", "").strip() or DEFAULT_TEXT_MODEL
        effort_raw = os.environ.get("WXLIVE_LLM_EFFORT", "").strip() or DEFAULT_REASONING_EFFORT
        try:
            effort = validate_reasoning_effort(effort_raw)
        except ValueError:
            effort = DEFAULT_REASONING_EFFORT
        threads = max(1, min(16, int(os.environ.get("WXLIVE_LOCAL_THREADS", "4") or 4)))
        enable_env = os.environ.get("WXLIVE_ENABLE_LLM", "").strip().lower()
        if enable_env in ("1", "true", "yes", "on"):
            enable_llm = True
        elif enable_env in ("0", "false", "no", "off"):
            enable_llm = False
        else:
            enable_llm = bool(endpoint and api_key)
        # 环境变量未设置时保持 0（不自动删）——见 NasSettings.retention_days 注释
        try:
            retention = int(os.environ.get("WXLIVE_RETENTION_DAYS", "0") or 0)
        except ValueError:
            retention = 0
        try:
            cleanup_hours = int(os.environ.get("WXLIVE_CLEANUP_INTERVAL_HOURS", "24") or 24)
        except ValueError:
            cleanup_hours = 24
        try:
            silence_secs = int(os.environ.get("WXLIVE_SILENCE_STOP_SECONDS", "40") or 40)
        except ValueError:
            silence_secs = 40
        silence_env = os.environ.get("WXLIVE_AUTO_STOP_ON_SILENCE", "").strip().lower()
        if silence_env in ("0", "false", "no", "off"):
            auto_stop_silence = False
        elif silence_env in ("1", "true", "yes", "on"):
            auto_stop_silence = True
        else:
            auto_stop_silence = True
        try:
            max_hours = int(os.environ.get("WXLIVE_MAX_RECORD_HOURS", "8") or 8)
        except ValueError:
            max_hours = 8
        try:
            max_gb = int(os.environ.get("WXLIVE_MAX_RECORD_GB", "2") or 2)
        except ValueError:
            max_gb = 2
        try:
            min_secs = int(os.environ.get("WXLIVE_MIN_RECORD_SECONDS", "300") or 300)
        except ValueError:
            min_secs = 300
        auto_title_env = os.environ.get("WXLIVE_AUTO_TITLE", "").strip().lower()
        auto_title = auto_title_env not in ("0", "false", "no", "off")
        return cls(
            local_threads=threads,
            use_lexicon=True,
            enable_llm=enable_llm,
            llm_endpoint=endpoint,
            llm_api_key=api_key,
            llm_model=model,
            llm_reasoning_effort=effort,
            auto_transcribe=True,
            keep_audio=True,
            auto_stop_on_silence=auto_stop_silence,
            silence_stop_seconds=max(10, silence_secs),
            retention_days=max(0, retention),
            cleanup_interval_hours=max(1, cleanup_hours),
            max_record_hours=max(0, max_hours),
            max_record_gb=max(0, max_gb),
            min_record_seconds=max(0, min_secs),
            auto_title=auto_title,
        )

    def to_public_dict(self) -> dict[str, Any]:
        return {
            "local_threads": self.local_threads,
            "use_lexicon": self.use_lexicon,
            "enable_llm": self.enable_llm,
            "llm_endpoint": self.llm_endpoint,
            "llm_api_key_masked": _mask_secret(self.llm_api_key),
            "has_api_key": bool(self.llm_api_key),
            "llm_model": self.llm_model,
            "llm_reasoning_effort": self.llm_reasoning_effort,
            "auto_transcribe": self.auto_transcribe,
            "keep_audio": self.keep_audio,
            "auto_stop_on_silence": self.auto_stop_on_silence,
            "silence_stop_seconds": self.silence_stop_seconds,
            "silence_rms_threshold": self.silence_rms_threshold,
            "retention_days": self.retention_days,
            "cleanup_delete_transcripts": self.cleanup_delete_transcripts,
            "cleanup_interval_hours": self.cleanup_interval_hours,
            "max_record_hours": self.max_record_hours,
            "max_record_gb": self.max_record_gb,
            "min_record_seconds": self.min_record_seconds,
            "auto_title": self.auto_title,
        }


class NasJobRunner:
    """管理拉流录制、媒体上传、SenseVoice 转写队列与可选 LLM 后处理。"""

    def __init__(
        self,
        data_dir: Path | str | None = None,
        engine_factory: Callable[[int, bool], Any] | None = None,
    ) -> None:
        self.data_dir = Path(data_dir) if data_dir else Path(OUTPUT_DIR)
        self.recordings_dir = self.data_dir / "recordings"
        self.transcripts_dir = self.data_dir / "transcripts"
        self.jobs_path = self.data_dir / "jobs.json"
        self.settings_path = self.data_dir / "settings.json"

        self.recordings_dir.mkdir(parents=True, exist_ok=True)
        self.transcripts_dir.mkdir(parents=True, exist_ok=True)

        self._lock = threading.RLock()
        self._engine_lock = threading.Lock()
        self._engine: Any = None
        self._engine_key: tuple[int, bool] | None = None
        self._engine_factory = engine_factory

        self._settings = self._load_settings()
        # 这两个字段由 _load_jobs 写入，必须在调用前先定义
        self._jobs_load_error: str = ""
        self._jobs_load_skipped: int = 0
        self._jobs: dict[str, dict[str, Any]] = self._load_jobs()
        self._recorders: dict[str, StreamRecorder] = {}
        self._stop_overrides: dict[str, bool] = {}

        self._asr_queue: queue.Queue[str | None] = queue.Queue()
        self._shutdown_event = threading.Event()
        self._worker = threading.Thread(
            target=self._worker_loop, daemon=True, name="nas-asr-worker"
        )
        self._worker.start()

        # 保留策略状态（供 UI 展示"上次清理时间/释放了多少"）
        self._last_cleanup: dict[str, Any] = {}
        self._cleanup_thread: threading.Thread | None = None
        self._start_cleanup_loop()

    # ---------------- 配置持久化 ----------------

    def _load_settings(self) -> NasSettings:
        cfg = NasSettings.from_env()
        if not self.settings_path.exists():
            return cfg
        try:
            raw = json.loads(self.settings_path.read_text(encoding="utf-8"))
            if isinstance(raw, dict):
                for key in (
                    "local_threads", "use_lexicon", "enable_llm",
                    "llm_endpoint", "llm_api_key", "llm_model",
                    "llm_reasoning_effort", "auto_transcribe", "keep_audio",
                    "retention_days", "cleanup_delete_transcripts",
                    "cleanup_interval_hours",
                    "auto_stop_on_silence", "silence_stop_seconds",
                    "silence_rms_threshold",
                    "max_record_hours", "max_record_gb", "min_record_seconds",
                ):
                    if key in raw and raw[key] is not None:
                        setattr(cfg, key, raw[key])
                cfg.local_threads = max(1, min(16, int(cfg.local_threads)))
                cfg.llm_reasoning_effort = validate_reasoning_effort(cfg.llm_reasoning_effort)
                cfg.retention_days = max(0, int(cfg.retention_days))
                cfg.cleanup_interval_hours = max(1, int(cfg.cleanup_interval_hours))
                cfg.silence_stop_seconds = max(10, int(cfg.silence_stop_seconds))
                cfg.silence_rms_threshold = max(0.0, float(cfg.silence_rms_threshold))
                cfg.min_record_seconds = max(0, int(cfg.min_record_seconds))
        except Exception:
            pass
        return cfg

    def get_settings(self) -> dict[str, Any]:
        with self._lock:
            return self._settings.to_public_dict()

    def update_settings(self, patch: dict[str, Any]) -> dict[str, Any]:
        with self._lock:
            s = self._settings
            if "local_threads" in patch and patch["local_threads"] is not None:
                s.local_threads = max(1, min(16, int(patch["local_threads"])))
            for bool_key in ("use_lexicon", "enable_llm", "auto_transcribe",
                             "keep_audio", "cleanup_delete_transcripts",
                             "auto_stop_on_silence", "auto_title"):
                if bool_key in patch and patch[bool_key] is not None:
                    setattr(s, bool_key, bool(patch[bool_key]))
            if "retention_days" in patch and patch["retention_days"] is not None:
                s.retention_days = max(0, int(patch["retention_days"]))
            if "cleanup_interval_hours" in patch and patch["cleanup_interval_hours"] is not None:
                s.cleanup_interval_hours = max(1, int(patch["cleanup_interval_hours"]))
            if "silence_stop_seconds" in patch and patch["silence_stop_seconds"] is not None:
                # 下限 10 秒：比说话的自然停顿还短的话会频繁误判
                s.silence_stop_seconds = max(10, int(patch["silence_stop_seconds"]))
            if "silence_rms_threshold" in patch and patch["silence_rms_threshold"] is not None:
                # 下限钳到 1e-6：0 会让 rms>=0 恒真，静音永不累计，
                # 自动停止静默失效（对立性审查 M9）
                v = float(patch["silence_rms_threshold"])
                s.silence_rms_threshold = min(0.05, max(1e-6, v))
            if "max_record_hours" in patch and patch["max_record_hours"] is not None:
                s.max_record_hours = max(0, int(patch["max_record_hours"]))
            if "max_record_gb" in patch and patch["max_record_gb"] is not None:
                s.max_record_gb = max(0, int(patch["max_record_gb"]))
            if "min_record_seconds" in patch and patch["min_record_seconds"] is not None:
                s.min_record_seconds = max(0, int(patch["min_record_seconds"]))
            if "llm_endpoint" in patch and patch["llm_endpoint"] is not None:
                s.llm_endpoint = str(patch["llm_endpoint"]).strip()
            if "llm_model" in patch and patch["llm_model"] is not None:
                val = str(patch["llm_model"]).strip()
                if val:
                    s.llm_model = val
            if "llm_reasoning_effort" in patch and patch["llm_reasoning_effort"] is not None:
                s.llm_reasoning_effort = validate_reasoning_effort(str(patch["llm_reasoning_effort"]))
            if patch.get("clear_api_key"):
                s.llm_api_key = ""
            elif "llm_api_key" in patch and patch["llm_api_key"] is not None:
                new_key = str(patch["llm_api_key"]).strip()
                if new_key and "****" not in new_key:
                    s.llm_api_key = new_key
            _atomic_write_json(self.settings_path, asdict(s))
            started_cleanup = s.retention_days > 0
            needs_start = started_cleanup and (
                self._cleanup_thread is None or not self._cleanup_thread.is_alive()
            )

        # 锁外启动线程：_start_cleanup_loop 自己会再加锁判重
        if needs_start:
            self._start_cleanup_loop()
        return self.get_settings()

    def test_llm(self) -> dict[str, Any]:
        with self._lock:
            endpoint = self._settings.llm_endpoint.strip()
            api_key = self._settings.llm_api_key.strip()
            model = self._settings.llm_model.strip() or DEFAULT_TEXT_MODEL
            effort = self._settings.llm_reasoning_effort
        if not endpoint:
            return {"ok": False, "error": "未配置 LLM Endpoint"}
        t0 = time.monotonic()
        try:
            reply = postprocess._call(
                model=model,
                prompt="只回复 OK 两个字即可。",
                endpoint=endpoint,
                api_key=api_key,
                max_tokens=64,
                timeout=30,
                reasoning_effort=effort,
            )
            return {
                "ok": True,
                "seconds": round(time.monotonic() - t0, 2),
                "reply": reply[:80],
                "model": model,
            }
        except Exception as e:  # noqa: BLE001
            return {
                "ok": False,
                "seconds": round(time.monotonic() - t0, 2),
                "error": str(e)[:200],
            }

    # ---------------- 任务存储 ----------------

    def _load_jobs(self) -> dict[str, dict[str, Any]]:
        """加载任务表。**逐条容错**，绝不因单条畸形而丢掉整份。

        ## 为什么必须逐条容错（2026-11-23 对立性审查 C1）

        旧实现是 `for j in raw.values(): j.get(...)`，只要有**一条**不是 dict
        （JSON 手改出错、外部工具写坏、磁盘半写），`AttributeError` 就会被外层
        `except Exception: return {}` 吞掉 —— 整份任务表变成空字典。

        后果是链式的、灾难性的：
        1. `referenced_audio` 为空 → 清理时所有老音频都被当"孤儿"
        2. 下一次任何 `_save_jobs` 会把空表**写回磁盘**，原始记录永久消失
        3. 启动 60 秒后自动清理线程真删，全程无人确认

        实测复现：`{"j1":{正常}, "j2":"oops"}` → 加载后 jobs 为空 →
        健康任务的 100 天前录音被列入待删清单。

        所以这里改成：坏条目单独跳过并计数，好的照常加载。
        并且把"是否发生过损坏"记在 `self._jobs_load_error` 上，
        供清理逻辑做二次保护（损坏时拒绝真删）。
        """
        self._jobs_load_error = ""
        self._jobs_load_skipped = 0

        if not self.jobs_path.exists():
            return {}
        try:
            raw = json.loads(self.jobs_path.read_text(encoding="utf-8"))
        except Exception as exc:  # noqa: BLE001
            self._jobs_load_error = f"{type(exc).__name__}: {exc}"[:300]
            return {}
        if not isinstance(raw, dict):
            self._jobs_load_error = f"jobs.json 顶层不是对象，而是 {type(raw).__name__}"
            return {}

        good: dict[str, dict[str, Any]] = {}
        bad_keys: list[str] = []
        changed = False

        for key, job in raw.items():
            if not isinstance(job, dict):
                bad_keys.append(str(key))
                continue
            # 逐字段容错：status 读不出来也不能拖垮整条
            try:
                status = job.get("status")
            except Exception:  # noqa: BLE001
                bad_keys.append(str(key))
                continue
            if status in ACTIVE_STATUSES:
                job["status"] = "interrupted"
                job["error"] = "服务重启，任务已中断（如有已录制音频可点击重新转写）"
                try:
                    job.setdefault("logs", []).append(
                        f"[{time.strftime('%H:%M:%S')}] 服务重启，状态恢复为 interrupted"
                    )
                except Exception:  # noqa: BLE001
                    job["logs"] = []
                changed = True
            if not isinstance(job.get("logs"), list):
                job["logs"] = []
            # id 缺失时用 key 补上，否则后续按 job_id 索引会失效
            if not job.get("id"):
                job["id"] = str(key)
            good[str(key)] = job

        if bad_keys:
            self._jobs_load_skipped = len(bad_keys)
            self._jobs_load_error = (
                f"{len(bad_keys)} 条任务记录格式非法已跳过："
                + "、".join(bad_keys[:5])
            )
            # 把坏条目另存一份，避免被 _save_jobs 覆盖后彻底丢失
            try:
                _atomic_write_json(
                    self.data_dir / "jobs.corrupt.json",
                    {k: raw.get(k) for k in bad_keys},
                )
            except Exception:  # noqa: BLE001
                pass

        if changed:
            try:
                _atomic_write_json(self.jobs_path, good)
            except Exception:  # noqa: BLE001
                pass
        return good

    def _save_jobs(self) -> None:
        with self._lock:
            # 加载失败或有过损坏时，仍然允许保存（否则新任务无法落盘），
            # 但清理逻辑会自己拒绝真删 —— 见 run_cleanup 的保护。
            _atomic_write_json(self.jobs_path, self._jobs)

    def _append_log(self, job_id: str, msg: str) -> None:
        line = f"[{time.strftime('%H:%M:%S')}] {msg}"
        with self._lock:
            job = self._jobs.get(job_id)
            if not job:
                return
            logs = job.setdefault("logs", [])
            logs.append(line)
            if len(logs) > 200:
                del logs[:-200]
            job["updated_at"] = time.time()

    def _update_job(self, job_id: str, persist: bool = True, **fields: Any) -> dict[str, Any] | None:
        with self._lock:
            job = self._jobs.get(job_id)
            if not job:
                return None
            job.update(fields)
            job["updated_at"] = time.time()
            snap = dict(job)
        if persist:
            self._save_jobs()
        return snap

    def list_jobs(self) -> list[dict[str, Any]]:
        with self._lock:
            items = [self._serialize_job(j) for j in self._jobs.values()]
        items.sort(key=lambda x: x.get("created_at", 0), reverse=True)
        return items

    def get_job(self, job_id: str) -> dict[str, Any] | None:
        with self._lock:
            j = self._jobs.get(job_id)
            return self._serialize_job(j) if j else None

    def _serialize_job(self, j: dict[str, Any]) -> dict[str, Any]:
        out = dict(j)
        audio_p = Path(out["audio_path"]) if out.get("audio_path") else None
        if audio_p and audio_p.exists():
            try:
                size = audio_p.stat().st_size
                out["audio_bytes"] = size
                if out.get("status") == "recording":
                    out["audio_seconds"] = round(max(0, size - 44) / 32000.0, 1)
            except OSError:
                pass
        out["has_md"] = bool(out.get("md_path") and Path(out["md_path"]).exists())
        out["has_json"] = bool(out.get("json_path") and Path(out["json_path"]).exists())
        out["has_audio"] = bool(audio_p and audio_p.exists())
        # 不对外暴露完整流签名 URL（可能含认证参数）
        if out.get("source_url"):
            u = str(out["source_url"])
            out["source_url_preview"] = u[:80] + ("…" if len(u) > 80 else "")
            out.pop("source_url", None)
        return out

    # ---------------- 引擎管理 ----------------

    def _get_engine(self):
        with self._lock:
            threads = self._settings.local_threads
            use_lexicon = self._settings.use_lexicon
        key = (threads, use_lexicon)
        with self._engine_lock:
            if self._engine is not None and self._engine_key == key:
                return self._engine
            if self._engine_factory is not None:
                self._engine = self._engine_factory(threads, use_lexicon)
            else:
                from .sensevoice import SenseVoiceTranscriber

                if use_lexicon:
                    lexicon.apply_loaded(lexicon.load(None))
                self._engine = SenseVoiceTranscriber(
                    num_threads=threads,
                    language="auto",
                    correct_after=use_lexicon,
                )
            self._engine_key = key
            return self._engine

    def engine_loaded(self) -> bool:
        with self._engine_lock:
            return self._engine is not None

    # ---------------- 流录制任务 ----------------

    def start_stream_job(
        self,
        url: str,
        name: str = "",
        auto_transcribe: bool | None = None,
    ) -> dict[str, Any]:
        # 允许直接粘贴微信"下载"按钮复制出来的整条 ffmpeg 命令；
        # 也允许特殊值 "auto"（由调用方先去本机 ffmpeg 进程里找地址）
        clean_url = extract_url(url)
        if not is_url(clean_url):
            raise ValueError(
                "没识别出流地址。可以直接粘贴 URL，"
                "或整条 `ffmpeg -i \"https://...\" -c copy -y x.flv` 命令。"
            )
        if not find_ffmpeg():
            raise RuntimeError("系统未找到 ffmpeg，无法拉流录制")

        with self._lock:
            auto_tx = self._settings.auto_transcribe if auto_transcribe is None else bool(auto_transcribe)

        stamp = time.strftime("%Y%m%d_%H%M%S")
        job_id = f"{stamp}_{uuid.uuid4().hex[:6]}"
        title = (name or "").strip() or f"直播_{stamp}"
        safe = _safe_stem(title, fallback="stream")

        job: dict[str, Any] = {
            "id": job_id,
            "kind": "stream",
            "title": title,
            "status": "recording",
            "source_url": clean_url,
            "created_at": time.time(),
            "updated_at": time.time(),
            "audio_path": "",
            "audio_bytes": 0,
            "audio_seconds": 0.0,
            "md_path": "",
            "json_path": "",
            "progress_pct": 0.0,
            "done_seconds": 0.0,
            "total_seconds": 0.0,
            "sentence_count": 0,
            "changed_count": 0,
            "rtf": 0.0,
            "llm_status": "",
            "error": "",
            "auto_transcribe": auto_tx,
            "logs": [],
        }

        rec = StreamRecorder(
            clean_url,
            self.recordings_dir,
            name=safe,
            log_cb=lambda m: self._append_log(job_id, m),
        )

        with self._lock:
            self._jobs[job_id] = job
            self._recorders[job_id] = rec
            self._save_jobs()

        rec.start()
        if rec.out_path:
            self._update_job(job_id, audio_path=str(rec.out_path))
        self._append_log(job_id, f"开始拉流录制：{title}")

        t = threading.Thread(
            target=self._watch_recorder,
            args=(job_id, rec),
            daemon=True,
            name=f"watch-{job_id}",
        )
        t.start()
        return self.get_job(job_id) or job

    def _watch_recorder(self, job_id: str, rec: StreamRecorder) -> None:
        """监控录制进度，并按静音判停（直播结束后自动收工）。

        静音检测为什么要放在这个循环里而不是单独线程：
        它需要读**正在增长**的 WAV 尾段，与进度更新共用同一个 1 秒节拍，
        分两个线程会各自 stat 一遍文件、还要加锁协调，得不偿失。
        """
        with self._lock:
            auto_stop = self._settings.auto_stop_on_silence
            stop_secs = self._settings.silence_stop_seconds
            rms_thr = self._settings.silence_rms_threshold
            max_hours = self._settings.max_record_hours
            max_gb = self._settings.max_record_gb
            min_secs = self._settings.min_record_seconds

        max_bytes = max_gb * 1024 * 1024 * 1024 if max_gb > 0 else 0
        max_secs = max_hours * 3600 if max_hours > 0 else 0

        monitor = LiveSilenceMonitor(
            silence_rms=rms_thr,
            stop_after_seconds=stop_secs,
            require_sound_before=True,
            min_record_seconds=min_secs,
        )
        stop_reason = ""
        # 每 3 秒做一次静音采样：块长 0.5s，3 秒窗口能覆盖 6 个块，
        # 既够判静音，又不会因为频繁 seek 拖慢磁盘。
        silence_every = 3.0
        last_silence_check = 0.0
        last_status_log = 0.0
        last_grow_log = 0.0
        # 静音分析进度（秒）。用绝对位置而不是"上次读到的块"，
        # 这样无论 ffmpeg 的写盘节奏怎么变，都不会漏块或重算。
        analyzed_until = 0.0
        _t0 = time.time()

        while not self._shutdown_event.is_set() and rec.running:
            now = time.time()
            size = 0
            if rec.out_path and rec.out_path.exists():
                try:
                    size = rec.out_path.stat().st_size
                    self._update_job(
                        job_id,
                        persist=False,
                        audio_bytes=size,
                        audio_seconds=round(max(0, size - 44) / 32000.0, 1),
                    )
                except OSError:
                    pass

            # ---- 录制上限（对立性审查 H2）----
            # 永不结束且有声音的流会无限增长；磁盘写满后转写/日志全挂。
            if size > 0 and max_bytes and size >= max_bytes:
                stop_reason = f"达到单任务体积上限 {max_gb} GB，自动停止"
                self._append_log(job_id, stop_reason)
                break
            if max_secs and (now - _t0) >= max_secs:
                stop_reason = f"达到单任务时长上限 {max_hours} 小时，自动停止"
                self._append_log(job_id, stop_reason)
                break

            # ---- 静音判停 ----
            if auto_stop and rec.out_path and size > 44 and \
                    (now - last_silence_check) >= silence_every:
                last_silence_check = now
                audio_elapsed = max(now - _t0, max(0, size - 44) / 32000.0)

                # ---- 从"上次分析到的位置"读到现在 ----
                # 为什么不用 read_tail_window（真机实测 2026-11-23）：
                #   ffmpeg 写 WAV 是每 8 秒落一块、每块刚好 8 秒音频
                #   （实测 7.8s->262144B、16.1s->524288B、24.1s->786432B）。
                #   旧代码每 3 秒读"末尾 4.5 秒"，于是：
                #     · 8 秒周期里只有 1 次读到新数据，另 2 次纯空转
                #     · 4.5 秒窗口只覆盖 8 秒块的一半，另一半永远没被分析
                #   真实后果：一场 150 分钟的直播，静音检测**只成功跑了 3 次**，
                #   判停完全失效，最后靠播放地址 404 才收工，白录 1 小时以上。
                #
                # 改成按位置增量读之后，每块音频都被完整分析一次：
                # 不丢、不重、不空转。合成场景实测 40s 静音在 48s 准确判停。
                audio, sr, chunk_start = read_since(rec.out_path, analyzed_until)
                if audio.size > 0 and sr > 0:
                    verdict = monitor.poll(
                        audio, sr, audio_offset=chunk_start,
                        elapsed_seconds=audio_elapsed,
                    )
                    # 严格按"实际读到的音频"推进，不用文件大小推算，
                    # 免得多算一段还没落盘的区域。
                    analyzed_until = chunk_start + audio.size / float(sr)

                    if verdict.checked:
                        if verdict.silent_seconds > 0 and (now - last_status_log) >= 30:
                            last_status_log = now
                            self._append_log(
                                job_id,
                                f"静音监测：连续静音 {verdict.silent_seconds:.0f}s"
                                f"（阈值 {stop_secs}s，RMS {verdict.rms}）",
                            )
                        if verdict.should_stop:
                            stop_reason = verdict.reason
                            self._append_log(job_id, f"自动停止：{verdict.reason}")
                            break
                    elif (now - last_status_log) >= 300:
                        # 每 5 分钟报一次"分析到哪了"，便于确认没僵死。
                        # 不再输出"文件已停止增长，等待看门狗收尾"——那句话
                        # 是假的（文件其实一直在长），只会掩盖真问题。
                        last_status_log = now
                        self._append_log(
                            job_id,
                            f"静音监测正常（已分析 {analyzed_until:.0f}s，"
                            f"{size / 1048576:.1f} MB）",
                        )
                # audio.size == 0 表示这一轮还没写新块，直接等下一轮。
                # 不算异常、也不推进 analyzed_until —— 否则会跳着漏音频。


            time.sleep(1.0)

        rec.stop()

        # 如果是因为静音停的，把原因写进 job。
        # 必须持久化：容器重启/进程崩溃后要能知道"这场是静音收工还是异常中断"，
        # 否则事后完全无法归因（persist=False 只写内存，重启即丢，实测复现）。
        if stop_reason:
            self._update_job(job_id, stop_reason=stop_reason, persist=True)
            self._append_log(job_id, f"停止原因：{stop_reason}")

        with self._lock:
            self._recorders.pop(job_id, None)
            override = self._stop_overrides.pop(job_id, None)
            job = self._jobs.get(job_id)
            auto_tx = job.get("auto_transcribe", True) if job else True
            if override is not None:
                auto_tx = override

        out_path = rec.out_path
        if not out_path or not out_path.exists() or out_path.stat().st_size <= 44:
            err_tail = rec.errors[-1] if rec.errors else "未录到有效音频（流地址可能已失效或无音频轨）"
            self._append_log(job_id, f"录制失败：{err_tail}")
            self._update_job(job_id, status="failed", error=err_tail)
            return

        size = out_path.stat().st_size
        self._append_log(job_id, f"录制结束，文件 {out_path.name}（{size / 1048576:.1f} MB）")
        if auto_tx and not self._shutdown_event.is_set():
            self._update_job(
                job_id,
                status="queued",
                audio_path=str(out_path),
                audio_bytes=size,
                error="",
            )
            self._append_log(job_id, "已自动加入转写队列")
            self._asr_queue.put(job_id)
        else:
            self._update_job(
                job_id,
                status="recorded",
                audio_path=str(out_path),
                audio_bytes=size,
            )

    def stop_stream_job(self, job_id: str, transcribe: bool | None = None) -> dict[str, Any]:
        with self._lock:
            job = self._jobs.get(job_id)
            if not job:
                raise KeyError(f"任务不存在: {job_id}")
            rec = self._recorders.get(job_id)
            if transcribe is not None:
                self._stop_overrides[job_id] = bool(transcribe)
        if rec is None:
            raise ValueError("该任务当前不在录制中")
        self._append_log(job_id, "收到停止指令，正在收尾 WAV 头…")
        rec.stop()
        # 给 watcher 一点时间完成状态切换
        for _ in range(25):
            cur = self.get_job(job_id)
            if cur and cur["status"] != "recording":
                return cur
            time.sleep(0.1)
        return self.get_job(job_id) or job

    # ---------------- 上传与已有音频转写 ----------------

    def create_upload_job(
        self,
        filename: str,
        file_obj: IO[bytes],
        name: str = "",
        auto_transcribe: bool = True,
    ) -> dict[str, Any]:
        orig_name = Path(filename or "audio.wav").name
        ext = Path(orig_name).suffix.lower()
        if ext not in SUPPORTED_MEDIA_EXTS:
            raise ValueError(
                f"不支持的媒体格式 {ext or '(无扩展名)'}，支持：{', '.join(sorted(SUPPORTED_MEDIA_EXTS))}"
            )

        stamp = time.strftime("%Y%m%d_%H%M%S")
        job_id = f"{stamp}_{uuid.uuid4().hex[:6]}"
        title = (name or "").strip() or Path(orig_name).stem or f"音频_{stamp}"
        safe = _safe_stem(title, fallback="upload")
        dest = self.recordings_dir / f"{stamp}_{safe}{ext}"

        with dest.open("wb") as out:
            shutil.copyfileobj(file_obj, out, length=1024 * 1024)

        size = dest.stat().st_size
        if size == 0:
            dest.unlink(missing_ok=True)
            raise ValueError("上传的文件为空（0 字节）")

        job: dict[str, Any] = {
            "id": job_id,
            "kind": "upload",
            "title": title,
            "status": "queued" if auto_transcribe else "recorded",
            "created_at": time.time(),
            "updated_at": time.time(),
            "audio_path": str(dest),
            "audio_bytes": size,
            "audio_seconds": 0.0,
            "md_path": "",
            "json_path": "",
            "progress_pct": 0.0,
            "done_seconds": 0.0,
            "total_seconds": 0.0,
            "sentence_count": 0,
            "changed_count": 0,
            "rtf": 0.0,
            "llm_status": "",
            "error": "",
            "auto_transcribe": bool(auto_transcribe),
            "logs": [f"[{time.strftime('%H:%M:%S')}] 已接收上传文件 {dest.name}（{size / 1048576:.1f} MB）"],
        }
        with self._lock:
            self._jobs[job_id] = job
            self._save_jobs()

        if auto_transcribe:
            self._asr_queue.put(job_id)
        return self.get_job(job_id) or job

    def enqueue_transcribe(self, job_id: str) -> dict[str, Any]:
        with self._lock:
            job = self._jobs.get(job_id)
            if not job:
                raise KeyError(f"任务不存在: {job_id}")
            if job["status"] in ACTIVE_STATUSES:
                raise ValueError(f"任务当前状态为 {job['status']}，不能重复入队")
            audio_p = Path(job.get("audio_path") or "")
            if not audio_p.exists():
                raise FileNotFoundError("原始音频文件不存在，无法转写")
            job["status"] = "queued"
            job["error"] = ""
            job["progress_pct"] = 0.0
            self._save_jobs()
        self._append_log(job_id, "重新加入转写队列")
        self._asr_queue.put(job_id)
        return self.get_job(job_id) or job

    # ---------------- ASR + 后处理 Worker ----------------

    def _worker_loop(self) -> None:
        while not self._shutdown_event.is_set():
            try:
                job_id = self._asr_queue.get(timeout=0.5)
            except queue.Empty:
                continue
            if job_id is None:
                break
            try:
                self._process_asr_job(job_id)
            except Exception as e:  # noqa: BLE001
                self._append_log(job_id, f"任务异常：{e}")
                self._update_job(job_id, status="failed", error=str(e)[:300])

    def _ensure_16k_wav(self, job_id: str, src: Path) -> Path:
        """把非 WAV 或采样率不规范的媒体转为 16k mono PCM WAV（仅当需要时）。"""
        if src.suffix.lower() == ".wav":
            try:
                info = sf.info(str(src))
                if int(info.frames) > 0:
                    return src
            except Exception:
                pass

        ff = find_ffmpeg()
        if not ff:
            raise RuntimeError(f"文件 {src.name} 需要 ffmpeg 解码，但系统未找到 ffmpeg")

        self._update_job(job_id, status="converting")
        self._append_log(job_id, f"正在用 ffmpeg 提取 16k 单声道 WAV：{src.name}")
        target = src.with_suffix(".16k.wav")
        cmd = [
            ff, "-y", "-hide_banner", "-loglevel", "error",
            "-i", str(src),
            "-vn", "-ac", "1", "-ar", "16000", "-c:a", "pcm_s16le",
            str(target),
        ]
        r = winproc.run(cmd, capture_output=True, text=True, timeout=1800)
        if r.returncode != 0 or not target.exists() or target.stat().st_size <= 44:
            err = (r.stderr or "ffmpeg 转码失败").strip()[:200]
            raise RuntimeError(err)
        self._update_job(job_id, audio_path=str(target), audio_bytes=target.stat().st_size)
        return target

    def _process_asr_job(self, job_id: str) -> None:
        with self._lock:
            job = self._jobs.get(job_id)
            if not job:
                return
            src = Path(job["audio_path"])
            title = job.get("title") or src.stem
            keep_audio = self._settings.keep_audio

        wav_path = self._ensure_16k_wav(job_id, src)
        info = sf.info(str(wav_path))
        sr = int(info.samplerate)
        total = int(info.frames)
        dur = total / sr if sr else 0.0
        if dur <= 0:
            raise RuntimeError("音频时长为 0，无法转写")

        self._update_job(
            job_id,
            status="transcribing",
            total_seconds=round(dur, 1),
            audio_seconds=round(dur, 1),
        )
        self._append_log(job_id, f"加载 SenseVoice 模型并开始转写（{dur / 60:.1f} 分钟 @ {sr}Hz）")

        engine = self._get_engine()
        with self._lock:
            effort = self._settings.llm_reasoning_effort
            use_lexicon = self._settings.use_lexicon
        fp = f"nas_{effort}"
        sess = Session(
            self.transcripts_dir,
            name=title,
            sample_rate=sr,
            record_audio=False,
            audio_stem=wav_path.stem,
            fingerprint=fp,
            audio_path=wav_path,
        )
        sess.source_audio_name = wav_path.name

        rs = StreamResampler(in_rate=sr, out_rate=16000)
        block = sr * CHUNK_SECONDS
        n = max(1, (total + block - 1) // block)
        t0 = time.monotonic()

        with sf.SoundFile(str(wav_path)) as f:
            for bi in range(n):
                if self._shutdown_event.is_set():
                    self._update_job(job_id, status="interrupted", error="服务正在关闭")
                    return
                piece = f.read(block, dtype="float32", always_2d=False)
                if piece is None or len(piece) == 0:
                    break
                if piece.ndim > 1:
                    piece = piece.mean(axis=1)
                pcm = rs.process(piece)
                if len(pcm) == 0:
                    continue
                res = engine.transcribe(pcm, 16000)
                if res:
                    sess.add_segments(res, offset=(bi * block) / sr, expand_codes=use_lexicon)
                done = min(total, (bi + 1) * block) / sr
                pct = min(95.0, round(done / dur * 90.0, 1)) if dur else 90.0
                self._update_job(
                    job_id,
                    persist=False,
                    done_seconds=round(done, 1),
                    progress_pct=pct,
                    sentence_count=len(sess.segments),
                )
                self._append_log(
                    job_id,
                    f"块 {bi + 1}/{n} 完成（{done / 60:.1f}/{dur / 60:.1f} min，累计 {len(sess.segments)} 句）",
                )

        tail = rs.flush()
        if len(tail):
            res = engine.transcribe(tail, 16000)
            if res:
                sess.add_segments(res, offset=total / sr, expand_codes=use_lexicon)

        asr_elapsed = time.monotonic() - t0
        rtf = round(asr_elapsed / dur, 3) if dur else 0.0
        last_ts = float(sess.segments[-1]["end"]) if sess.segments else 0.0
        gap = dur - last_ts
        self._append_log(
            job_id,
            f"ASR 完成：{len(sess.segments)} 句，耗时 {asr_elapsed:.1f}s（RTF={rtf:.2f}）",
        )

        changed, llm_status = self._run_llm_postprocess(job_id, sess, title)
        doc = sess.close(
            extra_meta={
                "engine": "nas_runner",
                "asr": getattr(engine, "stats", {}) or {},
                "audio_seconds": round(dur, 1),
                "last_segment_end": round(last_ts, 1),
                "tail_gap_seconds": round(gap, 1),
                "transcript_complete": bool(sess.segments) and gap < 120,
                "llm_status": llm_status,
            }
        )
        if getattr(sess, "_digest_text", ""):
            sess.md_path.write_text(
                sess.md_path.read_text(encoding="utf-8")
                + "\n\n---\n\n# AI 整理要点\n\n"
                + sess._digest_text
                + "\n",
                encoding="utf-8",
            )

        if not keep_audio and wav_path.exists():
            wav_path.unlink(missing_ok=True)
            if src != wav_path and src.exists():
                src.unlink(missing_ok=True)

        self._update_job(
            job_id,
            persist=False,
            done_seconds=round(dur, 1),
            sentence_count=len(doc["segments"]),
            changed_count=changed,
            rtf=rtf,
            llm_status=llm_status,
            md_path=str(sess.md_path),
            json_path=str(sess.json_path),
            error="",
        )
        self._maybe_auto_title(job_id, getattr(sess, "_digest_text", "") or sess.segments)
        self._update_job(job_id, status="completed", progress_pct=100.0)
        self._append_log(job_id, f"全部完成：{sess.md_path.name}（{len(doc['segments'])} 句）")

    def _run_llm_postprocess(self, job_id: str, sess: Session, title: str) -> tuple[int, str]:
        with self._lock:
            enable_llm = self._settings.enable_llm
            endpoint = self._settings.llm_endpoint.strip()
            api_key = self._settings.llm_api_key.strip()
            model = self._settings.llm_model.strip() or DEFAULT_TEXT_MODEL
            effort = self._settings.llm_reasoning_effort
            use_lexicon = self._settings.use_lexicon

        if not sess.segments:
            return 0, "skipped: empty_segments"
        if not enable_llm or not endpoint:
            return 0, "skipped: llm_disabled"

        self._update_job(job_id, status="postprocessing", progress_pct=93.0)
        self._append_log(job_id, f"LLM 纠错与要点整理中（{model} / {effort}）…")

        vocab = ""
        if use_lexicon:
            try:
                lexicon.apply_loaded(lexicon.load(None))
                vocab = "、".join(lexicon.SCHOOLS + lexicon.TERMS)
            except Exception:
                vocab = ""

        lines = [s["text"] for s in sess.segments]
        changed = 0
        fix_stats: dict[str, Any] = {}
        try:
            fixed = postprocess.fix_errors(
                lines,
                model=model,
                endpoint=endpoint,
                api_key=api_key,
                vocabulary=vocab,
                reasoning_effort=effort,
                out_stats=fix_stats,
            )
            changed = sum(1 for a, b in zip(lines, fixed) if a and b and a != b)
            for seg, new in zip(sess.segments, fixed):
                if new and new != seg["text"]:
                    seg["raw_text"] = seg["text"]
                    seg["text"] = new
            self._append_log(job_id, f"LLM 纠错 {changed} 处")
        except Exception as e:  # noqa: BLE001
            self._append_log(job_id, f"LLM 纠错失败（保留原文）：{str(e)[:120]}")
            return 0, f"failed: {str(e)[:120]}"

        digest_stats: dict[str, Any] = {}
        try:
            digest = postprocess.build_digest(
                sess.segments,
                model=model,
                endpoint=endpoint,
                api_key=api_key,
                title=title,
                reasoning_effort=effort,
                out_stats=digest_stats,
            )
            sess._digest_text = digest  # type: ignore[attr-defined]
            if digest_stats.get("ok"):
                self._append_log(job_id, "AI 要点整理完成")
                return changed, "ok"
            err = digest_stats.get("error") or "digest_incomplete"
            self._append_log(job_id, f"AI 要点未完全成功：{err}")
            return changed, f"partial: {err}"
        except Exception as e:  # noqa: BLE001
            self._append_log(job_id, f"AI 要点失败：{str(e)[:120]}")
            return changed, f"partial: {str(e)[:120]}"

    def repostprocess_job(self, job_id: str) -> dict[str, Any]:
        """对已完成转写的任务重新执行 LLM 纠错与要点整理（不重跑 ASR）。"""
        with self._lock:
            job = self._jobs.get(job_id)
            if not job:
                raise KeyError(f"任务不存在: {job_id}")
            json_p = Path(job.get("json_path") or "")
            if not json_p.exists():
                raise FileNotFoundError("找不到逐字稿 JSON，无法重新 AI 整理")
            if not self._settings.enable_llm or not self._settings.llm_endpoint.strip():
                raise ValueError("请先在设置中启用 LLM 并填写 Endpoint")

        doc = json.loads(json_p.read_text(encoding="utf-8"))
        segments = doc.get("segments") or []
        if not segments:
            raise ValueError("逐字稿为空，无法整理")

        # 回退到 raw_text 以便换模型重跑时不被上次改写叠加污染
        for seg in segments:
            if seg.get("raw_text"):
                seg["text"] = seg["raw_text"]

        from . import render
        from .session import _ts

        class _TempSess:
            def __init__(self, segs):
                self.segments = segs
                self._digest_text = ""

        tmp = _TempSess(segments)
        changed, llm_status = self._run_llm_postprocess(job_id, tmp, job.get("title") or json_p.stem)  # type: ignore[arg-type]

        doc["segments"] = tmp.segments
        doc["full_text"] = "".join(s["text"] for s in tmp.segments)
        doc.setdefault("meta", {})["llm_status"] = llm_status
        # 产物自检：行数守恒 / 失败批 0 / 三份产物齐全，产物仍可能是坏的。
        # 2026-11-26 复盘发现某场终稿 574 句繁体、另两场幻觉句混进正文，
        # 旧验收一条都查不出来。纯本地计算，毫秒级，只报告不改写。
        q = artifacts.check_segments(tmp.segments)
        doc["meta"]["quality"] = q.to_meta()
        _atomic_write_json(json_p, doc)
        self._append_log(job_id, q.summary())
        for _kind, _samples in q.samples.items():
            for _s in _samples:
                self._append_log(job_id, f"  [{_kind}] {_s[:60]}")

        md_p = Path(job.get("md_path") or "")
        if md_p.exists():
            groups = render.group_segments(tmp.segments)
            body = render.render_paragraphs(tmp.segments)
            dur_s = _ts(float(doc.get("duration_seconds") or 0))
            header = (
                f"# 微信直播逐字稿（{job.get('title') or json_p.stem}）\n\n"
                f"- 会话 ID：`{doc.get('session_id', json_p.stem)}`\n"
                f"- 时长：{dur_s}\n"
                f"- 句数：{len(tmp.segments)}\n"
                f"- 段落：{len(groups)}\n"
                f"- 音频文件：`{doc.get('audio_file', '')}`\n\n"
                f"---\n\n"
            )
            full_md = header + body + "\n"
            if tmp._digest_text:
                full_md += "\n---\n\n# AI 整理要点\n\n" + tmp._digest_text + "\n"
            md_p.write_text(full_md, encoding="utf-8")

        self._maybe_auto_title(job_id, tmp._digest_text or tmp.segments)
        return self._update_job(
            job_id,
            status="completed",
            progress_pct=100.0,
            changed_count=changed,
            llm_status=llm_status,
        ) or job

    # ---------------- 任务重命名与 AI 自动识别标题 ----------------

    def rename_job(
        self,
        job_id: str,
        new_title: str,
        *,
        auto_named: bool | None = None,
    ) -> dict[str, Any]:
        """更新任务名称，并同步更新磁盘上的 .md 标题行与 .json 元数据。"""
        clean = " ".join((new_title or "").split())[:120].strip()
        if not clean:
            raise ValueError("任务名称不能为空")

        with self._lock:
            job = self._jobs.get(job_id)
            if not job:
                raise KeyError(f"任务不存在: {job_id}")
            job["title"] = clean
            if auto_named is not None:
                job["auto_named"] = bool(auto_named)
            else:
                job["auto_named"] = False
            job["updated_at"] = time.time()
            md_raw = str(job.get("md_path") or "")
            json_raw = str(job.get("json_path") or "")
            self._save_jobs()

        md_p = Path(md_raw) if md_raw else None
        if md_p and md_p.exists():
            try:
                text = md_p.read_text(encoding="utf-8")
                lines = text.splitlines(keepends=True)
                if lines and lines[0].startswith("# 微信直播逐字稿"):
                    eol = "\n" if lines[0].endswith("\n") else ""
                    lines[0] = f"# 微信直播逐字稿（{clean}）{eol}"
                    md_p.write_text("".join(lines), encoding="utf-8")
            except OSError:
                pass

        json_p = Path(json_raw) if json_raw else None
        if json_p and json_p.exists():
            try:
                doc = json.loads(json_p.read_text(encoding="utf-8"))
                if isinstance(doc, dict):
                    doc["title"] = clean
                    _atomic_write_json(json_p, doc)
            except Exception:  # noqa: BLE001
                pass

        return self.get_job(job_id) or {}

    def _maybe_auto_title(self, job_id: str, source: str | list[dict]) -> str:
        """若当前标题仍是时间/ID 占位名，且启用了自动命名与 LLM，则自动提炼标题。"""
        with self._lock:
            job = self._jobs.get(job_id)
            if not job:
                return ""
            if not self._settings.auto_title:
                return ""
            if not self._settings.enable_llm or not self._settings.llm_endpoint.strip():
                return ""
            cur_title = str(job.get("title") or "")
            if not is_placeholder_title(cur_title):
                return ""
            model = self._settings.llm_model.strip() or DEFAULT_TEXT_MODEL
            endpoint = self._settings.llm_endpoint.strip()
            api_key = self._settings.llm_api_key.strip()
            effort = self._settings.llm_reasoning_effort

        suggested = postprocess.suggest_title(
            source,
            model=model,
            endpoint=endpoint,
            api_key=api_key,
            reasoning_effort=effort,
        )
        if not suggested or suggested == cur_title:
            return ""
        self.rename_job(job_id, suggested, auto_named=True)
        self._append_log(job_id, f"AI 自动识别直播名称：{suggested}")
        return suggested

    def auto_title_job(self, job_id: str) -> dict[str, Any]:
        """手动触发：根据已完成任务的 AI 要点或逐字稿，用 LLM 识别并命名。"""
        with self._lock:
            job = self._jobs.get(job_id)
            if not job:
                raise KeyError(f"任务不存在: {job_id}")
            if not self._settings.enable_llm or not self._settings.llm_endpoint.strip():
                raise ValueError("请先在「系统设置」中启用 LLM 并填写 Endpoint")
            md_raw = str(job.get("md_path") or "")
            json_raw = str(job.get("json_path") or "")
            model = self._settings.llm_model.strip() or DEFAULT_TEXT_MODEL
            endpoint = self._settings.llm_endpoint.strip()
            api_key = self._settings.llm_api_key.strip()
            effort = self._settings.llm_reasoning_effort

        source: str | list[dict] = ""
        md_p = Path(md_raw) if md_raw else None
        if md_p and md_p.exists():
            md_text = md_p.read_text(encoding="utf-8")
            marker = "# AI 整理要点"
            if marker in md_text:
                source = md_text.split(marker, 1)[1].strip()
            else:
                source = md_text

        if not source or len(str(source)) < 20:
            json_p = Path(json_raw) if json_raw else None
            if json_p and json_p.exists():
                doc = json.loads(json_p.read_text(encoding="utf-8"))
                source = doc.get("segments") or doc.get("full_text") or ""

        if not source:
            raise ValueError("任务暂无可用的逐字稿或 AI 要点，无法自动识别名称")

        suggested = postprocess.suggest_title(
            source,
            model=model,
            endpoint=endpoint,
            api_key=api_key,
            reasoning_effort=effort,
        )
        if not suggested:
            raise RuntimeError("AI 未能提炼出有效标题，请检查 LLM 接口连通性或手动重命名")

        updated = self.rename_job(job_id, suggested, auto_named=True)
        self._append_log(job_id, f"AI 识别命名：{suggested}")
        return updated

    # ---------------- 磁盘保留策略 ----------------

    def _start_cleanup_loop(self) -> None:
        """后台定时清理线程（幂等，可由 update_settings 在 0→>0 时懒启动）。

        为什么是"启动后延迟 60 秒再跑第一次"：
        容器刚起来时 CPU/IO 都很忙（模型加载、任务恢复），
        清理是纯磁盘扫描，没必要抢这个时间窗。

        ## 为什么允许重复调用（对立性审查 M2）

        旧实现只在 __init__ 且 retention_days>0 时建线程。
        用户在网页里把保留天数从 0 改成 30 后，定时清理**永远不会启动**，
        直到重启容器 —— 表现是"设置了但没生效"。
        现在 update_settings 会在 0→>0 时调本方法，靠 `_cleanup_thread` 判重。
        """
        with self._lock:
            if self._settings.retention_days <= 0:
                return
            if self._cleanup_thread is not None and self._cleanup_thread.is_alive():
                return

        def loop() -> None:
            # 首次延迟，避开启动高峰
            if self._shutdown_event.wait(60):
                return
            while not self._shutdown_event.is_set():
                try:
                    with self._lock:
                        enabled = self._settings.retention_days > 0
                    if enabled:
                        self.run_cleanup(dry_run=False, trigger="auto")
                except Exception as exc:  # noqa: BLE001
                    self._last_cleanup = {
                        "at": time.strftime("%Y-%m-%d %H:%M:%S"),
                        "ok": False,
                        "error": f"{type(exc).__name__}: {exc}"[:300],
                        "trigger": "auto",
                    }
                hours = max(1, int(self._settings.cleanup_interval_hours))
                if self._shutdown_event.wait(hours * 3600):
                    return

        self._cleanup_thread = threading.Thread(
            target=loop, daemon=True, name="nas-cleanup"
        )
        self._cleanup_thread.start()

    def cleanup_plan(self) -> dict[str, Any]:
        """预览清理计划（不删任何东西）。"""
        with self._lock:
            days = self._settings.retention_days
            del_tr = self._settings.cleanup_delete_transcripts
            jobs = dict(self._jobs)
        return retention.plan_cleanup(
            jobs,
            self.recordings_dir,
            self.transcripts_dir,
            retention_days=days,
            delete_transcripts=del_tr,
        ).to_dict()

    def run_cleanup(self, dry_run: bool = True, trigger: str = "manual") -> dict[str, Any]:
        """计算并（可选）执行清理。

        dry_run=True 只返回计划，供 UI 预览；dry_run=False 才真删。

        ## 二次保护（对立性审查 C1）

        真删前再检查一遍"任务表是否可信"：
        - `_jobs_load_error` 非空（jobs.json 损坏过）→ 拒绝真删
        - 任务表为空、但 recordings 目录里有音频 → 拒绝真删
          （正常情况下不可能：有音频就必然有任务记录。
            为空只能是"表读坏了"，这时删任何东西都可能误伤）

        预览(dry_run)不拦，否则用户看不到问题在哪。
        """
        with self._lock:
            days = self._settings.retention_days
            del_tr = self._settings.cleanup_delete_transcripts
            jobs = dict(self._jobs)
            load_error = self._jobs_load_error

        if not dry_run:
            if load_error:
                return {
                    "ok": False, "dry_run": False, "trigger": trigger,
                    "error": f"任务表存在损坏，为安全起见拒绝自动删除：{load_error}",
                    "deleted_count": 0, "failed_count": 0,
                    "freed_bytes": 0, "freed_mb": 0, "freed_human": "0 B",
                    "deleted": [], "failed": [],
                }
            if not jobs and self._has_audio_files():
                return {
                    "ok": False, "dry_run": False, "trigger": trigger,
                    "error": ("任务表为空但录制目录里有音频 —— 疑似任务表损坏，"
                              "拒绝自动删除。请先检查 jobs.json。"),
                    "deleted_count": 0, "failed_count": 0,
                    "freed_bytes": 0, "freed_mb": 0, "freed_human": "0 B",
                    "deleted": [], "failed": [],
                }

        plan = retention.plan_cleanup(
            jobs,
            self.recordings_dir,
            self.transcripts_dir,
            retention_days=days,
            delete_transcripts=del_tr,
        )
        if dry_run:
            payload = plan.to_dict()
            payload["dry_run"] = True
            payload["trigger"] = trigger
            # 预览时把"任务表不安全"直接摆出来，让人知道真删会被拒绝
            if load_error:
                payload["warning"] = (
                    f"任务表损坏（{load_error}）。预览基于已加载的有效任务，"
                    "但真删会被拒绝，请先修复 jobs.json。"
                )
            elif not jobs and self._has_audio_files():
                payload["warning"] = (
                    "任务表为空但录制目录有音频 —— 疑似任务表损坏，真删会被拒绝。"
                )
            return payload

        result = retention.execute_cleanup(plan)
        payload = {**result.to_dict(), "plan": plan.to_dict(),
                   "dry_run": False, "trigger": trigger,
                   "at": time.strftime("%Y-%m-%d %H:%M:%S")}
        self._last_cleanup = payload

        if result.deleted:
            self._log_cleanup(payload)
        return payload

    # cleanup.log 体积上限。每日一次清理，留 200KB 足够回溯几个月。
    CLEANUP_LOG_MAX_BYTES = 200 * 1024

    def _log_cleanup(self, payload: dict[str, Any]) -> None:
        """把清理结果落盘：一行摘要 + 一份**完整删除清单**。

        对立性审查 C2 指出：只记"删了几项、多少 MB"无法事后审计
        （"我哪天的那场录音哪去了？"）。所以除了摘要行，
        每批删除都把完整路径清单写成 JSON 存进 `cleanup_history/`。
        """
        ts = time.strftime("%Y-%m-%d %H:%M:%S")
        line = (
            f"[{ts}] 清理：删除 {payload.get('deleted_count', 0)} 项，"
            f"释放 {payload.get('freed_human') or (str(payload.get('freed_mb', 0)) + ' MB')}"
            f"（阈值 {self._settings.retention_days} 天，触发方式 {payload.get('trigger', '?')}）\n"
        )
        path = self.data_dir / "cleanup.log"
        try:
            # 必须 with：旧写法 `open().write()` 不关文件，
            # 7x24 常驻会累积句柄泄漏。
            if path.exists() and path.stat().st_size > self.CLEANUP_LOG_MAX_BYTES:
                # 超限就截断重来，不做轮转：这日志只用于人工回溯，保留最近的更有用
                with path.open("w", encoding="utf-8") as fh:
                    fh.write(line)
            else:
                with path.open("a", encoding="utf-8") as fh:
                    fh.write(line)
        except OSError:
            pass

        # 完整清单：只保留最近 100 批，避免无限堆积
        try:
            hist = self.data_dir / "cleanup_history"
            hist.mkdir(parents=True, exist_ok=True)
            stamp = time.strftime("%Y%m%d_%H%M%S")
            _atomic_write_json(hist / f"{stamp}.json", {
                "at": ts,
                "trigger": payload.get("trigger", ""),
                "retention_days": self._settings.retention_days,
                "deleted": payload.get("deleted", []),
                "failed": payload.get("failed", []),
            })
            files = sorted(hist.glob("*.json"))
            for old in files[:-100]:
                try:
                    old.unlink()
                except OSError:
                    pass
        except OSError:
            pass

    def _has_audio_files(self) -> bool:
        """录制目录里是否有任何音频文件（不含临时残骸）。"""
        try:
            if not self.recordings_dir.is_dir():
                return False
            for p in self.recordings_dir.iterdir():
                if p.is_file() and p.suffix.lower() == ".wav" and not p.is_symlink():
                    return True
        except OSError:
            return False
        return False

    def cleanup_status(self) -> dict[str, Any]:
        with self._lock:
            return {
                "retention_days": self._settings.retention_days,
                "cleanup_delete_transcripts": self._settings.cleanup_delete_transcripts,
                "cleanup_interval_hours": self._settings.cleanup_interval_hours,
                "auto_enabled": self._settings.retention_days > 0,
                "worker_alive": bool(self._cleanup_thread and self._cleanup_thread.is_alive()),
                "last_cleanup": self._last_cleanup,
            }

    # ---------------- 转录文件库 ----------------

    def library(self) -> list[dict[str, Any]]:
        """列出磁盘上所有转录产物（以磁盘为准，不依赖 jobs.json）。"""
        with self._lock:
            jobs = dict(self._jobs)
        return retention.scan_library(self.transcripts_dir, jobs)

    # 导出上限。NAS 实测可用内存只有约 4.2GB，整包进内存必须有硬上限，
    # 否则一次导出就能把容器顶到 OOM（转写任务会跟着挂）。
    EXPORT_MAX_BYTES = 256 * 1024 * 1024     # 256 MB

    def export_library(self) -> tuple[bytes, str]:
        """把整个转录文件库打包成 zip，返回 (字节内容, 建议文件名)。

        在内存里打包：转录产物总量很小（每场几十 KB），
        不值得为它写临时文件再删。但加了两道保护：

        1. **先算总量再打包**：超过 `EXPORT_MAX_BYTES` 直接拒绝并说明原因，
           而不是打一半把内存吃满。
        2. **跳过符号链接**：`rglob` 本身不跟随目录链接，但文件级链接
           可能指向库外的大文件，一并跳过更安全。
        """
        import io
        import zipfile

        tr_root = self.transcripts_dir
        if not tr_root.is_dir():
            raise FileNotFoundError("转录文件库是空的，没有可导出的内容")

        files: list[Path] = []
        total = 0
        for p in sorted(tr_root.rglob("*")):
            try:
                if p.is_symlink() or not p.is_file():
                    continue
                size = p.stat().st_size
            except OSError:
                continue
            total += size
            if total > self.EXPORT_MAX_BYTES:
                raise ValueError(
                    f"转录库超过导出上限（{self.EXPORT_MAX_BYTES // 1048576} MB），"
                    "请改用逐个下载或先归档清理"
                )
            files.append(p)

        if not files:
            raise FileNotFoundError("转录文件库是空的，没有可导出的内容")

        buf = io.BytesIO()
        count = 0
        with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
            for p in files:
                try:
                    zf.write(p, arcname=str(p.relative_to(tr_root)))
                    count += 1
                except OSError:
                    continue
        if count == 0:
            raise FileNotFoundError("转录文件库是空的，没有可导出的内容")
        stamp = time.strftime("%Y%m%d_%H%M%S")
        return buf.getvalue(), f"wxlive_transcripts_{stamp}.zip"

    # ---------------- 查看与删除 ----------------

    def get_transcript(self, job_id: str) -> dict[str, Any]:
        with self._lock:
            job = self._jobs.get(job_id)
            if not job:
                raise KeyError(f"任务不存在: {job_id}")
            md_p = Path(job.get("md_path") or "")
            json_p = Path(job.get("json_path") or "")

        md_text = md_p.read_text(encoding="utf-8") if md_p.exists() else ""
        doc = json.loads(json_p.read_text(encoding="utf-8")) if json_p.exists() else {}
        return {
            "job": self.get_job(job_id),
            "md_text": md_text,
            "segments": doc.get("segments") or [],
            "duration_seconds": doc.get("duration_seconds", 0),
        }

    def delete_job(self, job_id: str, delete_files: bool = False) -> bool:
        with self._lock:
            job = self._jobs.get(job_id)
            if not job:
                return False
            if job["status"] in ("recording", "transcribing", "converting", "postprocessing"):
                raise ValueError("任务正在运行中，请先停止后再删除")
            self._jobs.pop(job_id, None)
            self._save_jobs()

        if delete_files:
            ap = Path(job.get("audio_path") or "")
            if ap.exists() and ap.parent == self.recordings_dir:
                ap.unlink(missing_ok=True)
            mp = Path(job.get("md_path") or "")
            if mp.exists() and mp.parent.parent == self.transcripts_dir:
                shutil.rmtree(mp.parent, ignore_errors=True)
        return True

    def shutdown(self) -> None:
        self._shutdown_event.set()
        with self._lock:
            recs = list(self._recorders.values())
        for r in recs:
            try:
                r.stop()
            except Exception:
                pass
        self._asr_queue.put(None)
        if self._worker.is_alive():
            self._worker.join(timeout=3.0)
