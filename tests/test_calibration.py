"""校准期行为测试（实时路径专属的时序 bug）。

背景：实时运行时，开头约 3 秒处于底噪校准期。
初版在这段时间里仍然用初始阈值 0.004 做语音判定，
而真实音频 RMS 普遍 > 0.02，于是全部被判为"有声"，
开头整段话粘连成一个巨大片段 → ASR 合并、丢句、幻觉。
离线复现（整段喂入）不会触发，只有实时路径才会 —— 所以必须单独测。
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
    return rng.normal(0, amp, int(SR * sec)).astype(np.float32)


def feed(seg: Segmenter, audio: np.ndarray, chunk_sec: float = 0.1) -> list:
    out = []
    n = int(SR * chunk_sec)
    for i in range(0, len(audio), n):
        out.extend(seg.feed(audio[i : i + n]))
    return out


def test_speech_during_calibration_is_not_merged_into_one_blob():
    """校准期内的人声不能被粘成一个大片段。"""
    seg = Segmenter(SR, calibration_seconds=3.0, min_silence_seconds=0.7, min_speech_seconds=0.5)
    # 校准期 3 秒内就说话，且中间有停顿 → 必须能切出来
    audio = np.concatenate(
        [
            tone(1.2, 0.25),  # 校准期内就有人声
            noise(1.0, 0.02),
            tone(1.2, 0.25),
            noise(1.2, 0.02),
            tone(1.2, 0.25),
            noise(1.2, 0.02),
        ]
    )
    segs = feed(seg, audio)
    assert len(segs) >= 2, (
        f"校准期内的人声被粘成 {len(segs)} 段，实时录制会丢句/合并"
    )
    # 不能出现一个吞掉大部分音频的巨型片段
    assert max(s.duration for s in segs) < 4.0, "出现异常长的合并片段"


def test_no_audio_lost_during_calibration():
    """校准期的音频不能丢：片段总时长应覆盖绝大部分有声内容。"""
    seg = Segmenter(SR, calibration_seconds=2.0, min_silence_seconds=0.6, min_speech_seconds=0.4)
    audio = np.concatenate([tone(1.5, 0.25), noise(0.8, 0.02), tone(1.5, 0.25), noise(1.0, 0.02)])
    segs = feed(seg, audio)
    tail = seg.flush()
    total = sum(s.duration for s in segs) + (tail.duration if tail else 0)
    # 两段各 1.5s 语音，合计应接近 3s（允许 preroll/tail pad 的少量冗余）
    assert total >= 2.6, f"只切出 {total:.2f}s，校准期音频丢失"


def test_calibration_completes_and_sets_threshold():
    seg = Segmenter(SR, calibration_seconds=2.0)
    feed(seg, np.concatenate([noise(2.5, 0.02)]))
    assert seg._calib_done, "校准未完成"
    assert seg.silence_rms > 0
