"""finish_reason="length" 截断处理测试。

背景：workbuddy/deepseek-v4.1-flash（effort=low）会把 reasoning_tokens
计入 completion_tokens/max_tokens，触顶时返回 finish_reason="length"，
content 被硬截断。不可把截断结果当作成功。

这里只 mock `_call`（返回 str，保持旧签名兼容）或 urllib，不碰真实网关。
"""

from __future__ import annotations

import io
import json

from wxlive import postprocess


def _fake_call(model, prompt, endpoint, api_key, **kwargs):
    out_meta = kwargs.get("out_meta")
    if out_meta is not None:
        out_meta["finish_reason"] = "stop"
        out_meta["usage"] = {"completion_tokens": 10}
    body = prompt.split("待修正：\n", 1)[-1]
    n = sum(1 for ln in body.split("\n") if ln.strip())
    return "\n".join(["已修正"] * max(n, 1))


# ---------------- _call 记录 finish_reason ----------------

class _FakeResp(io.BytesIO):
    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


def test_call_records_finish_reason_and_keeps_str_return(monkeypatch):
    body = {
        "choices": [{"message": {"content": "文本"}, "finish_reason": "length"}],
        "usage": {"completion_tokens": 8000, "total_tokens": 8300},
    }
    monkeypatch.setattr(postprocess.urllib.request, "urlopen",
                        lambda *a, **k: _FakeResp(json.dumps(body).encode()))

    meta: dict = {}
    out = postprocess._call("m", "p", "http://x", "k", out_meta=meta)

    assert out == "文本"
    assert meta["finish_reason"] == "length"
    assert meta["usage"]["completion_tokens"] == 8000


def test_call_uses_messages_when_given(monkeypatch):
    captured = {}

    def fake_urlopen(req, timeout=0):
        captured["payload"] = json.loads(req.data.decode())
        return _FakeResp(json.dumps({
            "choices": [{"message": {"content": "续"}, "finish_reason": "stop"}],
        }).encode())

    monkeypatch.setattr(postprocess.urllib.request, "urlopen", fake_urlopen)
    msgs = [{"role": "user", "content": "第一轮"},
            {"role": "assistant", "content": "半截"},
            {"role": "user", "content": "继续"}]

    assert postprocess._call("m", "ignored", "http://x", "k", messages=msgs) == "续"
    assert captured["payload"]["messages"] == msgs


# ---------------- fix_errors：截断必须重试 ----------------

def test_fix_retries_when_truncated(monkeypatch):
    calls = {"n": 0}

    def call(model, prompt, endpoint, api_key, **kwargs):
        calls["n"] += 1
        meta = kwargs.get("out_meta")
        if calls["n"] == 1:
            meta["finish_reason"] = "length"
            return "被截断的第一行"
        meta["finish_reason"] = "stop"
        return "修正一\n修正二"

    monkeypatch.setattr(postprocess, "_call", call)
    stats: dict = {}
    out = postprocess.fix_errors(["原一", "原二"], batch_size=10, workers=1,
                                 retry_delays=(0.0, 0.0), out_stats=stats)

    assert calls["n"] == 2
    assert out == ["修正一", "修正二"]
    assert stats["failed_batches"] == []


def test_fix_truncated_exhausted_keeps_original(monkeypatch):
    calls = {"n": 0}

    def call(model, prompt, endpoint, api_key, **kwargs):
        calls["n"] += 1
        kwargs["out_meta"]["finish_reason"] = "length"
        return "被截断"

    monkeypatch.setattr(postprocess, "_call", call)
    stats: dict = {}
    lines = ["原一", "原二"]

    out = postprocess.fix_errors(lines, batch_size=10, workers=1,
                                 retry_delays=(0.0, 0.0), out_stats=stats)

    assert calls["n"] == 3
    assert out == lines
    assert stats["failed_batches"] == [1]


def test_fix_escalates_max_tokens_when_truncated(monkeypatch):
    """截断不能拿同样预算重问；必须逐步放大 max_tokens。"""
    seen = []

    def call(model, prompt, endpoint, api_key, **kwargs):
        seen.append(kwargs.get("max_tokens"))
        meta = kwargs["out_meta"]
        if len(seen) == 1:
            meta["finish_reason"] = "length"
            return "半截"
        meta["finish_reason"] = "stop"
        return "修正一\n修正二"

    monkeypatch.setattr(postprocess, "_call", call)
    out = postprocess.fix_errors(["原一", "原二"], batch_size=10, workers=1,
                                 retry_delays=(0.0, 0.0))

    assert out == ["修正一", "修正二"]
    assert seen[0] == postprocess.FIX_MAX_TOKENS
    assert seen[1] == min(postprocess.FIX_MAX_TOKENS_CAP,
                          postprocess.FIX_MAX_TOKENS * 2)


def test_fix_strips_number_prefix_from_output(monkeypatch):
    """模型偶尔无视“不要序号”，返回 1. / 2、/ 3) 前缀，必须剥离。"""
    def call(model, prompt, endpoint, api_key, **kwargs):
        kwargs["out_meta"]["finish_reason"] = "stop"
        return "1. 修正一\n2、修正二\n3) 修正三"

    monkeypatch.setattr(postprocess, "_call", call)
    out = postprocess.fix_errors(["原一", "原二", "原三"], batch_size=10,
                                 workers=1)
    assert out == ["修正一", "修正二", "修正三"]


def test_fix_does_not_strip_decimal_lines(monkeypatch):
    """小数开头的内容不能被当序号删掉（1.5万 ≠ 5万）。"""
    def call(model, prompt, endpoint, api_key, **kwargs):
        kwargs["out_meta"]["finish_reason"] = "stop"
        return "1.5万人报名\n2.5万学费"

    monkeypatch.setattr(postprocess, "_call", call)
    out = postprocess.fix_errors(["原一", "原二"], batch_size=10, workers=1)
    assert out == ["1.5万人报名", "2.5万学费"]


def test_fix_monkeypatched_str_call_still_works(monkeypatch):
    """旧测试的 _call 只返回 str、不写 out_meta：不能因此误判失败。"""
    monkeypatch.setattr(postprocess, "_call", _fake_call)
    assert postprocess.fix_errors(["原一", "原二", "原三"], batch_size=10,
                                  workers=1) == ["已修正"] * 3


# ---------------- build_digest：自动续写 ----------------

def test_digest_continues_when_truncated(monkeypatch):
    calls = {"n": 0}

    def call(model, prompt, endpoint, api_key, **kwargs):
        calls["n"] += 1
        meta = kwargs["out_meta"]
        if calls["n"] == 1:
            meta["finish_reason"] = "length"
            return "## 一句话概括\n前半段内容"
        meta["finish_reason"] = "stop"
        return "后半段内容"

    monkeypatch.setattr(postprocess, "_call", call)
    stats: dict = {}
    out = postprocess.build_digest([{"start": 0, "end": 5, "text": "一句"}],
                                   out_stats=stats)

    assert calls["n"] == 2
    assert out == "## 一句话概括\n前半段内容后半段内容"
    assert stats["ok"] is True
    assert stats["continuations"] == 1
    assert stats["truncated"] is False
    assert stats["finish_reason"] == "stop"


def test_digest_continuation_strips_repeated_prefix(monkeypatch):
    calls = {"n": 0}

    def call(model, prompt, endpoint, api_key, **kwargs):
        calls["n"] += 1
        meta = kwargs["out_meta"]
        if calls["n"] == 1:
            meta["finish_reason"] = "length"
            return "第一段写到这"
        meta["finish_reason"] = "stop"
        return "写到这接着写"

    monkeypatch.setattr(postprocess, "_call", call)
    out = postprocess.build_digest([{"start": 0, "end": 1, "text": "x"}])
    assert out == "第一段写到这接着写"


def test_digest_truncated_after_continuations_is_unhealthy(monkeypatch):
    def call(model, prompt, endpoint, api_key, **kwargs):
        kwargs["out_meta"]["finish_reason"] = "length"
        return f"第{len(kwargs['messages'])}轮片段"

    monkeypatch.setattr(postprocess, "_call", call)
    stats: dict = {}
    out = postprocess.build_digest([{"start": 0, "end": 1, "text": "x"}],
                                   out_stats=stats)

    # 1 次首发 + 3 次续写（对话每轮 +2 条消息）
    assert out == "第1轮片段第3轮片段第5轮片段第7轮片段"
    assert stats["ok"] is False
    assert stats["truncated"] is True
    assert stats["continuations"] == 3
    assert "截断" in stats["error"]


def test_digest_uses_larger_max_tokens_for_final_merge(monkeypatch):
    seen = []

    def call(model, prompt, endpoint, api_key, **kwargs):
        seen.append(kwargs.get("max_tokens"))
        kwargs["out_meta"]["finish_reason"] = "stop"
        return "ok"

    monkeypatch.setattr(postprocess, "_call", call)
    postprocess.build_digest([{"start": 0, "end": 1, "text": "x"}])

    assert seen == [postprocess.DIGEST_MAX_TOKENS]
    assert postprocess.DIGEST_MAX_TOKENS == 16000


# ---------------- build_digest：分块失败/截断不可假健康 ----------------

def _long_segments(n_chars: int = 35000) -> list[dict]:
    return [{"start": 0, "end": 60, "text": "长" * n_chars}]


def test_digest_chunk_failure_marks_unhealthy(monkeypatch):
    calls = {"n": 0}

    def call(model, prompt, endpoint, api_key, **kwargs):
        calls["n"] += 1
        if "部分记录" in prompt:  # 分块摘要请求
            raise RuntimeError("分块超时")
        kwargs["out_meta"]["finish_reason"] = "stop"
        return "归并结果"

    monkeypatch.setattr(postprocess, "_call", call)
    stats: dict = {}
    out = postprocess.build_digest(_long_segments(), out_stats=stats, retry_delays=())

    assert out == "归并结果"
    assert stats["ok"] is False
    assert stats["failed_chunks"]
    assert "分块超时" in stats["error"]

    # 无重试时 1 次分块 + 1 次归并 = 2 次调用
    assert calls["n"] == 2


def test_digest_chunk_retries_and_recovers(monkeypatch):
    """分块摘要偶发超时/502 时应退避重试并恢复，不能直接把残缺正文写进成品。"""
    attempts = {"chunk": 0, "merge": 0}

    def call(model, prompt, endpoint, api_key, **kwargs):
        if "部分记录" in prompt:
            attempts["chunk"] += 1
            if attempts["chunk"] < 2:
                raise TimeoutError("The read operation timed out")
            kwargs["out_meta"]["finish_reason"] = "stop"
            return "恢复后的分块摘要"
        attempts["merge"] += 1
        kwargs["out_meta"]["finish_reason"] = "stop"
        return "完整归并结果"

    monkeypatch.setattr(postprocess, "_call", call)
    stats: dict = {}
    out = postprocess.build_digest(
        _long_segments(), out_stats=stats, retry_delays=(0.0, 0.0)
    )

    assert out == "完整归并结果"
    assert stats["ok"] is True
    assert stats["chunk_retries"] == 1
    assert attempts == {"chunk": 2, "merge": 1}


def test_digest_chunk_truncated_marks_unhealthy(monkeypatch):
    def call(model, prompt, endpoint, api_key, **kwargs):
        meta = kwargs["out_meta"]
        if "部分记录" in prompt:
            meta["finish_reason"] = "length"
            return "分块半截"
        meta["finish_reason"] = "stop"
        return "归并结果"

    monkeypatch.setattr(postprocess, "_call", call)
    stats: dict = {}
    out = postprocess.build_digest(_long_segments(), out_stats=stats)

    assert out == "归并结果"
    assert stats["ok"] is False
    assert stats["truncated"] is False  # 终稿没截断，问题在分块
    assert "截断" in stats["error"]
