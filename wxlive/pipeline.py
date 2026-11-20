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
            backend = (getattr(cfg.asr, "local_backend", "sensevoice") or "sensevoice").strip().lower()
            if backend == "sensevoice":
                from .sensevoice import SenseVoiceTranscriber

                local = SenseVoiceTranscriber(
                    model_repo_or_dir=getattr(
                        cfg.asr, "sensevoice_model",
                        "csukuangfj/sherpa-onnx-sense-voice-zh-en-ja-ko-yue-2024-07-17",
                    ),
                    num_threads=getattr(cfg.asr, "local_threads", 2),
                    language="auto" if cfg.asr.language == "zh" else cfg.asr.language,
                    correct_after=cfg.asr.use_lexicon,
                )
            else:
                # 本地 whisper 也吃词表：作为 initial_prompt 提供领域偏置。
                # 措辞刻意写成陈述句而非"以下是……"，避免 whisper 当上文复读。
                local = Transcriber(
                    model_size=cfg.asr.model_size,
                    device=cfg.asr.device,
                    compute_type=cfg.asr.compute_type,
                    language=cfg.asr.language,
                    beam_size=cfg.asr.beam_size,
                    vad_filter=cfg.asr.vad_filter,
                    initial_prompt=cfg.asr.initial_prompt or hint,
                    correct_after=cfg.asr.use_lexicon,
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
            workers=getattr(cfg.asr, "gemini_workers", 1),
        )

    kind = (cfg.asr.engine or "auto").lower()

    if kind == "auto":
        return FallbackEngine(primary=_gemini(), fallback_factory=_local)
    if kind == "gemini":
        # 纯 gemini：仍包一层 FallbackEngine，但主引擎失败时没有本地兜底，
        # 保留包装是为了 stats 结构一致；如需纯云端可设 --engine gemini。
        return FallbackEngine(primary=_gemini(), fallback_factory=_local)
    return _local()


class Pipeline:
    # 录制结束后等待 ASR 排空队列的最长时间（秒）。
    # 给足够余量：2.4 小时直播遇到网关抖动时，落后 3-5 分钟是可能的。
    drain_timeout: int = 900

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
            out_rate=cfg.audio.sample_rate,
        )
        # 重采样已在采集端（消费侧）完成，这里只是拿 recorder 的实际输出率
        # 构造一遍，相等时 StreamResampler 走 passthrough —— 保证只重采样一次。
        # 若两边相等却仍按 device.rate 构造，会把 16k 当 48k 再转一遍：
        # 时长变 1/3、频率变 3 倍，ASR 输出全是幻觉。
        self.resampler = StreamResampler(
            in_rate=self.recorder.out_rate, out_rate=cfg.audio.sample_rate
        )
        # 引擎在这里构造，支持本地/云端/自动降级三种模式
        self.transcriber = build_engine(cfg)

        from .config import RECORDINGS_DIR, TRANSCRIPTS_DIR

        # 产物（json/jsonl/md）写 TRANSCRIPTS_DIR，音频写 RECORDINGS_DIR。
        # 之前这里误传 RECORDINGS_DIR，导致逐字稿跟 WAV 混在 recordings/ 下，
        # 而 gui 按 TRANSCRIPTS_DIR 拼路径去打开，必然找不到文件。
        self.session = Session(
            TRANSCRIPTS_DIR, name=cfg.session_name,
            sample_rate=self.recorder.out_rate,
            audio_dir=RECORDINGS_DIR,
        )

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

        stop_reason = "用户停止"
        incomplete = False
        try:
            while not self._stop.is_set():
                if max_seconds and (time.time() - t_start) >= max_seconds:
                    print(f"\n[main] 达到设定时长 {max_seconds}s，停止。")
                    stop_reason = f"达到设定时长 {max_seconds}s"
                    break

                chunk = self.recorder.read(timeout=0.5)
                if chunk is None:  # 采集端已结束
                    stop_reason = self.recorder.stop_reason or "采集端已结束"
                    break
                if len(chunk) == 0:
                    continue

                n_chunks += 1
                # 音频立即落盘：即使 ASR 排不上队，声音不会丢。
                # chunk 已经是 recorder.out_rate（默认 16k），与 Session 一致。
                self.session.write_audio(chunk)

                # 兜底：recorder.out_rate 与目标率不同时这里才真正转换，
                # 相同时是 passthrough（不会二次重采样）。
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
                    qsize = self._asr_queue.qsize()
                    print(
                        f"\r[status] 已录 {el/60:.1f}min | 块 {n_chunks} | "
                        f"片段 {n_segments} | 已出句 {len(self.session.segments)} | "
                        f"RTF {rtf:.2f} | 队列 {qsize}",
                        end="", flush=True,
                    )
                    # 队列持续堆积 = 转写已经跟不上录制。不能等收尾才暴露：
                    # 2026-11-17 实测网关 RTF 从 0.35 飙到 3.8 时，队列从 0 堆到 9，
                    # 日志里只有数字变化，没有任何"你在落后"的提示。
                    if qsize >= 5:
                        print(f"\n[main][warn] 转写落后：队列已积压 {qsize} 个片段，"
                              f"当前 RTF {rtf:.2f}（>1 表示比实时还慢）。"
                              f"若持续下去，录制结束时可能还要等较久才能出完整稿。",
                              flush=True)
                    last_report = now

        except KeyboardInterrupt:
            print("\n[main] 收到中断信号，正在收尾…")
        finally:
            # 收尾顺序很重要：先停采集（此后不会再有新块）→ 补写重采样器
            # 冲出的尾部 → 再 flush 残留 → 最后等 ASR 队列排空
            self.recorder.stop(reason=stop_reason)
            # stop() 会 flush 重采样器，把最后几十毫秒挂到 read() 的 _final 上；
            # 主循环已经退出，这里主动 drain 出来补进 WAV，否则结尾会丢一截。
            while True:
                c = self.recorder.read(timeout=0.0)
                if c is None:
                    break
                if len(c):
                    self.session.write_audio(c)
                    for seg in self.segmenter.feed(self.resampler.process(c)):
                        n_segments += 1
                        self._enqueue_tail(seg, "收尾 drain")

            leftover = self.resampler.flush()
            if len(leftover):
                for seg in self.segmenter.feed(leftover):
                    self._enqueue_tail(seg, "重采样器残余")

            tail = self.segmenter.flush()
            if tail:
                self._enqueue_tail(tail, "切分器残余")

            self.recorder.stop()
            self._asr_queue.put(None)

            # 录制时长到了不等于活儿干完了：队列里可能还压着几分钟音频。
            # 2026-11-17 实测：10 分钟录制里网关两次 RTF 飙到 3.2/3.8，
            # 收尾时队列堆到 9 个片段，进程在收尾期间被外部超时截断，
            # 结果 json 没生成、转写比音频少 187 秒，而产物看起来"正常"。
            print(f"\n[main] 录制已结束，等待剩余转写"
                  f"（队列 {self._asr_queue.qsize()} 个片段）…")
            if self._asr_thread is not None:
                self._asr_thread.join(timeout=self.drain_timeout)
                if self._asr_thread.is_alive():
                    # 不能静默继续：否则产物会"看起来正常但少了尾部"。
                    incomplete = True
                    left = max(0, self._asr_queue.qsize())
                    print(f"[main][warn] 收尾超时（{self.drain_timeout}s），"
                          f"仍有 {left} 个片段留在队列里。"
                          f"音频已完整保存，可用 wxlive transcribe 补转。",
                          flush=True)

        doc = self.session.close(extra_meta={
            "asr": self.transcriber.stats,
            # 停止原因进产物：以后想知道"这场为什么断在这"，查 meta 即可，
            # 不必再对着 WAV 尾部的静音时长猜（2026-11-16/17 都这么猜过）。
            "stop_reason": stop_reason,
            "dropped_chunks": getattr(self.recorder, "dropped_chunks", 0),
            # 收尾是否跑完。False 表示产物可能缺尾部，需要人工补转。
            "transcript_complete": not incomplete,
        })
        print(f"\n完成。逐字稿: {self.session.md_path}")
        print(f"      结构化: {self.session.json_path}")
        print(f"      音频  : {self.session.audio_path}")
        if incomplete:
            # 结论必须显眼：否则用户会拿着缺尾部的产物当完整稿用。
            print("\n[!] 注意：本次转写未跑完（收尾超时）。")
            print("    音频是完整的，缺的是后半段的文字。补转办法：")
            print(f"    .venv-trim/Scripts/python.exe -m wxlive transcribe "
                  f"\"{self.session.audio_path}\"")
        return doc

    def _enqueue_tail(self, seg, where: str) -> None:
        """收尾阶段投递片段到 ASR 队列。

        为什么单独抽出来：收尾（drain / resampler.flush / segmenter.flush）
        投递的是整场**最后**的音频，恰恰最可能含结论。队列满时不能像主循环那样
        静默 pass —— 那会让最后几十秒内容凭空消失且无任何痕迹。
        这里改为：等待 → 仍失败则落盘到丢失清单，并在日志里明确报出来。
        """
        try:
            self._asr_queue.put(seg, timeout=5)
            return
        except queue.Full:
            pass
        # 队列满且 5 秒内没腾出空间：不能丢，先把这一段的音频存成独立 WAV，
        # 事后至少可以手工补转，不会"消失得无声无息"。
        try:
            self._spill_audio(seg, where)
        except Exception as e:  # noqa: BLE001
            print(f"[asr][error] 收尾片段落盘也失败（{where}）: {e}", flush=True)

    def _spill_audio(self, seg, where: str) -> None:
        """把未能入队的片段音频单独存盘，避免静默丢失。"""
        from .config import OUTPUT_DIR

        spill_dir = OUTPUT_DIR / "spilled"
        spill_dir.mkdir(parents=True, exist_ok=True)
        name = f"{self.session.id}_{seg.start_ts:.1f}s.wav"
        path = spill_dir / name
        import soundfile as sf

        sf.write(str(path), seg.audio, self.cfg.audio.sample_rate, subtype="PCM_16")
        print(f"[asr][warn] 转写队列满（{where}），该片段音频已另存: {path}",
              flush=True)

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
