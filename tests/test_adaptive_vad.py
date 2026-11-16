"""自适应 VAD 阈值的回归测试。

背景：初版只在校准期算一次底噪，且阈值上限 0.05 会低于实际底噪 0.053，
结果是「阈值 < 底噪」→ 所有音频都判定为有声 → 永不切句 → 整场不分段。
这组测试专门锁死这几条不变量。
"""

from __future__ import annotations

import numpy as np

from wxlive.vad import Segmenter

SR = 16000


def tone(sec: float, amp: float, freq: float = 300.0) -> np.ndarray:
    t = np.arange(int(SR * sec)) / SR
    return (amp * np.sin(2 * np.pi * freq * t)).astype(np.float32)


def noise(sec: float, amp: float, seed: int = 0) -> np.ndarray:
    rng = np.random.default_rng(seed)
    return (rng.normal(0, amp, int(SR * sec))).astype(np.float32)


def feed(seg: Segmenter, audio: np.ndarray, chunk_sec: float = 0.1) -> list:
    out = []
    n = int(SR * chunk_sec)
    for i in range(0, len(audio), n):
        out.extend(seg.feed(audio[i : i + n]))
    return out


def test_threshold_never_below_noise_floor():
    """核心不变量：阈值必须严格高于底噪，否则永不切句。"""
    seg = Segmenter(SR)
    # 高底噪 + 明显人声，交替出现
    audio = np.concatenate([noise(2.0, 0.06), tone(2.0, 0.3), noise(2.0, 0.06)])
    feed(seg, audio)
    assert seg.noise_floor > 0, "应已测得底噪"
    assert seg.silence_rms > seg.noise_floor, (
        f"阈值 {seg.silence_rms:.5f} 未高于底噪 {seg.noise_floor:.5f}，会导致永不切句"
    )


def test_high_noise_environment_still_segments():
    """高底噪环境下，人声段仍必须被切出来。"""
    seg = Segmenter(SR, min_silence_seconds=0.7, min_speech_seconds=0.5)
    audio = np.concatenate(
        [
            noise(1.5, 0.06),
            tone(2.0, 0.35),
            noise(1.5, 0.06),
            tone(2.0, 0.35),
            noise(1.5, 0.06),
        ]
    )
    segs = feed(seg, audio)
    assert len(segs) >= 2, f"高底噪下只切出 {len(segs)} 段，自适应失败"


def test_flat_loud_audio_does_not_hang_forever():
    """全程持续有声（无停顿）时必须靠 max_segment 强制切分，不能无限累积。"""
    seg = Segmenter(SR, max_segment_seconds=5.0, min_speech_seconds=0.5)
    segs = feed(seg, tone(30.0, 0.3))
    assert len(segs) >= 4, "持续音频未被强制切分，内存会无限增长"
    assert all(s.duration <= 6.0 for s in segs)


def test_quiet_environment_segments_normally():
    """安静环境（低底噪）的基础能力不能被自适应逻辑破坏。"""
    seg = Segmenter(SR, min_silence_seconds=0.7, min_speech_seconds=0.5)
    audio = np.concatenate(
        [
            noise(1.0, 0.0005),
            tone(1.5, 0.25),
            noise(1.5, 0.0005),
            tone(1.5, 0.25),
            noise(1.5, 0.0005),
        ]
    )
    segs = feed(seg, audio)
    assert len(segs) == 2


def test_silence_only_never_emits():
    seg = Segmenter(SR)
    assert feed(seg, noise(10.0, 0.01)) == []
