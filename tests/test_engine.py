"""引擎自动降级（熔断）测试。

设计目标：云端失败绝不能中断录音链路。
所以用"连续失败 N 次就整场降级"的熔断策略，而不是每段都重试。
"""

from __future__ import annotations

import numpy as np

from wxlive.engine import FallbackEngine


class FakeEngine:
    def __init__(self, name, fail=False, text="句子"):
        self.name = name
        self.fail = fail
        self.calls = 0
        self.text = text

    def transcribe(self, audio, sample_rate=16000):
        self.calls += 1
        if self.fail:
            raise RuntimeError(f"{self.name} 故意失败")
        return [{"start": 0.0, "end": 1.0, "text": self.text, "words": []}]

    @property
    def stats(self):
        return {"engine": self.name, "calls": self.calls}


def audio():
    return np.zeros(16000, dtype=np.float32)


def test_primary_used_when_healthy():
    p = FakeEngine("primary", text="云端结果")
    f = FakeEngine("fallback", text="本地结果")
    e = FallbackEngine(primary=p, fallback=f)

    out = e.transcribe(audio())
    assert out[0]["text"] == "云端结果"
    assert f.calls == 0, "主引擎正常时不应调用备引擎"


def test_fallback_on_failure():
    p = FakeEngine("primary", fail=True)
    f = FakeEngine("fallback", text="本地结果")
    e = FallbackEngine(primary=p, fallback=f, max_consecutive_failures=10)

    out = e.transcribe(audio())
    assert out[0]["text"] == "本地结果", "主引擎失败时应回退本地"
    assert f.calls == 1


def test_circuit_breaker_stops_retrying_primary():
    """连续失败达到阈值后，不应再尝试主引擎（避免整场延迟翻倍）。"""
    p = FakeEngine("primary", fail=True)
    f = FakeEngine("fallback")
    e = FallbackEngine(primary=p, fallback=f, max_consecutive_failures=3)

    for _ in range(10):
        e.transcribe(audio())

    assert p.calls == 3, f"熔断后不应再调主引擎，实际调用 {p.calls} 次"
    assert f.calls == 10, "所有请求都应由本地引擎完成"
    assert e.stats["degraded"] is True


def test_recovery_resets_failure_count():
    """主引擎偶尔失败但能恢复时，不应误熔断。"""
    class Flaky:
        def __init__(self):
            self.n = 0

        def transcribe(self, a, sr=16000):
            self.n += 1
            if self.n % 3 == 0:
                raise RuntimeError("偶发失败")
            return [{"start": 0, "end": 1, "text": "ok", "words": []}]

        @property
        def stats(self):
            return {}

    p = Flaky()
    f = FakeEngine("fallback")
    e = FallbackEngine(primary=p, fallback=f, max_consecutive_failures=3)

    for _ in range(9):
        e.transcribe(audio())

    assert e.stats["degraded"] is False, "偶发失败被误判为熔断"
    assert e.stats["primary_ok"] >= 4


def test_no_primary_goes_straight_to_fallback():
    f = FakeEngine("fallback", text="本地")
    e = FallbackEngine(primary=None, fallback=f)
    out = e.transcribe(audio())
    assert out[0]["text"] == "本地"


def test_no_fallback_returns_empty_not_crash():
    """没有备引擎且主引擎失败时，返回空列表而不是抛异常中断录制。"""
    p = FakeEngine("primary", fail=True)
    e = FallbackEngine(primary=p, fallback=None)
    assert e.transcribe(audio()) == []
