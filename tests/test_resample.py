"""重采样回归测试。

这个 bug 一旦复发会非常隐蔽：音频时长"看起来"正常、文件能播、
只是 ASR 结果全是幻觉。所以必须用频域断言把它钉死。
"""

from __future__ import annotations

import numpy as np
import pytest

from wxlive.resample import StreamResampler


def dominant_freq(x: np.ndarray, sr: int) -> float:
    if len(x) == 0:
        return 0.0
    spec = np.abs(np.fft.rfft(x * np.hanning(len(x))))
    freqs = np.fft.rfftfreq(len(x), 1 / sr)
    return float(freqs[int(np.argmax(spec))])


def make_tone(freq: float, sec: float, sr: int) -> np.ndarray:
    t = np.arange(int(sr * sec)) / sr
    return (0.5 * np.sin(2 * np.pi * freq * t)).astype(np.float32)


@pytest.mark.parametrize("in_rate", [48000, 44100, 32000])
def test_tone_frequency_preserved(in_rate: int):
    """核心断言：重采样后频率必须不变（只是样本数变少）。"""
    x = make_tone(440.0, 2.0, in_rate)
    rs = StreamResampler(in_rate, 16000)
    y = rs.process(x)
    y = np.concatenate([y, rs.flush()])

    assert abs(dominant_freq(y, 16000) - 440.0) < 15.0, "重采样后频率漂移，ASR 会听错"
    # 时长必须保持 ~2 秒，允许 soxr 滤波器的少量延迟
    assert 1.8 <= len(y) / 16000 <= 2.2


def test_chunked_equals_whole():
    """分块重采样必须与整段处理等价，否则块边界会出现爆音。"""
    in_rate = 48000
    x = make_tone(300.0, 3.0, in_rate)

    whole = StreamResampler(in_rate, 16000)
    y_whole = np.concatenate([whole.process(x), whole.flush()])

    chunked = StreamResampler(in_rate, 16000)
    parts = []
    step = int(in_rate * 0.1)  # 100ms 块，模拟真实采集
    for i in range(0, len(x), step):
        parts.append(chunked.process(x[i : i + step]))
    parts.append(chunked.flush())
    y_chunked = np.concatenate(parts)

    assert abs(len(y_whole) - len(y_chunked)) < 64
    n = min(len(y_whole), len(y_chunked))
    diff = np.abs(y_whole[:n] - y_chunked[:n]).max()
    assert diff < 1e-3, f"分块与整段结果差异过大 ({diff})，会导致块间爆音"


def test_passthrough_when_rates_equal():
    rs = StreamResampler(16000, 16000)
    x = make_tone(200.0, 1.0, 16000)
    y = rs.process(x)
    assert len(y) == len(x)
    assert np.allclose(y, x)


def test_empty_chunk_is_safe():
    rs = StreamResampler(48000, 16000)
    assert len(rs.process(np.zeros(0, dtype=np.float32))) == 0
