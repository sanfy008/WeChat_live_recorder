"""直播静音监控：边录边检测，判定"直播已结束"。

## 为什么不能用 silence.detect_silence

那个模块是**离线**判据：一次拿到整段音频再做统计（百分位求底噪）。
直播场景音频是持续增长的，等它读完就等于永不结束。

所以要一个**增量**版本：
- 每次只读文件末尾最近一段（滑动窗口）
- 用绝对阈值判断静音（不需要全局百分位）
- 连续静音累计到阈值就判定直播结束

## 判据怎么定（这是最关键的部分）

本地 GUI 用的是"块 RMS < 0.002 累计 40 秒"，实测 2026-11-17 那场直播
末尾正好 40.0 秒静音，与设定完全一致。这里沿用同一套，但做了三点加固：

1. **绝对阈值而非相对阈值**：直播底噪会变（BGM、空调、音效），
   相对阈值在"整场都安静"时会把正常说话也判成静音。
   0.002 是本地实测出来的值（16k 归一化 float 的 RMS）。
2. **连续计数要能被打断清零**：主播咳嗽一声、翻页声都会让 RMS 跳一下。
   只要有一个块不算静音，累计立刻归零 —— 否则会把"零星响声的长时间静音"
   误判成"还在播"。
3. **首段宽限**：开播初期可能没声音（主播在调设备），
   如果一开始就计数，容易误杀。所以要求先见过"有声音"才开始计时。

## 为什么还要"见过声音"

`require_sound_before` 这个开关很关键：
一个流如果从头到尾都是静音（地址错了、音轨是空的、被静音推流），
不该傻等 40 秒后"正常收工"并去转写空文件 —— 那应该报错。
所以默认要求先观察到有效声音，才允许触发静音停止。
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

import numpy as np

# 本地 GUI 实测值：16k mono 归一化 float 的 RMS 低于它算静音
DEFAULT_SILENCE_RMS = 0.002


@dataclass
class SilenceVerdict:
    """一次检测的结论。"""

    checked: bool = False          # 这一轮是否真的分析了音频
    rms: float = 0.0               # 最近窗口的平均 RMS
    silent_seconds: float = 0.0    # 连续静音累计秒数
    heard_sound: bool = False      # 是否听到过有效声音
    should_stop: bool = False      # 是否判定直播结束
    reason: str = ""               # 人读原因，写进日志


def _parse_wav_header(head: bytes) -> dict | None:
    """解析 RIFF/WAVE 头部，返回 {sr, channels, width, data_offset}。

    为什么不能用标准库 `wave`：
    录制中的 WAV 头部 `data` 长度字段是**占位值**（常见 0 或 0x7FFFFFFF），
    而 `wave.Wave_read.setpos()` 会按头里的 `nframes` 做边界检查 ——
    头部写 0 时 `setpos` 直接失败，读不到任何数据。
    实测：文件实际有 256KB 数据，头部 nframes=0 → wave 拒绝 seek。

    所以这里自己走一遍 chunk 链，只取真正需要的几项，
    数据起点用 chunk 偏移算，长度用**文件实际大小**决定。
    """
    if len(head) < 12 or head[:4] != b"RIFF" or head[8:12] != b"WAVE":
        return None

    pos = 12
    fmt: dict | None = None
    data_offset = -1
    data_declared = 0

    while pos + 8 <= len(head):
        chunk_id = head[pos:pos + 4]
        try:
            chunk_size = int.from_bytes(head[pos + 4:pos + 8], "little")
        except ValueError:
            return None

        body = pos + 8
        if chunk_id == b"fmt " and body + 16 <= len(head):
            # fmt chunk 布局（每项字节偏移）：
            #   0 audioFormat(2) | 2 channels(2) | 4 sampleRate(4) |
            #   8 byteRate(4) | 12 blockAlign(2) | 14 bitsPerSample(2)
            audio_format = int.from_bytes(head[body:body + 2], "little")
            channels = int.from_bytes(head[body + 2:body + 4], "little")
            sr = int.from_bytes(head[body + 4:body + 8], "little")
            byte_rate = int.from_bytes(head[body + 8:body + 12], "little")
            block_align = int.from_bytes(head[body + 12:body + 14], "little")
            bits = int.from_bytes(head[body + 14:body + 16], "little")
            # bitsPerSample 是"位"，要换成"字节"；块对齐更可靠时优先用它
            if block_align > 0 and channels > 0:
                width = block_align // channels
            elif bits > 0:
                width = max(1, bits // 8)
            elif byte_rate > 0 and sr > 0 and channels > 0:
                width = byte_rate // (sr * channels)
            else:
                width = 0
            # audio_format: 1=PCM, 3=IEEE float, 0xFFFE=extensible
            fmt = {
                "format": audio_format,
                "channels": channels,
                "sr": sr,
                "width": width,
            }
        elif chunk_id == b"data":
            data_offset = body
            data_declared = chunk_size
            break

        # chunk 按偶数字节对齐
        pos = body + chunk_size + (chunk_size & 1)

    if fmt is None or data_offset < 0:
        return None
    if fmt["sr"] <= 0 or fmt["channels"] <= 0 or fmt["width"] <= 0:
        return None

    fmt["data_offset"] = data_offset
    fmt["data_declared"] = data_declared
    return fmt


def read_tail_mono(path: Path | str, max_seconds: float = 3.0) -> tuple[np.ndarray, int]:
    """读 WAV 文件末尾最多 max_seconds 的音频（单声道 float32）。

    用实际文件大小反推可用数据长度，忽略头部声明值 —— 见 `_parse_wav_header`。

    返回 (音频, 采样率)；文件不可读或为空时返回 (空数组, 0)。

    注意：只看返回值无法得知这段音频在文件里的**位置**，
    增量检测必须用 `read_tail_window()`（它额外返回起始秒）。
    """
    audio, sr, _start = read_tail_window(path, max_seconds=max_seconds)
    return audio, sr


def read_tail_window(
    path: Path | str, max_seconds: float = 3.0
) -> tuple[np.ndarray, int, float]:
    """读 WAV 末尾一段，同时返回该段在文件中的**起始秒位置**。

    起始位置是增量静音检测的前提：监控器靠它判断"这次读到的窗口里
    有多少是新增的、有多少是上一轮已经算过的重叠部分"。
    不返回位置就会重复计数（实测虚高 30%）。
    """
    audio, sr, start = _read_tail_impl(path, max_seconds)
    return audio, sr, start


def _read_tail_impl(
    path: Path | str, max_seconds: float
) -> tuple[np.ndarray, int, float]:
    """读 WAV 末尾一段，返回 (音频, 采样率, 该段起始秒)。

    起始秒必须返回：增量静音检测要靠它算"这次有多少新增"，
    否则窗口重叠部分会被重复计数。
    """
    empty = (np.zeros(0, dtype=np.float32), 0, 0.0)

    p = Path(path)
    try:
        file_size = p.stat().st_size
    except OSError:
        return empty
    if file_size <= 44:
        return empty

    try:
        with p.open("rb") as fh:
            head = fh.read(min(4096, file_size))
            info = _parse_wav_header(head)
            if info is None:
                return empty

            sr = int(info["sr"])
            channels = int(info["channels"])
            width = int(info["width"])
            data_offset = int(info["data_offset"])
            frame_bytes = channels * width
            if frame_bytes <= 0:
                return empty

            # 实际可用数据长度（不信任头部声明）
            available = file_size - data_offset
            if available <= 0:
                return empty
            total_frames = available // frame_bytes
            if total_frames <= 0:
                return empty

            want = min(total_frames, int(sr * max_seconds))
            if want <= 0:
                return empty
            first_frame = total_frames - want
            start_sec = first_frame / float(sr)
            fh.seek(data_offset + first_frame * frame_bytes)
            raw = fh.read(want * frame_bytes)
    except (OSError, EOFError, ValueError):
        return empty

    if not raw:
        return empty

    raw = raw[: (len(raw) // frame_bytes) * frame_bytes]
    if info["format"] == 3 and width == 4:
        a = np.frombuffer(raw, dtype="<f4").astype(np.float32)
    elif width == 2:
        a = np.frombuffer(raw, dtype="<i2").astype(np.float32) / 32768.0
    elif width == 4:
        a = np.frombuffer(raw, dtype="<i4").astype(np.float32) / 2147483648.0
    elif width == 1:
        a = (np.frombuffer(raw, dtype=np.uint8).astype(np.float32) - 128.0) / 128.0
    elif width == 3:
        b = np.frombuffer(raw, dtype=np.uint8).reshape(-1, 3).astype(np.int32)
        v = b[:, 0] | (b[:, 1] << 8) | (b[:, 2] << 16)
        v = np.where(v & 0x800000, v - 0x1000000, v)
        a = (v.astype(np.float32) / 8388608.0)
    else:
        return empty

    if channels > 1:
        usable = (a.size // channels) * channels
        if usable <= 0:
            return empty
        a = a[:usable].reshape(-1, channels).mean(axis=1)
    return a, sr, start_sec


class LiveSilenceMonitor:
    """增量静音累计器。

    ## 关键：必须按"新增了多少字节"推进，不能按"读到的窗口长度"

    外层是"每 N 秒读文件末尾 M 秒（M > N）"。如果直接把读到的窗口整段计入，
    重叠部分会被**重复计算**，累计值虚高 30%（实测：真实静音 33 秒被判成 43 秒）。

    还有一种更隐蔽的情况：ffmpeg 卡住不再写文件时，`read_tail_mono`
    每次返回**同一段旧音频**。若照样计入，真实静音 45 秒也能被累加到 100+ 秒
    （上限取决于轮询次数而不是真实时长），判停时机完全失控。

    所以这里维护"上次处理到的绝对字节偏移"，每次只分析**新增部分**：
      - 文件没长 → 没有新增 → 不计入（但也不清零，静音状态保持）
      - 文件长了 → 只分析新增的那一段

    `poll()` 因此需要一个 `audio_offset`：该段音频在文件中的起始字节位置。
    不传时退化为"整段计入"（兼容测试与一次性调用），并给出警告标记。
    """

    def __init__(
        self,
        silence_rms: float = DEFAULT_SILENCE_RMS,
        stop_after_seconds: float = 40.0,
        block_seconds: float = 0.5,
        require_sound_before: bool = True,
    ) -> None:
        self.silence_rms = float(silence_rms)
        self.stop_after_seconds = float(stop_after_seconds)
        self.block_seconds = max(0.1, float(block_seconds))
        self.require_sound_before = bool(require_sound_before)
        self.silent_seconds = 0.0
        self.heard_sound = False
        self._last_rms = 0.0
        # 上次分析到的绝对位置（以"传入音频的起始偏移 + 已消费长度"计）
        self._consumed_end: float | None = None

    def poll(
        self,
        audio: np.ndarray,
        sample_rate: int,
        *,
        audio_offset: float | None = None,
    ) -> SilenceVerdict:
        """分析一段音频。

        Args:
            audio: 待分析音频（单声道 float）
            sample_rate: 采样率
            audio_offset: 这段音频在文件里的起始**秒**位置。
                传入时只分析"上次之后的新增部分"；不传则整段计入。
        """
        if sample_rate <= 0 or audio is None or audio.size == 0:
            return SilenceVerdict(
                checked=False,
                silent_seconds=self.silent_seconds,
                heard_sound=self.heard_sound,
            )

        block = max(1, int(sample_rate * self.block_seconds))
        total_dur = audio.size / float(sample_rate)

        # 计算本次真正要分析的那一段
        start_sec = 0.0
        if audio_offset is not None:
            seg_start = float(audio_offset)
            seg_end = seg_start + total_dur
            if self._consumed_end is not None:
                if seg_end <= self._consumed_end + 1e-6:
                    # 文件没长（或窗口完全落在已消费区）→ 没有新内容
                    return SilenceVerdict(
                        checked=False,
                        rms=round(self._last_rms, 5),
                        silent_seconds=round(self.silent_seconds, 1),
                        heard_sound=self.heard_sound,
                        reason="文件未增长，无新增音频",
                    )
                if seg_start < self._consumed_end:
                    start_sec = self._consumed_end - seg_start
            self._consumed_end = seg_end
        else:
            # 未提供偏移：按整段计入（旧行为）
            self._consumed_end = (self._consumed_end or 0.0) + total_dur

        start_idx = int(start_sec * sample_rate)
        audio = audio[start_idx:]
        n = audio.size // block
        if n == 0:
            # 新增不足一个块：**必须把 _consumed_end 退回去**，否则这段音频
            # 被标记为"已消费"却从未分析，永久丢失（对立性审查 M1）。
            # 影响是 40 秒阈值系统性偏松（实测每轮最多漏 0.5 秒）。
            if audio_offset is not None and self._consumed_end is not None:
                self._consumed_end -= audio.size / float(sample_rate)
            return SilenceVerdict(
                checked=False,
                rms=round(self._last_rms, 5),
                silent_seconds=round(self.silent_seconds, 1),
                heard_sound=self.heard_sound,
                reason="新增不足一个块，留待下次",
            )

        verdict = SilenceVerdict(checked=True)
        rms_vals: list[float] = []
        for i in range(n):
            seg = audio[i * block:(i + 1) * block]
            rms = float(np.sqrt(np.mean(seg.astype(np.float64) ** 2)))
            rms_vals.append(rms)
            if rms >= self.silence_rms:
                # 有任何一块不静音 → 听到过声音，且连续计数清零
                self.heard_sound = True
                self.silent_seconds = 0.0
            else:
                self.silent_seconds += self.block_seconds

        # 不足一个块的尾巴不能丢：它已经算进 consumed_end 了，
        # 但要把它从累计里"退回"——否则下一轮不会重复分析，会永久漏掉。
        # 做法：只把整块部分算作已消费。
        if audio_offset is not None and self._consumed_end is not None:
            leftover = (audio.size - n * block) / float(sample_rate)
            if leftover > 0:
                self._consumed_end -= leftover

        self._last_rms = float(np.mean(rms_vals)) if rms_vals else 0.0
        verdict.rms = round(self._last_rms, 5)
        verdict.silent_seconds = round(self.silent_seconds, 1)
        verdict.heard_sound = self.heard_sound

        if not self.heard_sound and self.require_sound_before:
            verdict.reason = "尚未检测到有效声音，继续等待"
            return verdict

        if self.silent_seconds >= self.stop_after_seconds:
            verdict.should_stop = True
            verdict.reason = (
                f"连续静音 {self.silent_seconds:.0f} 秒"
                f"（阈值 {self.stop_after_seconds:.0f} 秒），判定直播已结束"
            )
        return verdict

    def reset(self) -> None:
        self.silent_seconds = 0.0
        self.heard_sound = False
        self._last_rms = 0.0
        self._consumed_end = None
