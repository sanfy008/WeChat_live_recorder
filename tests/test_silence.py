"""静音检测与剔除的测试。

核心要保证的是**不误杀**：念评论、轻声说话都算内容，
不能被当成静音切掉。所以这里大量测试都是"不该切"的场景。
"""
import numpy as np
import pytest

from wxlive.silence import (
    SilenceRegion,
    detect_silence,
    cut_silence,
    map_back,
    summarize,
)

SR = 16000


def _tone(sec: float, amp: float = 0.15, sr: int = SR, freq: float = 220.0) -> np.ndarray:
    t = np.arange(int(sec * sr)) / sr
    return (amp * np.sin(2 * np.pi * freq * t)).astype(np.float32)


def _silence(sec: float, sr: int = SR, amp: float = 0.0005) -> np.ndarray:
    rng = np.random.default_rng(0)
    return (rng.standard_normal(int(sec * sr)) * amp).astype(np.float32)


# ---- 不该切的场景（最重要） ----

def test_short_pause_between_speech_is_kept():
    """说话中间 3 秒停顿不算静音（默认阈值 8 秒）。"""
    audio = np.concatenate([_tone(5.0), _silence(3.0), _tone(5.0)])
    regions = detect_silence(audio, SR)
    assert regions == [], f"误切了说话停顿: {regions}"


def test_quiet_speech_is_kept():
    """念评论时音量偏小（但不是数字静默），不能被切。

    直播间念家长留言音量比主播说话小很多，这是最常见的误切来源。
    """
    loud = _tone(6.0, amp=0.15)
    quiet_but_real = _tone(12.0, amp=0.012)   # 明显低于说话，但有明确信号
    audio = np.concatenate([loud, quiet_but_real, loud])
    regions = detect_silence(audio, SR)
    assert regions == [], f"把轻声说话当静音切了: {regions}"


def test_continuous_speech_no_silence():
    audio = _tone(60.0)
    assert detect_silence(audio, SR) == []


def test_empty_audio():
    assert detect_silence(np.array([], dtype=np.float32), SR) == []


def test_all_silence_still_detected():
    """整段都是静音时应该能识别出来（不是刻意不切）。"""
    audio = _silence(30.0)
    regions = detect_silence(audio, SR)
    assert len(regions) >= 1
    assert sum(r.duration for r in regions) > 10


# ---- 该切的场景 ----

def test_long_silence_is_detected():
    """超过 8 秒的真静音应被识别。"""
    audio = np.concatenate([_tone(3.0), _silence(20.0), _tone(3.0)])
    regions = detect_silence(audio, SR)
    assert len(regions) == 1
    assert regions[0].duration >= 8


def test_silence_at_start_and_end():
    audio = np.concatenate([_silence(15.0), _tone(20.0), _silence(15.0)])
    regions = detect_silence(audio, SR)
    assert len(regions) == 2


def test_padding_preserves_speech_edges():
    """静音两侧要留 padding，不能贴边切到语音。

    构造：语音 0~5s，静音 5~25s，语音 25~30s。
    静音区原始范围 5~25，留 1s padding 后应为 6~24 ——
    两侧各让出 1 秒，保证切完不会啃到相邻语音。
    """
    audio = np.concatenate([_tone(5.0), _silence(20.0), _tone(5.0)])
    regions = detect_silence(audio, SR, pad_sec=1.0)
    assert regions
    r = regions[0]
    # 起点必须晚于语音结束(5.0)，终点必须早于语音开始(25.0)
    assert r.start >= 5.0, f"起点切到语音了: {r.start}"
    assert r.end <= 25.0, f"终点切到语音了: {r.end}"
    # 且要真的让出了约 1 秒
    assert r.start - 5.0 >= 0.9
    assert 25.0 - r.end >= 0.9


# ---- 剔除与时间映射 ----

def test_cut_silence_shortens_audio():
    audio = np.concatenate([_tone(5.0), _silence(20.0), _tone(5.0)])
    regions = detect_silence(audio, SR)
    kept, mapping = cut_silence(audio, SR, regions)
    assert len(kept) < len(audio)
    assert mapping


def test_cut_silence_no_regions_is_identity():
    audio = _tone(10.0)
    kept, mapping = cut_silence(audio, SR, [])
    assert len(kept) == len(audio)
    assert mapping == [(0.0, 0.0, pytest.approx(10.0))]


def test_map_back_restores_original_time():
    """剔除后转写出的时间，必须能还原到原始时间轴。"""
    audio = np.concatenate([_tone(10.0), _silence(20.0), _tone(10.0)])
    regions = detect_silence(audio, SR)
    kept, mapping = cut_silence(audio, SR, regions)

    # 剔除后第 11 秒的内容，原本大约在第 31 秒（中间挖掉约 20 秒）
    t_kept = 11.0
    t_orig = map_back(t_kept, mapping)
    assert 29.0 <= t_orig <= 33.0, f"时间还原不准: {t_orig}"


def test_map_back_monotonic():
    """时间映射必须单调递增，否则拼出来的时间轴会乱。"""
    audio = np.concatenate([_tone(8.0), _silence(15.0), _tone(8.0), _silence(15.0), _tone(8.0)])
    regions = detect_silence(audio, SR)
    _, mapping = cut_silence(audio, SR, regions)
    kept_times = [0.0, 3.0, 8.0, 12.0, 16.0]
    orig = [map_back(t, mapping) for t in kept_times]
    assert all(b >= a for a, b in zip(orig, orig[1:])), f"时间轴非单调: {orig}"


def test_cut_all_silence_returns_empty():
    audio = _silence(30.0)
    regions = [SilenceRegion(0.0, 30.0)]
    kept, mapping = cut_silence(audio, SR, regions)
    assert len(kept) == 0


def test_overlapping_regions_are_merged():
    """重叠/相邻区间不能导致重复拼接。"""
    audio = _tone(30.0)
    regions = [SilenceRegion(5.0, 10.0), SilenceRegion(8.0, 14.0)]
    kept, mapping = cut_silence(audio, SR, regions)
    # 合并后只挖掉 5~14，应剩 21 秒
    assert abs(len(kept) / SR - 21.0) < 0.5


def test_summarize_messages():
    assert "未发现" in summarize([], 100.0)
    msg = summarize([SilenceRegion(0, 60), SilenceRegion(100, 160)], 600.0)
    assert "2 段" in msg and "2.0 分钟" in msg
