"""\u9636\u68AF\u5F0F WAV \u589E\u957F\u4E0B\u9759\u97F3\u5224\u505C\u7684\u56DE\u5F52\u6D4B\u8BD5\uFF082026-11-23\uFF09\u3002

\u5B9E\u6D4B\u80CC\u666F\uFF1Affmpeg \u5199 WAV \u662F**\u5927\u5757\u7F13\u51B2**\u7684 \u2014\u2014 \u524D 7 \u79D2\u6587\u4EF6\u5927\u5C0F\u6052\u4E3A 0\uFF0C
\u4E4B\u540E\u6BCF\u6B21\u6309\u7EA6 256KB \u4E00\u8DF3\u3002\u800C\u76D1\u63A7\u6BCF 3 \u79D2\u624D\u8BFB\u4E00\u6B21\u672B\u5C3E 4.5 \u79D2\uFF0C\u4E8E\u662F\uFF1A
- \u5927\u90E8\u5206\u8F6E\u6B21\u8BFB\u5230\u7684\u662F\u540C\u4E00\u6BB5\u65E7\u6570\u636E \u2192 poll() \u56DE"\u6587\u4EF6\u672A\u589E\u957F"
- \u8DF3\u4E00\u6B21\u5410\u51FA 8 \u79D2\uFF0C\u4F46\u53EA\u5206\u6790\u4E86\u672B\u5C3E 4.5 \u79D2 \u2192 \u4E2D\u95F4 3.5 \u79D2\u6C38\u8FDC\u6F0F\u6389

\u540E\u679C\uFF08\u5B9E\u6D4B\uFF09\uFF1A40 \u79D2\u5168\u9759\u97F3\u53EA\u7D2F\u8BA1\u5230 18 \u79D2\uFF0C\u9608\u503C 40s \u6C38\u8FDC\u8FBE\u4E0D\u5230\uFF0C
\u4E3B\u64AD\u4E0B\u64AD\u540E NAS \u4F1A\u4E00\u76F4\u5F55\u5230\u7B7E\u540D\u8FC7\u671F\u3002

\u4FEE\u6CD5\u5728 nas_runner._watch_recorder\uFF1A\u8BFB\u5230"\u6587\u4EF6\u672A\u589E\u957F"\u4E14\u6B64\u524D\u5DF2\u542C\u5230\u8FC7
\u58F0\u97F3\u65F6\uFF0C\u6309**\u5B9E\u9645\u6D41\u901D\u65F6\u95F4**\u63A8\u8FDB\u9759\u97F3\u8BA1\u6570\uFF08\u8FD9\u6BB5\u65F6\u95F4\u6587\u4EF6\u6CA1\u53D8 = \u8FD8\u5728\u9759\u97F3\uFF09\u3002
"""

from __future__ import annotations

import numpy as np
import pytest

from wxlive.livesilence import LiveSilenceMonitor


class _ClockSim:
    """\u590D\u523B _watch_recorder \u7684\u9759\u97F3\u5206\u652F\uFF08\u9636\u68AF\u589E\u957F + \u6309\u65F6\u949F\u8865\u8BA1\uFF09\u3002"""

    def __init__(self, stop_secs: float = 40.0, window: float = 4.5,
                 sample_every: float = 3.0, flush_every: float = 8.0):
        self.monitor = LiveSilenceMonitor(
            silence_rms=0.01, stop_after_seconds=stop_secs,
            require_sound_before=True,
        )
        self.stop_secs = stop_secs
        self.window = window
        self.every = sample_every
        self.flush_every = flush_every
        self.sr = 16000
        self.last_check = 0.0
        self.stopped_at = None

    def file_secs_at(self, t: float) -> float:
        return (int(t) // int(self.flush_every)) * float(self.flush_every)

    def step(self, t: float, speech_until: float) -> None:
        """推进一帧。

        speech_until 是"文件里前多少秒是说话"，
        按**已落盘秒数**判断而不是按时钟 —— 文件在 t=8 才 flush 出 8 秒，
        按时钟判会永远采样不到那 8 秒声音。
        """
        if t - self.last_check < self.every:
            return
        gap = t - self.last_check if self.last_check else 0.0
        self.last_check = t

        total = self.file_secs_at(t)
        if total <= 0:
            return
        start = max(0.0, total - self.window)
        dur = total - start
        speaking = total <= speech_until
        audio = np.full(int(self.sr * dur), 0.3 if speaking else 0.0,
                        dtype=np.float32)
        verdict = self.monitor.poll(audio, self.sr, audio_offset=start)

        # \u2190 \u8FD9\u5C31\u662F\u4FEE\u590D\u70B9
        if not verdict.checked and self.monitor.heard_sound and gap > 0:
            self.monitor.silent_seconds += gap
            if self.monitor.silent_seconds >= self.stop_secs:
                self.stopped_at = t
                return

        if verdict.should_stop:
            self.stopped_at = t


def test_silence_detected_under_chunked_growth():
    """\u9636\u68AF\u589E\u957F\u4E0B\uFF0C40 \u79D2\u9759\u97F3\u5FC5\u987B\u771F\u7684\u80FD\u89E6\u53D1\u505C\u6B62\u3002

    \u4FEE\u590D\u524D\uFF1A\u8DD1\u6EE1 75 \u79D2\u4E5F\u53EA\u7D2F\u8BA1\u5230\u5341\u51E0\u79D2\uFF0C\u6C38\u8FDC\u4E0D\u505C\u3002
    """
    sim = _ClockSim()
    t = 0.0
    while t < 120.0 and sim.stopped_at is None:
        t += sim.every
        # \u524D 8 \u79D2\u6709\u58F0\u97F3\uFF0C\u4E4B\u540E\u4E3B\u64AD\u4E0B\u64AD
        sim.step(t, speech_until=8.0)

    assert sim.stopped_at is not None, "40 \u79D2\u9759\u97F3\u6CA1\u6709\u89E6\u53D1\u505C\u6B62\uFF08\u56DE\u5F52\u4E86\uFF09"
    # \u9759\u97F3\u4ECE ~8s \u8D77\u7B97\uFF0C\u9608\u503C 40s \u2192 \u5E94\u5728 8+40=48s \u4E4B\u540E\u4E0D\u4E45\u505C
    assert 44.0 <= sim.stopped_at <= 60.0, f"\u505C\u6B62\u65F6\u523B\u5F02\u5E38\uFF1A{sim.stopped_at}"


def test_no_stop_while_stream_still_has_sound():
    """\u4E00\u76F4\u5728\u8BF4\u8BDD\u5C31\u4E0D\u8BE5\u505C \u2014\u2014 \u522B\u628A\u6B63\u5E38\u76F4\u64AD\u6390\u4E86\u3002"""
    sim = _ClockSim()
    t = 0.0
    while t < 120.0:
        t += sim.every
        sim.step(t, speech_until=1e9)
    assert sim.stopped_at is None


def test_no_stop_before_any_sound():
    """\u5F00\u64AD\u524D\u9ED1\u5C4F\u7F13\u51B2\u671F\uFF08\u4E00\u76F4\u9759\u97F3\uFF09\u4E0D\u80FD\u88AB\u7B97\u6210"\u4E3B\u64AD\u4E0B\u64AD"\u3002"""
    sim = _ClockSim()
    t = 0.0
    while t < 60.0:
        t += sim.every
        sim.step(t, speech_until=0.0)
    assert sim.stopped_at is None, \
        "\u8FD8\u6CA1\u542C\u5230\u8FC7\u58F0\u97F3\u5C31\u5224\u505C\u4E86\uFF08\u4F1A\u628A\u521A\u5F00\u64AD\u7684\u76F4\u64AD\u6390\u6389\uFF09"
