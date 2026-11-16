"""基于能量的轻量 VAD + 片段切分。

为什么不用 Silero VAD：
- 目标是「直播录音」，语流连续、背景稳定，能量 VAD 已足够；
- 少一个 onnxruntime 依赖，装包快、体积小、CPU 占用低；
- 真正的语音精修交给 faster-whisper 内置的 Silero VAD 做二次过滤。

切分策略：
- 连续静音 >= min_silence 且累计语音 >= min_speech → 切出一个片段
- 累计语音 >= max_segment → 强制切分（长句/连续讲话不卡死）
"""

from __future__ import annotations

from dataclasses import dataclass

import numpy as np


@dataclass
class Segment:
    """一个待转写的语音片段。"""

    audio: np.ndarray
    start_ts: float  # 相对会话开始的秒数
    end_ts: float

    @property
    def duration(self) -> float:
        return self.end_ts - self.start_ts


class Segmenter:
    """把连续的音频块流切成语音片段。

    阈值是自适应的：前 ~3 秒当作环境底噪采样，语音阈值 = 底噪 × 倍数。
    理由：固定阈值在噪声大的环境（直播间有背景音乐）会全程判定为有声，
    导致整段不分句，ASR 只能对着 30s 的长音频瞎猜 —— 这是幻觉的主要来源。
    """

    def __init__(
        self,
        sample_rate: int,
        silence_rms: float = 0.004,
        min_silence_seconds: float = 0.7,
        min_speech_seconds: float = 0.6,
        max_segment_seconds: float = 20.0,
        # 切点时向前多保留一点，避免吃掉句首字
        pre_roll_seconds: float = 0.25,
        # 自适应阈值：相对底噪的倍数
        noise_multiplier: float = 3.5,
        # 自适应阈值下限/上限，防止极端环境失控
        adaptive_floor: float = 0.002,
        adaptive_ceil: float = 0.05,
        calibration_seconds: float = 3.0,
    ) -> None:
        self.sr = sample_rate
        self._fixed_threshold = silence_rms
        self.silence_rms = silence_rms  # 会被自适应校准覆盖
        self.min_silence = min_silence_seconds
        self.min_speech = min_speech_seconds
        self.max_segment = max_segment_seconds
        self.pre_roll = pre_roll_seconds
        self.noise_multiplier = noise_multiplier
        self.adaptive_floor = adaptive_floor
        self.adaptive_ceil = adaptive_ceil

        # 底噪校准（滑动窗口，持续自适应）
        self._calib_seconds = calibration_seconds
        self._calib_rms: list[float] = []
        self._calib_done = False
        self.noise_floor = 0.0
        self._auto_threshold = silence_rms
        # 滑动窗口：只保留最近 N 个块的 RMS。
        # 用低分位而非均值 —— 直播里背景音乐是持续的，均值会被音乐抬起来，
        # 10 分位更接近"真正安静的那几帧"，也就是底噪。
        self._rms_window: list[float] = []
        self._window_size = max(20, int(calibration_seconds / 0.1) * 3)
        # 校准期攒下的原始音频，校准一结束就补处理
        self._calib_pending: list[np.ndarray] = []

        self._buf: list[np.ndarray] = []
        self._buf_samples = 0
        self._voiced_samples = 0
        self._silence_run = 0.0  # 连续静音秒数
        self._seg_start = 0.0  # 当前片段起始时间戳
        self._clock = 0.0  # 已消费的总时长
        self._pre_roll_buf: list[np.ndarray] = []
        self._pre_roll_len = 0.0
        # 上次检测到有声的绝对时间点 + 有声采样累计（用于尾部静音裁剪）
        self._last_voiced_end = 0.0
        self._voiced_end_samples = 0  # 片段内"最后一个有声块"结束时的累计采样数
        # 尾部多留一点，避免削掉尾音的余韵（爆破音、拖长音）
        self.tail_pad_seconds = 0.15

    # ---------- 内部 ----------

    def _calibrate(self, rms: float, dur: float) -> None:
        """持续自适应底噪。

        踩过的坑：如果只在开头 3 秒采样，而开头正好在放背景音乐/开场音乐，
        底噪会被高估到 0.05，之后所有真实人声都被判定为静音 —— 整场录音不分句。
        直播场景必须有背景声（BGM、掌声），所以改为滑动窗口 + 低分位数，
        让它能跟着内容变化自动回落。
        """
        self._rms_window.append(rms)
        if len(self._rms_window) > self._window_size:
            del self._rms_window[: len(self._rms_window) - self._window_size]

        if not self._calib_done:
            self._calib_rms.append(rms)
            if len(self._calib_rms) * dur < self._calib_seconds:
                return
            self._calib_done = True

        # 低分位代表"安静的瞬间"，即真实底噪；高分位代表语音量级
        floor = float(np.percentile(self._rms_window, 15))
        peak = float(np.percentile(self._rms_window, 90))
        self.noise_floor = floor

        ratio = peak / floor if floor > 1e-9 else 1.0

        if ratio > 1.8:
            # 有声/无声有明显落差 → 切在落差下沿，能干净区分
            thr = floor + (peak - floor) * 0.22
        else:
            # 音量平坦：能量 VAD 本来就分不出语音与音乐。
            # 此时不能把阈值往上抬（会高于全部音频 → 永不切分），
            # 退回固定阈值，让 min_silence 永远不触发、由 max_segment 兜底切分。
            self.silence_rms = self._fixed_threshold
            return

        # 上限只用于防止极端值，但绝不能把阈值压到低于底噪
        new_thr = max(self.adaptive_floor, thr)
        new_thr = max(new_thr, floor * 1.2)  # 硬保护：阈值必须高于底噪 20%

        if abs(new_thr - self.silence_rms) > 0.004:
            print(f"[vad] 底噪={floor:.5f} 峰值={peak:.5f} → 语音阈值={new_thr:.5f}")
        self.silence_rms = new_thr

    def _preroll_keep(self) -> float:
        return self.pre_roll

    def _push_preroll(self, chunk: np.ndarray) -> None:
        """始终保持一小段"尾巴"，切点时拼到句首前面。"""
        self._pre_roll_buf.append(chunk)
        self._pre_roll_len += len(chunk) / self.sr
        while self._pre_roll_len > self._preroll_keep() and len(self._pre_roll_buf) > 1:
            dropped = self._pre_roll_buf.pop(0)
            self._pre_roll_len -= len(dropped) / self.sr

    def _emit(self, end_ts: float) -> Segment | None:
        if self._voiced_samples / self.sr < self.min_speech:
            self._reset_segment(end_ts)
            return None

        audio = np.concatenate(self._buf) if self._buf else np.zeros(0, dtype=np.float32)

        # 裁掉尾部静音：只保留到「最后一个有声块 + tail_pad」为止。
        # 理由：尾部静音对 ASR 无用，反而容易诱发 whisper 幻听（凭空生成句子）。
        keep = min(len(audio), self._voiced_end_samples + int(self.tail_pad_seconds * self.sr))
        if 0 < keep < len(audio):
            audio = audio[:keep]
            end_ts = self._seg_start + keep / self.sr

        seg = Segment(audio=audio, start_ts=self._seg_start, end_ts=end_ts)
        self._reset_segment(self._seg_start + len(audio) / self.sr)
        return seg

    def _reset_segment(self, now: float) -> None:
        self._buf = []
        self._buf_samples = 0
        self._voiced_samples = 0
        self._silence_run = 0.0
        self._seg_start = now
        self._voiced_end_samples = 0

    # ---------- 对外 ----------

    def feed(self, chunk: np.ndarray) -> list[Segment]:
        """喂入一个音频块，返回本次切出的完整片段（通常 0 或 1 个）。"""
        if len(chunk) == 0:
            return []

        out: list[Segment] = []
        dur = len(chunk) / self.sr
        rms = float(np.sqrt(np.mean(chunk**2))) if len(chunk) else 0.0

        # 校准期：只采样底噪，把音频原样存进待处理缓冲，不做语音判定。
        # 为什么不判：此时阈值还是初始值(0.004)，真实音频 RMS 普遍 >0.02，
        # 会被全部判成"有声"，开头整段粘成巨型片段 → ASR 合并、丢句、幻觉。
        # 校准一结束，把攒下的音频按正常流程重新喂一遍，内容不丢也不粘。
        if not self._calib_done:
            self._calib_pending.append(chunk)
            self._calibrate(rms, dur)
            if not self._calib_done:
                return out
            # 校准刚完成：回放缓冲的音频
            pending = self._calib_pending
            self._calib_pending = []
            for pc in pending:
                out.extend(self._feed_voiced(pc))
            return out

        return self._feed_voiced(chunk)

    def _feed_voiced(self, chunk: np.ndarray) -> list[Segment]:
        """校准完成后的正常切分流程。"""
        out: list[Segment] = []
        dur = len(chunk) / self.sr
        rms = float(np.sqrt(np.mean(chunk**2))) if len(chunk) else 0.0
        voiced = rms >= self.silence_rms

        # 片段还没开始，且当前静音 → 只更新 preroll 与时钟
        if not self._buf and not voiced:
            self._push_preroll(chunk)
            self._clock += dur
            self._seg_start = self._clock
            return out

        # 片段起始：把 preroll 拼进来
        if not self._buf:
            prefix = np.concatenate(self._pre_roll_buf) if self._pre_roll_buf else np.zeros(0, np.float32)
            self._seg_start = max(0.0, self._clock - len(prefix) / self.sr)
            if len(prefix):
                self._buf.append(prefix)
                self._buf_samples += len(prefix)
            self._pre_roll_buf = []
            self._pre_roll_len = 0.0

        self._buf.append(chunk)
        self._buf_samples += len(chunk)
        self._clock += dur

        if voiced:
            self._voiced_samples += len(chunk)
            self._silence_run = 0.0
            # 记录到本块结束为止的累计长度，用于裁剪尾部静音
            self._voiced_end_samples = self._buf_samples
        else:
            self._silence_run += dur

        # 条件 1：静音够久 → 断句
        if self._silence_run >= self.min_silence:
            seg = self._emit(self._clock)
            if seg:
                out.append(seg)
            return out

        # 条件 2：片段过长 → 必须切，但不要硬切在半个字上。
        # 在缓冲区尾部 30% 范围内找能量最低的位置切，尽量落在字间空隙。
        if self._buf_samples / self.sr >= self.max_segment:
            seg = self._emit_at_best_gap()
            if seg:
                out.append(seg)
            return out

        return out

    def _emit_at_best_gap(self) -> Segment | None:
        """超长片段：在尾部找一个"最像停顿"的位置切分，避免切断语义。"""
        if not self._buf:
            return None
        audio = np.concatenate(self._buf)
        total = len(audio)

        # 只在尾部 30% 搜索切点，保证这次至少切掉 70% 的内容（不陷入死循环）
        lo = int(total * 0.70)
        search = audio[lo:]
        if len(search) < self.sr * 0.2:  # 太短就直接切
            return self._emit(self._clock)

        # 5ms 帧能量，找最低点作为切点
        frame = max(1, int(self.sr * 0.005))
        n = len(search) // frame
        if n < 2:
            return self._emit(self._clock)
        frames = search[: n * frame].reshape(n, frame)
        energy = np.sqrt(np.mean(frames**2, axis=1))
        cut = lo + int(np.argmin(energy)) * frame

        head = audio[:cut]
        if len(head) / self.sr < self.min_speech:
            return self._emit(self._clock)

        seg = Segment(audio=head, start_ts=self._seg_start,
                      end_ts=self._seg_start + len(head) / self.sr)

        # 剩余部分作为新片段的开头，时间戳接续
        remain = audio[cut:]
        consumed = self._seg_start + len(head) / self.sr
        self._buf = [remain] if len(remain) else []
        self._buf_samples = len(remain)
        self._voiced_samples = len(remain)  # 保守估计：剩余部分当作有声
        self._voiced_end_samples = len(remain)
        self._silence_run = 0.0
        self._seg_start = consumed
        return seg

    def flush(self) -> Segment | None:
        """会话结束时，把残留的片段吐出来。"""
        # 极短会话可能还没走完校准期，把这部分音频补进正常流程
        if self._calib_pending:
            pending = self._calib_pending
            self._calib_pending = []
            if not self._calib_done:
                # 强制结束校准：用已采样本直接定阈值，避免整段被丢弃
                if self._rms_window:
                    self._calib_done = True
                    self._calibrate(0.0, 0.0)
            for pc in pending:
                self._feed_voiced(pc)

        if not self._buf:
            return None
        # 结尾往往没有静音，这里不做 min_speech 过滤，只要有内容就保留
        audio = np.concatenate(self._buf)
        keep = min(len(audio), self._voiced_end_samples + int(self.tail_pad_seconds * self.sr))
        if 0 < keep < len(audio):
            audio = audio[:keep]
        end_ts = self._seg_start + len(audio) / self.sr
        seg = Segment(audio=audio, start_ts=self._seg_start, end_ts=end_ts)
        self._reset_segment(self._clock)
        return seg if seg.duration > 0.05 else None
