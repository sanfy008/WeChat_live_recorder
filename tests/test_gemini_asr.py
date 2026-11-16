"""Gemini 引擎的离线可测部分（不打网络）。

重点覆盖两个踩过的坑：
1. 空/极短输入必须早退 —— 否则模型会凭空编出一大段内容（实测 206 句假句子）
2. 繁体输出必须转简体 —— 3.8-flash 实测整段繁体
"""

from __future__ import annotations

import numpy as np

from wxlive.gemini_asr import GeminiASR, to_simplified


def test_empty_audio_returns_empty():
    """空输入必须早退，不能拿空音频去问模型。"""
    g = GeminiASR(model="dummy")
    assert g.transcribe(np.zeros(0, dtype=np.float32), 16000) == []


def test_too_short_audio_returns_empty():
    """极短音频（0.1s）应早退，避免模型瞎猜。"""
    g = GeminiASR(model="dummy")
    assert g.transcribe(np.zeros(int(16000 * 0.1), dtype=np.float32), 16000) == []


def test_traditional_to_simplified():
    t = "它寒假會有一個冬令營，雲山培英考初中內容嗎？"
    s = to_simplified(t)
    assert "會" not in s and "個" not in s and "營" not in s, f"繁转简失败: {s}"
    assert "会" in s and "个" in s and "营" in s


def test_simplified_text_unchanged():
    t = "他寒假会有一个冬令营，云山培英考初中内容吗？"
    assert to_simplified(t) == t


def test_empty_string_safe():
    assert to_simplified("") == ""


class _FakeResp:
    def __init__(self, text):
        self._text = text

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def read(self):
        import json

        return json.dumps(
            {"choices": [{"message": {"content": self._text}}]}
        ).encode()


def test_long_audio_is_chunked(monkeypatch):
    """超过 110s 必须自动切片，而不是整个塞给云端（云端单次上限 120s）。"""
    g = GeminiASR(model="dummy")
    calls = []

    # 真实直播 110s 块产出 580~670 字（密度约 5.5 字/秒）。
    # 这里每句 75 字 × 3 句 = 225 字，密度 2.0 字/秒，高于 1.5 的异常线，
    # 不会触发重试，用例只关心切片边界。
    one_line = (
        "这是一句足够长的测试内容用来避免触发长度异常的判定逻辑所以需要写得更长一些"
        "并且再补充一些内容让它达到真实直播的平均语速水平这样就不会被误判"
    )

    def fake_call(pcm):
        calls.append(len(pcm))
        return "\n".join([one_line] * 3)

    monkeypatch.setattr(g, "_call_once", fake_call)
    # 250 秒音频 → 应切成 3 片（110 + 110 + 30）
    audio = np.zeros(16000 * 250, dtype=np.float32)
    out = g.transcribe(audio, 16000)

    assert len(calls) == 3, f"应切 3 片，实际 {len(calls)} 片: {calls}"
    assert all(c <= 16000 * 110 for c in calls), "单片的长度超过 110s 上限"
    assert len(out) == 9, "每片产出 3 句"


def test_timestamps_are_monotonic(monkeypatch):
    g = GeminiASR(model="dummy")
    # 文本要够长，否则会被判为"长度异常"而触发重试，干扰本用例的断言
    long_text = "第一句话内容足够长一些\n第二句话内容更长一些也要够长"
    monkeypatch.setattr(g, "_call_once", lambda pcm: long_text)
    audio = np.zeros(16000 * 200, dtype=np.float32)
    out = g.transcribe(audio, 16000)

    assert len(out) == 4  # 2 片 × 每片 2 句
    prev = -1.0
    for seg in out:
        assert seg["start"] >= prev - 1e-6, "时间戳出现倒序"
        assert seg["end"] > seg["start"]
        prev = seg["end"]


# ---------- 幻觉检测与恢复（2026-11-16 新增） ----------


def test_hallucination_detected_on_real_samples():
    """用真实产物里出现的幻觉原文做回归。"""
    from wxlive import quality

    samples = [
        "抱歉，我无法访问或接收您提到的音频文件 `[attachment audio/wav]`，"
        "当前对话中未包含实际音频内容。",
        "未收到有效的音频文件，仅有 `[attachment audio/wav]` 占位文本，无法进行转写。",
        "无法完成转写，当前会话中仅包含 `[attachment audio/wav]` 的文字占位。",
    ]
    for s in samples:
        assert quality.is_hallucination(s), f"漏判了真实幻觉样本: {s[:40]}"


def test_normal_speech_not_flagged():
    """正常口播不能误杀——这是硬约束。"""
    from wxlive import quality

    normal = [
        "那个，东风实验的青云A班，应该它的重点班是吧？",
        "我无法理解家长为什么这么想，其实民办公办都有机会。",
        "这个音频设备好像有点问题，刚才声音小了。",
        "没有这个学校，我说的是白云区那所。",
        "好，下一个问题。西雅的岭南怎么选？",
    ]
    for s in normal:
        assert not quality.is_hallucination(s), f"误杀了正常口播: {s}"


def test_hallucination_is_retried_then_recovered(monkeypatch):
    """检出幻觉 → 原样重试一次；重试正常则救回，不落占位。"""
    g = GeminiASR(model="dummy")
    good = (
        "好，下一个问题。西雅的岭南和十六中重点班怎么选，这个要说清楚一些，"
        "所以我把句子写得长一点，让它达到真实直播的平均语速密度，避免被长度检查误判。"
        "西雅的话如果你指的是东西那个，我目前肯定是更建议东风这边的，因为它的班型更稳，"
        "而且今年的签约情况也比较明朗，家长可以重点留意一下它的开放日安排。"
    )
    texts = iter([
        "抱歉，我无法访问您提到的音频文件 `[attachment audio/wav]`。",
        good,
    ])

    monkeypatch.setattr(g, "_call_once", lambda pcm: next(texts))
    audio = np.zeros(16000 * 60, dtype=np.float32)
    out = g.transcribe(audio, 16000)

    assert g._bad_detected == 1
    assert g._recovered == 1
    assert not g._failed_chunks, "救回了就不该记失败"
    assert "西雅的岭南" in out[0]["text"], "救回的内容没被采用"


def test_hallucination_bisects_when_retry_also_fails(monkeypatch):
    """两次都幻觉 → 二分重切，且二分后能拿到真实内容。"""
    g = GeminiASR(model="dummy")
    calls = {"n": 0}

    def fake_call(pcm):
        calls["n"] += 1
        # 前两次（整块 + 重试）幻觉；二分后的子块正常
        if len(pcm) > 16000 * 60:
            return "抱歉，我无法访问您提到的音频文件 `[attachment audio/wav]`。"
        return "这是二分后拿到的正常内容" + "填充文字" * 12

    monkeypatch.setattr(g, "_call_once", fake_call)
    audio = np.zeros(16000 * 110, dtype=np.float32)
    out = g.transcribe(audio, 16000)

    assert calls["n"] > 2, "应该发生二分重切"
    assert not g._failed_chunks, "二分救回了，不应记失败"
    assert all("attachment" not in s["text"] for s in out), "幻觉仍进了正文"


def test_placeholder_when_recovery_exhausted(monkeypatch):
    """怎么都救不回来 → 落可见占位，不静默丢弃。"""
    g = GeminiASR(model="dummy")
    monkeypatch.setattr(
        g, "_call_once",
        lambda pcm: "抱歉，我无法访问您提到的音频文件 `[attachment audio/wav]`。",
    )
    # 30 秒块 < MIN_SPLIT_SECONDS，不会二分 → 直接落占位
    audio = np.zeros(16000 * 30, dtype=np.float32)
    out = g.transcribe(audio, 16000)

    assert len(out) == 1
    assert "转写失败" in out[0]["text"], f"应落占位，实际: {out[0]['text']}"
    assert g._failed_chunks, "失败块要记账"


def test_placeholder_covers_whole_chunk_not_fragments(monkeypatch):
    """救不回来时落**一个覆盖整块**的占位，而不是一堆碎片占位。

    否则 1 个 110 秒缺口会被渲染成 7 个 15 秒占位，读者无法判断缺口有多大。
    """
    g = GeminiASR(model="dummy")
    monkeypatch.setattr(
        g, "_call_once",
        lambda pcm: "抱歉，我无法访问您提到的音频文件 `[attachment audio/wav]`。",
    )
    audio = np.zeros(16000 * 110, dtype=np.float32)
    out = g.transcribe(audio, 16000)

    assert len(out) == 1, f"应只落 1 个占位，实际 {len(out)} 个: {[x['text'][:20] for x in out]}"
    assert "转写失败" in out[0]["text"]
    assert "110 秒" in out[0]["text"], "占位应覆盖整块时长"
    # 失败区间也是整块，不是碎片
    assert len(g._failed_chunks) == 1
    assert g._failed_chunks[0] == (0.0, 110.0)


def test_recovery_offsets_boundary_for_normal_audio(monkeypatch):
    """边界块被判异常但内容其实正常时，切分后必须能救回来。

    真实案例：越秀区那份录音 1540s 处的 56 秒音频 RMS 0.132、内容正常，
    旧逻辑因碎片跌破 40s 门槛而直接放弃，白丢内容。
    """
    g = GeminiASR(model="dummy")
    seen_sizes = []
    good = (
        "这一段是主播正常在讲的内容，学校名字和班型都说得很清楚，"
        "没有任何异常，长度也符合真实直播的语速密度，所以应该被正常接受。"
    )

    def fake_call(pcm):
        seen_sizes.append(len(pcm))
        # 第一次整块调用来一次幻觉，逼出切分路径；之后都正常
        if len(seen_sizes) == 1:
            return "抱歉，我无法访问您提到的音频文件 `[attachment audio/wav]`。"
        return good

    monkeypatch.setattr(g, "_call_once", fake_call)
    audio = np.zeros(16000 * 110, dtype=np.float32)
    out = g.transcribe(audio, 16000)

    assert not g._failed_chunks, "内容正常时应通过切分救回，不该记失败"
    assert all("attachment" not in s["text"] for s in out)


def test_short_output_not_bisected(monkeypatch):
    """长度异常只重试、不二分——避免把真实短内容切碎成幻觉。"""
    g = GeminiASR(model="dummy")
    monkeypatch.setattr(g, "_call_once", lambda pcm: "嗯。")
    audio = np.zeros(16000 * 110, dtype=np.float32)
    out = g.transcribe(audio, 16000)

    # 没有切碎成很多段，只是原样重试一次
    assert len(out) == 1
    assert out[0]["text"] == "嗯。", "短内容应照实保留，不能被丢弃"
    assert g._suspect_chunks, "短得可疑要记账（但不落占位）"
    assert not g._failed_chunks, "短内容不算失败"


def test_ts_source_marks_estimated(monkeypatch):
    """句级时间戳是估算的，必须显式标注，不能让下游当精确值。"""
    g = GeminiASR(model="dummy")
    monkeypatch.setattr(g, "_call_once", lambda pcm: "第一句话够长的内容\n第二句话也够长的内容")
    audio = np.zeros(16000 * 60, dtype=np.float32)
    out = g.transcribe(audio, 16000)

    assert out
    assert all(s["ts_source"] == "estimated" for s in out)
