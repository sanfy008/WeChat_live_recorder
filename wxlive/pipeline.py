"""主流水线：采集 → 切分 → 转写 → 落盘。

线程模型（关键）：
  主线程   : 从采集队列取块 → VAD 切分 → 投递到转写队列
  转写线程  : 从转写队列取片段 → ASR → 写盘

为什么必须分两个线程：
  ASR 在 CPU 上可能要 0.5-1x 实时，如果串在主线程里，
  采集队列会在几分钟内被灌满，回调整块丢帧 —— 这就是"录音链路一卡就全废"的典型死法。
  分离后：录音永不阻塞，转写慢只是排队，音频完整性优先。
"""

from __future__ import annotations

import queue
import signal
import threading
import time
from pathlib import Path

import numpy as np

from .audio_capture import LoopbackRecorder
from .config import Config
from .engine import FallbackEngine
from .resample import StreamResampler
from .session import Session
from .transcriber import Transcriber
from .vad import Segment, Segmenter


def _extract_rtf(stats: dict) -> float:
    """从任意引擎的 stats 里取 RTF，取不到就返回 0（不能抛异常）。"""
    try:
        if isinstance(stats, dict):
            if "rtf" in stats:
                return float(stats["rtf"])
            for key in ("primary", "fallback"):
                sub = stats.get(key)
                if isinstance(sub, dict) and "rtf" in sub:
                    return float(sub["rtf"])
    except Exception:
        pass
    return 0.0


def build_engine(cfg: Config):
    """按 engine 配置构造转写引擎。

    返回对象需实现 transcribe(audio, sample_rate) -> list[dict] 和 stats。
    """
    local = None
    primary = None

    def _local():
        nonlocal local
        if local is None:
            # 本地引擎也吃词表：作为 initial_prompt 提供领域偏置。
            # 措辞刻意写成陈述句而非"以下是……"，避免 whisper 当上文复读。
            local = Transcriber(
                model_size=cfg.asr.model_size,
                device=cfg.asr.device,
                compute_type=cfg.asr.compute_type,
                language=cfg.asr.language,
                beam_size=cfg.asr.beam_size,
                vad_filter=cfg.asr.vad_filter,
                initial_prompt=cfg.asr.initial_prompt or hint,
            )
        return local

    # 领域词表：注入 prompt + 转写后错字纠正，能明显改善专有名词识别
    hint = ""
    if cfg.asr.use_lexicon:
        from . import lexicon

        path = Path(cfg.asr.lexicon_path) if cfg.asr.lexicon_path else None
        lexicon.apply_loaded(lexicon.load(path))
        hint = lexicon.build_prompt_hint()

    def _gemini():
        from .gemini_asr import GeminiASR

        return GeminiASR(
            model=cfg.asr.gemini_model,
            endpoint=cfg.asr.gemini_endpoint,
            api_key=cfg.asr.gemini_api_key,
            lexicon_hint=hint,
            correct_after=cfg.asr.use_lexicon,
        )

    kind = (cfg.asr.engine or "auto").lower()

    if kind == "auto":
        return FallbackEngine(primary=_gemini(), fallback=_local())
    if kind == "gemini":
        # 纯 gemini：仍包一层 FallbackEngine，但主引擎失败时没有本地兜底，
        # 保留包装是为了 stats 结构一致；如需纯云端可设 --engine gemini。
        return FallbackEngine(primary=_gemini(), fallback=_local())
    return _local()


class Pipeline:
    def __init__(self, cfg: Config) -> None:
        self.cfg = cfg
        self.segmenter = Segmenter(
            sample_rate=cfg.audio.sample_rate,
            silence_rms=cfg.vad.silence_rms,
            min_silence_seconds=cfg.vad.min_silence_seconds,
            min_speech_seconds=cfg.vad.min_speech_seconds,
            max_segment_seconds=cfg.vad.max_segment_seconds,
        )
        self.recorder = LoopbackRecorder(
            device_hint=cfg.audio.device_hint,
            chunk_seconds=cfg.audio.chunk_seconds,
            mono=cfg.audio.mono,
        )
        # 设备原生采样率 → 16kHz。不做这一步，ASR 会把音频当慢放处理而编造内容。
        self.resampler = StreamResampler(
            in_rate=self.recorder.device.rate, out_rate=cfg.audio.sample_rate
        )
        # 引擎在这里构造，支持本地/云端/自动降级三种模式
        self.transcriber = build_engine(cfg)

        from .config import RECORDINGS_DIR, TRANSCRIPTS_DIR

        self.session = Session(RECORDINGS_DIR, name=cfg.session_name,
                               sample_rate=self.recorder.device.rate)
        # 逐字稿放单独目录（软链/复制关系在 close 时处理）
        self._transcripts_dir = TRANSCRIPTS_DIR

        self._asr_queue: queue.Queue[Segment | None] = queue.Queue(maxsize=200)
        self._stop = threading.Event()
        self._asr_thread: threading.Thread | None = None

    # ---------- 转写线程 ----------

    def _asr_worker(self) -> None:
        # 云端模式下把碎片合并成 ≥cloud_min_segment_seconds 的块再发，
        # 因为 Gemini 每次请求有约 3 秒固定开销，碎片会让 RTF 飙到 3.0。
        min_sec = 0.0
        if (self.cfg.asr.engine or "auto").lower() != "local":
            min_sec = self.cfg.vad.cloud_min_segment_seconds

        pending: list[Segment] = []
        pending_dur = 0.0

        def flush_pending(final: bool = False) -> None:
            nonlocal pending, pending_dur
            if not pending:
                return
            if not final and pending_dur < min_sec:
                return
            merged = np.concatenate([p.audio for p in pending])
            start = pending[0].start_ts
            pending, pending_dur = [], 0.0
            try:
                res = self.transcriber.transcribe(merged, self.cfg.audio.sample_rate)
                if res:
                    self.session.add_segments(
                        res, offset=start, expand_codes=self.cfg.asr.use_lexicon
                    )
            except Exception as e:  # noqa: BLE001
                print(f"[asr] 片段转写失败 ({start:.1f}s): {e}")

        while True:
            seg = self._asr_queue.get()
            if seg is None:
                flush_pending(final=True)
                break
            pending.append(seg)
            pending_dur += seg.duration
            flush_pending()

    # ---------- 主循环 ----------

    def run(self, max_seconds: float = 0.0) -> dict:
        print("=" * 62)
        print("微信直播录音 · 转录流水线")
        print("=" * 62)
        print(f"会话: {self.session.id}")

        self.recorder.start()
        self._asr_thread = threading.Thread(target=self._asr_worker, daemon=True, name="asr")
        self._asr_thread.start()

        t_start = time.time()
        last_report = t_start
        n_chunks = 0
        n_segments = 0

        try:
            while not self._stop.is_set():
                if max_seconds and (time.time() - t_start) >= max_seconds:
                    print(f"\n[main] 达到设定时长 {max_seconds}s，停止。")
                    break

                chunk = self.recorder.read(timeout=0.5)
                if chunk is None:  # 采集端已结束
                    break
                if len(chunk) == 0:
                    continue

                n_chunks += 1
                # 原始音频立即落盘：即使 ASR 排不上队，声音不会丢
                self.session.write_audio(chunk)

                # 送 ASR 前必须转成 16kHz（与会话音频分开，原始 WAV 保持设备原采样率）
                resampled = self.resampler.process(chunk)
                for seg in self.segmenter.feed(resampled):
                    n_segments += 1
                    try:
                        self._asr_queue.put(seg, timeout=5)
                    except queue.Full:
                        print("[warn] 转写队列已满，丢弃片段（ASR 跟不上）", flush=True)

                # 每 30 秒打印一次状态
                now = time.time()
                if now - last_report >= 30:
                    el = now - t_start
                    # 不同引擎的 stats 结构不同（本地引擎有顶层 rtf，
                    # FallbackEngine 是嵌套的），这里统一容错，不能因为状态栏崩掉主循环。
                    rtf = _extract_rtf(self.transcriber.stats)
                    print(
                        f"\r[status] 已录 {el/60:.1f}min | 块 {n_chunks} | "
                        f"片段 {n_segments} | 已出句 {len(self.session.segments)} | "
                        f"RTF {rtf:.2f} | 队列 {self._asr_queue.qsize()}",
                        end="", flush=True,
                    )
                    last_report = now

        except KeyboardInterrupt:
            print("\n[main] 收到中断信号，正在收尾…")
        finally:
            # 收尾顺序很重要：先停采集 → 再 flush 残留 → 最后等 ASR 队列排空
            leftover = self.resampler.flush()
            if len(leftover):
                for seg in self.segmenter.feed(leftover):
                    try:
                        self._asr_queue.put(seg, timeout=5)
                    except queue.Full:
                        pass

            tail = self.segmenter.flush()
            if tail:
                try:
                    self._asr_queue.put(tail, timeout=5)
                except queue.Full:
                    pass

            self.recorder.stop()
            self._asr_queue.put(None)

            print("\n[main] 等待剩余片段转写完成…")
            if self._asr_thread is not None:
                self._asr_thread.join(timeout=600)

        doc = self.session.close(extra_meta={"asr": self.transcriber.stats})
        print(f"\n完成。逐字稿: {self.session.md_path}")
        print(f"      结构化: {self.session.json_path}")
        print(f"      音频  : {self.session.audio_path}")
        return doc

    def stop(self) -> None:
        self._stop.set()


def run_pipeline(cfg: Config, max_seconds: float = 0.0) -> dict:
    from .config import ensure_dirs

    ensure_dirs()
    p = Pipeline(cfg)

    def _sigint(signum, frame):
        p.stop()

    try:
        signal.signal(signal.SIGINT, _sigint)
    except Exception:
        pass

    return p.run(max_seconds=max_seconds)
