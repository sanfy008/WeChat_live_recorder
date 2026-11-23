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
import shutil
import threading
import time
import uuid
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import IO, Any, Callable

import numpy as np
import soundfile as sf

from . import lexicon, postprocess
from .config import (
    DEFAULT_REASONING_EFFORT,
    DEFAULT_TEXT_MODEL,
    OUTPUT_DIR,
    validate_reasoning_effort,
)
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
        self._jobs: dict[str, dict[str, Any]] = self._load_jobs()
        self._recorders: dict[str, StreamRecorder] = {}
        self._stop_overrides: dict[str, bool] = {}

        self._asr_queue: queue.Queue[str | None] = queue.Queue()
        self._shutdown_event = threading.Event()
        self._worker = threading.Thread(
            target=self._worker_loop, daemon=True, name="nas-asr-worker"
        )
        self._worker.start()

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
                ):
                    if key in raw and raw[key] is not None:
                        setattr(cfg, key, raw[key])
                cfg.local_threads = max(1, min(16, int(cfg.local_threads)))
                cfg.llm_reasoning_effort = validate_reasoning_effort(cfg.llm_reasoning_effort)
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
            for bool_key in ("use_lexicon", "enable_llm", "auto_transcribe", "keep_audio"):
                if bool_key in patch and patch[bool_key] is not None:
                    setattr(s, bool_key, bool(patch[bool_key]))
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
            return s.to_public_dict()

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
        if not self.jobs_path.exists():
            return {}
        try:
            raw = json.loads(self.jobs_path.read_text(encoding="utf-8"))
            if not isinstance(raw, dict):
                return {}
            changed = False
            for j in raw.values():
                if j.get("status") in ACTIVE_STATUSES:
                    j["status"] = "interrupted"
                    j["error"] = "服务重启，任务已中断（如有已录制音频可点击重新转写）"
                    j.setdefault("logs", []).append(
                        f"[{time.strftime('%H:%M:%S')}] 服务重启，状态恢复为 interrupted"
                    )
                    changed = True
            if changed:
                _atomic_write_json(self.jobs_path, raw)
            return raw
        except Exception:
            return {}

    def _save_jobs(self) -> None:
        with self._lock:
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
        # 允许直接粘贴微信"下载"按钮复制出来的整条 ffmpeg 命令
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
        while not self._shutdown_event.is_set() and rec.running:
            if rec.out_path and rec.out_path.exists():
                try:
                    sz = rec.out_path.stat().st_size
                    self._update_job(
                        job_id,
                        persist=False,
                        audio_bytes=sz,
                        audio_seconds=round(max(0, sz - 44) / 32000.0, 1),
                    )
                except OSError:
                    pass
            time.sleep(1.0)

        rec.stop()
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
            status="completed",
            progress_pct=100.0,
            done_seconds=round(dur, 1),
            sentence_count=len(doc["segments"]),
            changed_count=changed,
            rtf=rtf,
            llm_status=llm_status,
            md_path=str(sess.md_path),
            json_path=str(sess.json_path),
            error="",
        )
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
        _atomic_write_json(json_p, doc)

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

        return self._update_job(
            job_id,
            status="completed",
            progress_pct=100.0,
            changed_count=changed,
            llm_status=llm_status,
        ) or job

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
