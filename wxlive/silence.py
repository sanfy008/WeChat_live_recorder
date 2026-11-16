"""离线静音检测与剔除。

场景：长录音（1-2.5 小时）里存在"什么都没发生"的时段——主播离开、
喝水、切场景、设备静默。这些段不需要转写，剔除能省时间和成本。

**关键设计原则（King 明确要求）**：
    念评论不算静音。

直播间里主播念家长留言、读弹幕，音量可能不大但**是有效内容**，
绝不能切。所以本模块只认"接近数字静默 + 持续足够久"的区间：

1. 阈值取**绝对低值**（相对本文件底噪自适应，但设上限），
   宁少切不多切。宁可漏掉一点静音，也不误删一句评论。
2. 只有**连续长时间**低于阈值才算静音（默认 ≥8 秒）。
   念评论时的自然停顿通常 1-3 秒，切不到。
3. 段落边界前后留 **padding**（默认 0.5 秒），避免切掉话音头尾。
4. 剔除后**保留时间映射**，转写出的时间戳必须能还原到原始时间轴，
   否则 LLM 分析时对不上进度条。

用法：
    regions = detect_silence(audio, sr)          # [(start_sec, end_sec), ...]
    kept, mapping = cut_silence(audio, sr, regions)
    # mapping: [(kept_start, original_start, length), ...]
"""

from __future__ import annotations

from dataclasses import dataclass

import numpy as np


@dataclass
class SilenceRegion:
    """一段可剔除的静音。"""

    start: float          # 秒
    end: float

    @property
    def duration(self) -> float:
        return self.end - self.start


def _frame_rms(audio: np.ndarray, frame: int, hop: int) -> np.ndarray:
    """按 hop 步长滑动求 RMS。"""
    if len(audio) < frame:
        return np.array([float(np.sqrt(np.mean(audio ** 2)))]) if len(audio) else np.array([])
    n = 1 + (len(audio) - frame) // hop
    idx = np.arange(frame)[None, :] + hop * np.arange(n)[:, None]
    frames = audio[idx]
    return np.sqrt(np.mean(frames.astype(np.float64) ** 2, axis=1))


def detect_silence(
    audio: np.ndarray,
    sample_rate: int,
    *,
    frame_sec: float = 0.5,
    min_silence_sec: float = 8.0,
    pad_sec: float = 0.5,
    rel_threshold: float = 3.0,
    abs_floor: float = 0.0008,
    abs_cap: float = 0.006,
) -> list[SilenceRegion]:
    """找出可安全剔除的静音区间。

    Args:
        audio: 单声道 float32，取值约 [-1, 1]
        sample_rate: 采样率
        frame_sec: 分析帧长（秒）。0.5s 足够，太短会被单个爆破音干扰
        min_silence_sec: **最短静音时长**。这条是防误杀的核心——
            念评论/说话的自然停顿一般 1-3 秒，设 8 秒就切不到
        pad_sec: 区段两侧保留的余量，避免切掉话头话尾
        rel_threshold: 阈值 = 底噪 × 该倍数
        abs_floor / abs_cap: 阈值的上下限。上限很重要：
            直播间常有底噪，不设上限会让阈值飘高，把轻声说话切掉

    Returns:
        按时间排序的静音区间列表（已含 padding 收缩）
    """
    if audio.size == 0:
        return []

    frame = max(1, int(frame_sec * sample_rate))
    hop = frame
    rms = _frame_rms(np.asarray(audio, dtype=np.float32), frame, hop)
    if rms.size == 0:
        return []

    # 底噪估计：取最安静的 10%
    floor = float(np.percentile(rms, 10))
    thr = floor * rel_threshold
    # 上下限夹紧：上限防止底噪抬高阈值误杀轻声；下限防止数字静默时阈值过低
    thr = min(max(thr, max(floor * 1.2, abs_floor)), abs_cap)

    quiet = rms < thr

    regions: list[SilenceRegion] = []
    i = 0
    n = quiet.size
    min_frames = int(round(min_silence_sec / frame_sec))
    while i < n:
        if not quiet[i]:
            i += 1
            continue
        j = i
        while j < n and quiet[j]:
            j += 1
        if (j - i) >= min_frames:
            start = i * frame_sec
            end = min(j * frame_sec, len(audio) / sample_rate)
            # 两侧各让出 padding，避免切掉相邻语音的头尾
            s2 = start + pad_sec
            e2 = end - pad_sec
            if e2 - s2 >= min_silence_sec * 0.5:
                regions.append(SilenceRegion(s2, e2))
        i = j
    return regions


def cut_silence(
    audio: np.ndarray,
    sample_rate: int,
    regions: list[SilenceRegion],
) -> tuple[np.ndarray, list[tuple[float, float, float]]]:
    """把静音区间从音频里去掉，同时返回时间映射。

    Returns:
        (保留的音频, mapping)
        mapping 每项 (kept_start_sec, original_start_sec, length_sec)：
        说明"保留音频中从 kept_start 开始的这段，原本位于 original_start，
        长度 length"。转写完成后据此把相对时间还原到原始时间轴。
    """
    if not regions:
        dur = len(audio) / sample_rate if sample_rate else 0.0
        return audio, [(0.0, 0.0, dur)]

    total = len(audio) / sample_rate
    keep: list[tuple[float, float]] = []
    cur = 0.0
    for r in sorted(regions, key=lambda x: x.start):
        s = max(0.0, min(r.start, total))
        e = max(0.0, min(r.end, total))
        if e <= s:
            continue
        if s > cur:
            keep.append((cur, s))
        cur = max(cur, e)
    if cur < total:
        keep.append((cur, total))

    if not keep:
        return audio[:0], []

    pieces = []
    mapping: list[tuple[float, float, float]] = []
    kept_pos = 0.0
    for s, e in keep:
        i0 = int(s * sample_rate)
        i1 = int(e * sample_rate)
        seg = audio[i0:i1]
        if seg.size == 0:
            continue
        pieces.append(seg)
        length = seg.size / sample_rate
        mapping.append((kept_pos, s, length))
        kept_pos += length

    out = np.concatenate(pieces) if pieces else audio[:0]
    return out, mapping


def map_back(t_in_kept: float, mapping: list[tuple[float, float, float]]) -> float:
    """把"剔除后音频"里的时间，还原成原始音频的时间。"""
    if not mapping:
        return t_in_kept
    for kept_start, orig_start, length in mapping:
        if t_in_kept <= kept_start + length:
            offset = max(0.0, t_in_kept - kept_start)
            return orig_start + offset
    # 超出末尾：按最后一段推
    kept_start, orig_start, length = mapping[-1]
    return orig_start + length


def summarize(regions: list[SilenceRegion], total_sec: float) -> str:
    """人读摘要，用于日志。"""
    if not regions:
        return "未发现可剔除的静音（念评论、轻声说话都算内容）"
    cut = sum(r.duration for r in regions)
    pct = cut / total_sec * 100 if total_sec else 0
    return (f"共 {len(regions)} 段静音，合计 {cut/60:.1f} 分钟 "
            f"（占 {pct:.1f}%），可跳过")
