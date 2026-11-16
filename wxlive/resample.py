"""流式重采样。

为什么必须做：
  采集端用设备原生采样率（常见 48000Hz），而 whisper 只接受 16000Hz。
  如果把 48k 的数组直接喂给 whisper，它会当成 16k 来解读 ——
  相当于把音频慢放 3 倍，模型完全听不懂，就会开始"编"（幻觉）。
  这是本项目踩过的最隐蔽的 bug：音频看起来正常、时长看起来正常，
  只是转写结果全是无关内容。

用 soxr 的流式接口，保证分块之间相位连续，避免块边界出现爆音。
"""

from __future__ import annotations

import numpy as np
import soxr

TARGET_SR = 16000


class StreamResampler:
    """把任意采样率的连续音频块转成 16kHz。"""

    def __init__(self, in_rate: int, out_rate: int = TARGET_SR, quality: str = "HQ") -> None:
        self.in_rate = in_rate
        self.out_rate = out_rate
        self._passthrough = in_rate == out_rate
        self._stream = None if self._passthrough else soxr.ResampleStream(
            in_rate, out_rate, 1, dtype="float32", quality=quality
        )

    def process(self, chunk: np.ndarray) -> np.ndarray:
        if len(chunk) == 0:
            return np.zeros(0, dtype=np.float32)
        if self._passthrough:
            return chunk.astype(np.float32, copy=False)
        out = self._stream.resample_chunk(chunk.astype(np.float32, copy=False), last=False)
        return np.asarray(out, dtype=np.float32).reshape(-1)

    def flush(self) -> np.ndarray:
        """会话结束时取出重采样器内部残余的样本。"""
        if self._passthrough or self._stream is None:
            return np.zeros(0, dtype=np.float32)
        out = self._stream.resample_chunk(np.zeros(0, dtype=np.float32), last=True)
        return np.asarray(out, dtype=np.float32).reshape(-1)
