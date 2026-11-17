"""fix_errors 并发与失败重试测试。

只 mock `_call`，不碰真实网关、不 sleep（重试间隔传 0 或空）。
并发保护逻辑与串行一致：重试用尽后行数不匹配或抛异常时，该批保留原文。
"""

from __future__ import annotations

import re

from wxlive import postprocess


def _fake_call(model, prompt, endpoint, api_key, **kwargs):
    """把 prompt 里的待修正行原样加前缀返回，行数天然一致。"""
    body = prompt.split("待修正：\n", 1)[1]
    lines = [re.sub(r"^\d+\. ", "", ln) for ln in body.split("\n") if ln.strip()]
    return "\n".join(f"已修正-{ln}" for ln in lines)


def test_concurrent_preserves_order(monkeypatch):
    """并发结果必须按输入批顺序拼回。"""
    monkeypatch.setattr(postprocess, "_call", _fake_call)
    lines = [f"原句{i}" for i in range(10)]

    out = postprocess.fix_errors(lines, batch_size=2, workers=4)

    assert out == [f"已修正-原句{i}" for i in range(10)]


def test_concurrent_mismatched_line_count_keeps_batch(monkeypatch):
    """并发下某批行数不匹配：只该批保留原文，其他批正常使用结果。"""
    def call(model, prompt, *a, **k):
        if "原句2" in prompt:
            return "只有一行"
        return _fake_call(model, prompt, *a, **k)

    monkeypatch.setattr(postprocess, "_call", call)
    lines = [f"原句{i}" for i in range(6)]

    out = postprocess.fix_errors(lines, batch_size=2, workers=3,
                                 retry_delays=())
    # 第 2 批（原句 2、3）保留原文，其余批使用修正结果
    assert out == [
        "已修正-原句0", "已修正-原句1",
        "原句2", "原句3",
        "已修正-原句4", "已修正-原句5",
    ]


def test_concurrent_one_batch_exception_isolated(monkeypatch):
    """并发下某批抛异常：只影响该批，其余批结果照常返回。"""
    def call(model, prompt, *a, **k):
        if "原句4" in prompt:
            raise RuntimeError("模拟网络失败")
        return _fake_call(model, prompt, *a, **k)

    monkeypatch.setattr(postprocess, "_call", call)
    lines = [f"原句{i}" for i in range(8)]

    out = postprocess.fix_errors(lines, batch_size=2, workers=4,
                                 retry_delays=())

    assert len(out) == len(lines)
    assert out == [
        "已修正-原句0", "已修正-原句1",
        "已修正-原句2", "已修正-原句3",
        "原句4", "原句5",
        "已修正-原句6", "已修正-原句7",
    ]


def test_concurrent_all_batches_fail_returns_original(monkeypatch):
    def boom(*a, **k):
        raise RuntimeError("全部失败")

    monkeypatch.setattr(postprocess, "_call", boom)
    lines = [f"句子{i}" for i in range(5)]

    assert postprocess.fix_errors(lines, batch_size=2, workers=3,
                                  retry_delays=()) == lines


def test_concurrent_progress_reaches_total(monkeypatch):
    monkeypatch.setattr(postprocess, "_call", _fake_call)
    calls: list[tuple[int, int]] = []

    postprocess.fix_errors([f"句子{i}" for i in range(5)], batch_size=2,
                           workers=2, progress=lambda d, t: calls.append((d, t)))

    assert calls == [(1, 3), (2, 3), (3, 3)]


def test_serial_path_still_works(monkeypatch):
    """workers=1 保持原串行路径，结果与并发一致。"""
    monkeypatch.setattr(postprocess, "_call", _fake_call)
    lines = [f"原句{i}" for i in range(7)]

    assert (postprocess.fix_errors(lines, batch_size=3, workers=1)
            == postprocess.fix_errors(lines, batch_size=3, workers=4))


# ---------------- 失败重试 ----------------

def test_default_retry_delays_and_workers():
    assert postprocess.FIX_RETRY_DELAYS == (1.0, 4.0)
    import inspect
    sig = inspect.signature(postprocess.fix_errors)
    assert sig.parameters["workers"].default == 12
    assert sig.parameters["retry_delays"].default is None


def test_retry_recovers_from_mismatch(monkeypatch):
    """第 1 次行数不符、第 2 次正常：应取第 2 次结果。"""
    calls = {"n": 0}

    def call(model, prompt, *a, **k):
        calls["n"] += 1
        if calls["n"] == 1:
            return "只有一行"
        return _fake_call(model, prompt, *a, **k)

    monkeypatch.setattr(postprocess, "_call", call)
    lines = ["原一", "原二", "原三"]

    out = postprocess.fix_errors(lines, batch_size=10, workers=1,
                                 retry_delays=(0.0, 0.0))

    assert calls["n"] == 2
    assert out == ["已修正-原一", "已修正-原二", "已修正-原三"]


def test_retry_recovers_from_http_error(monkeypatch):
    """HTTP 502 后重试成功。"""
    calls = {"n": 0}

    def call(model, prompt, *a, **k):
        calls["n"] += 1
        if calls["n"] == 1:
            raise OSError("HTTP Error 502: Bad Gateway")
        return _fake_call(model, prompt, *a, **k)

    monkeypatch.setattr(postprocess, "_call", call)

    out = postprocess.fix_errors(["原一", "原二"], batch_size=10, workers=1,
                                 retry_delays=(0.0, 0.0))

    assert calls["n"] == 2
    assert out == ["已修正-原一", "已修正-原二"]


def test_retry_exhausted_keeps_original(monkeypatch):
    """3 次尝试都失败：保留原文，调用次数封顶 3 次。"""
    calls = {"n": 0}

    def call(*a, **k):
        calls["n"] += 1
        raise OSError("HTTP Error 403: Forbidden")

    monkeypatch.setattr(postprocess, "_call", call)
    lines = ["原一", "原二"]

    out = postprocess.fix_errors(lines, batch_size=10, workers=1,
                                 retry_delays=(0.0, 0.0))

    assert calls["n"] == 3
    assert out == lines


def test_retry_exhausted_mismatch_keeps_original(monkeypatch):
    """行数不符重试也用尽：保留原文。"""
    calls = {"n": 0}

    def call(*a, **k):
        calls["n"] += 1
        return "只有一行"

    monkeypatch.setattr(postprocess, "_call", call)
    lines = ["原一", "原二"]

    assert postprocess.fix_errors(lines, batch_size=10, workers=1,
                                  retry_delays=(0.0, 0.0)) == lines
    assert calls["n"] == 3


def test_retry_in_concurrent_batches_keeps_order(monkeypatch):
    """并发中某批重试成功，最终顺序仍按输入批下标。"""
    seen: set[str] = set()

    def call(model, prompt, *a, **k):
        if "原句2" in prompt and "原句2" not in seen:
            seen.add("原句2")
            return "只有一行"
        return _fake_call(model, prompt, *a, **k)

    monkeypatch.setattr(postprocess, "_call", call)
    lines = [f"原句{i}" for i in range(6)]

    out = postprocess.fix_errors(lines, batch_size=2, workers=3,
                                 retry_delays=(0.0, 0.0))

    assert out == [f"已修正-原句{i}" for i in range(6)]
