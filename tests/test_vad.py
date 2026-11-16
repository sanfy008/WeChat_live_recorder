"""VAD 切分逻辑的单元测试（不依赖音频设备，可离线跑）。"""

from __future__ import annotations

import numpy as np
import pytest

from wxlive.vad import Segmenter

SR = 16000


def tone(sec: float, amp: float = 0.3, freq: float = 300.0) -> np.ndarray:
    t = np.arange(int(SR * sec)) / SR
    return (amp * np.sin(2 * np.pi * freq * t)).astype(np.float32)


def silence(sec: float) -> np.ndarray:
    return np.zeros(int(SR * sec), dtype=np.float32)


def feed_all(seg: Segmenter, audio: np.ndarray, chunk_sec: float = 0.1) -> list:
    out = []
    n = int(SR * chunk_sec)
    for i in range(0, len(audio), n):
        out.extend(seg.feed(audio[i : i + n]))
    return out


def test_silence_produces_no_segment():
    seg = Segmenter(SR)
    assert feed_all(seg, silence(5.0)) == []


def test_single_utterance_then_silence_emits_one_segment():
    seg = Segmenter(SR, min_silence_seconds=0.8, min_speech_seconds=0.6)
    audio = np.concatenate([silence(0.5), tone(2.0), silence(1.5)])
    segs = feed_all(seg, audio)
    assert len(segs) == 1
    # 语音 2s + preroll，不包含后面 1.5s 的静音
    assert 1.9 <= segs[0].duration <= 3.0


def test_short_blip_is_dropped():
    seg = Segmenter(SR, min_speech_seconds=0.6, min_silence_seconds=0.5)
    audio = np.concatenate([tone(0.2), silence(2.0)])
    assert feed_all(seg, audio) == []


def test_two_utterances_split_by_pause():
    seg = Segmenter(SR, min_silence_seconds=0.8, min_speech_seconds=0.6)
    audio = np.concatenate([tone(1.5), silence(2.0), tone(1.5), silence(2.0)])
    segs = feed_all(seg, audio)
    assert len(segs) == 2


def test_max_segment_forces_split():
    """连续讲话超过上限时必须强制切分，不能无限累积内存。"""
    seg = Segmenter(SR, max_segment_seconds=3.0, min_speech_seconds=0.5)
    segs = feed_all(seg, tone(10.0))  # 全程有声，无静音
    assert len(segs) >= 3
    for s in segs:
        assert s.duration <= 3.5


def test_timestamps_are_monotonic_and_cover_audio():
    seg = Segmenter(SR, min_silence_seconds=0.6, min_speech_seconds=0.4)
    audio = np.concatenate([tone(1.0), silence(1.0), tone(1.0), silence(1.0), tone(1.0)])
    segs = feed_all(seg, audio)
    assert len(segs) >= 2
    prev_end = -1.0
    for s in segs:
        assert s.start_ts >= prev_end - 1e-6, "时间戳出现倒序"
        assert s.end_ts > s.start_ts
        prev_end = s.end_ts


def test_flush_returns_tail_without_trailing_silence():
    seg = Segmenter(SR)
    audio = np.concatenate([silence(0.3), tone(1.0)])  # 结尾没有静音
    feed_all(seg, audio)
    tail = seg.flush()
    assert tail is not None
    assert tail.duration > 0.5


def test_preroll_keeps_sentence_head():
    """切点前应保留一点音频，避免吃掉句首辅音。"""
    seg = Segmenter(SR, min_silence_seconds=0.5, min_speech_seconds=0.3, pre_roll_seconds=0.25)
    audio = np.concatenate([silence(1.0), tone(1.0), silence(1.0)])
    segs = feed_all(seg, audio)
    assert len(segs) == 1
    # 起点应早于纯语音开始位置（1.0s），说明 preroll 生效
    assert segs[0].start_ts < 1.0
