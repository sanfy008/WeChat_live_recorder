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


def test_fallback_factory_is_lazy_until_needed():
    """主引擎正常时，fallback_factory 不应被调用（避免白加载本地模型）。"""
    created = []

    def make_fb():
        fb = FakeEngine("fallback", text="本地")
        created.append(fb)
        return fb

    p = FakeEngine("primary", text="云端")
    e = FallbackEngine(primary=p, fallback_factory=make_fb)
    assert e.transcribe(audio())[0]["text"] == "云端"
    assert created == []
    assert "fallback" not in e.stats

    # 主引擎失败后才真正实例化备引擎
    p.fail = True
    assert e.transcribe(audio())[0]["text"] == "本地"
    assert len(created) == 1
    assert "fallback" in e.stats


# ---------- 占位符式失败必须计入熔断（2026-11 审查） ----------


class _LossyEngine:
    """模拟 gemini_asr：不抛异常，但内部落占位并累积 failed_chunks。"""

    def __init__(self, per_call: int = 1) -> None:
        self.per_call = per_call
        self.calls = 0
        self._failed: list[tuple[float, float]] = []

    def transcribe(self, audio, sample_rate=16000):  # noqa: ARG002
        self.calls += 1
        for _ in range(self.per_call):
            self._failed.append((0.0, 1.0))
        return [{"start": 0.0, "end": 1.0, "text": "占位", "words": []}]

    @property
    def stats(self):
        return {"engine": "lossy", "failed_chunks": list(self._failed)}


def test_content_loss_counts_toward_circuit_breaker():
    """failed_chunks 增长必须触发熔断，否则整场都是占位却一直“成功”。"""
    p = _LossyEngine(per_call=1)
    f = FakeEngine("fallback", text="本地")
    e = FallbackEngine(primary=p, fallback=f, max_consecutive_failures=3)

    for _ in range(3):
        e.transcribe(audio())

    assert e.stats["degraded"] is True
    # 兼容旧语义：调用返回了仍记 primary_ok；但内容损失单独可见
    assert e.stats["primary_ok"] == 3
    assert e.stats["content_failure_calls"] == 3
    assert e.stats["failed_chunk_count"] == 3
    assert e.stats["has_content_loss"] is True
    # 降级后仍保留主引擎统计，供事后核账
    assert e.stats["primary"]["failed_chunks"]

    e.transcribe(audio())
    assert p.calls == 3, "熔断后不应再调主引擎"
    assert f.calls == 1, "熔断后的请求应走本地"


def test_single_call_with_many_failed_chunks_degrades():
    p = _LossyEngine(per_call=3)
    f = FakeEngine("fallback", text="本地")
    e = FallbackEngine(primary=p, fallback=f, max_consecutive_failures=3)

    e.transcribe(audio())

    assert e.stats["degraded"] is True
    assert e.stats["content_failure_calls"] == 1


def test_clean_chunk_resets_failure_streak():
    class _ThenClean(_LossyEngine):
        def transcribe(self, audio, sample_rate=16000):
            self.calls += 1
            if self.calls == 1:
                self._failed.append((0.0, 1.0))
            return [{"start": 0.0, "end": 1.0, "text": "ok", "words": []}]

    p = _ThenClean()
    f = FakeEngine("fallback", text="本地")
    e = FallbackEngine(primary=p, fallback=f, max_consecutive_failures=2)

    e.transcribe(audio())  # 1 次失败
    e.transcribe(audio())  # 正常 → 归零
    e.transcribe(audio())  # 仍正常
    assert e.stats["degraded"] is False
