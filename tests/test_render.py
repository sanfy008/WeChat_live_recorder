"""段落聚合渲染的测试。

**最重要的约束：不能丢失任何一个字。**
聚合只改变排版，正文必须与逐句版完全一致（拼起来逐字相等）。
"""
import pytest

from wxlive import render


def _seg(start, end, text):
    return {"start": start, "end": end, "text": text}


@pytest.fixture
def sample():
    """模拟一段直播：连续问答。"""
    return [
        _seg(0, 5, "各位家长好，今天我们讲择校。"),
        _seg(5, 12, "先说一下背景。"),
        _seg(12, 20, "有家长问省实南山怎么样。"),
        _seg(20, 30, "南山加工能力很强。"),
        _seg(30, 40, "白省南山也不错。"),
        _seg(40, 50, "下一个问题，金广附和龙高怎么选。"),
        _seg(50, 60, "金广附略卷一点。"),
    ]


# ---- 核心：内容零丢失 ----

def test_no_text_lost(sample):
    """聚合后正文拼接必须与原文逐字相等。"""
    groups = render.group_segments(sample)
    joined = "".join(g["text"] for g in groups)
    original = "".join(s["text"] for s in sample)
    assert joined == original, "聚合过程丢了或改了文字"


def test_all_segments_covered(sample):
    """每句都必须落在某个段落里（count 之和 == 原句数）。"""
    groups = render.group_segments(sample)
    assert sum(g["count"] for g in groups) == len(sample)


def test_empty_input():
    assert render.group_segments([]) == []
    assert render.render_paragraphs([]) == ""


def test_blank_text_skipped():
    segs = [_seg(0, 1, ""), _seg(1, 2, "有内容"), _seg(2, 3, "   ")]
    groups = render.group_segments(segs)
    joined = "".join(g["text"] for g in groups)
    assert joined == "有内容"


# ---- 断段行为 ----

def test_turn_marker_starts_new_paragraph(sample):
    """转折话术应触发断段。样本里有 2 处转折，应产生 3 段。"""
    groups = render.group_segments(sample, min_seconds=0)
    assert len(groups) == 3, [g["text"][:15] for g in groups]
    assert groups[1]["text"].startswith("有家长问")
    assert groups[2]["text"].startswith("下一个问题")


def test_min_seconds_prevents_fragmenting(sample):
    """段太短时即使命中转折也先不断，避免一句一段的碎片。

    样本总长 60s。设 min_seconds=100 时转折信号全部失效，
    但 max_seconds 仍需放宽，否则长度兜底会断（那是另一条规则）。
    """
    groups = render.group_segments(sample, min_seconds=100, max_seconds=9999)
    assert len(groups) == 1, "min_seconds 没起作用，碎成多段"
    assert groups[0]["count"] == len(sample)


def test_max_seconds_forces_break():
    """长时间无转折信号时，长度兜底必须生效。"""
    segs = [_seg(i * 5, (i + 1) * 5, f"第{i}句内容。") for i in range(30)]
    groups = render.group_segments(segs, max_seconds=30, min_seconds=0)
    assert len(groups) >= 4, "长度兜底没生效，出现巨型段落"
    for g in groups:
        assert g["end"] - g["start"] <= 35, f"段落过长: {g['end']-g['start']}"


def test_max_chars_forces_break():
    """字数兜底：句子很长时按字数断，不按时间。"""
    segs = [_seg(i, i + 1, "很长的内容" * 40) for i in range(5)]
    groups = render.group_segments(segs, max_chars=300, min_seconds=0, max_seconds=9999)
    assert len(groups) > 1


def test_marker_mid_sentence_does_not_break():
    """转折词出现在句子中间不算断点（只在句首 12 字内判断）。"""
    segs = [
        _seg(0, 10, "前面讲了很多内容，现在说到有家长问这个问题。"),
        _seg(10, 20, "接着说下去。"),
    ]
    groups = render.group_segments(segs, min_seconds=0)
    assert len(groups) == 1, "句中转折词误触发断段"


# ---- 时间格式 ----

def test_time_format_under_one_hour():
    assert render._fmt_span(0, 65) == "00:00-01:05"


def test_time_format_over_one_hour():
    assert render._fmt_span(3600, 3725) == "1:00:00-1:02:05"


def test_time_format_no_seconds_precision():
    """确认不输出秒级精细时间（那是伪精度）。"""
    s = render._fmt_span(123.456, 200.789)
    assert s == "02:03-03:20"
    assert "." not in s


def test_group_times_are_monotonic(sample):
    groups = render.group_segments(sample, min_seconds=0)
    starts = [g["start"] for g in groups]
    ends = [g["end"] for g in groups]
    assert starts == sorted(starts)
    assert ends == sorted(ends)
    # 相邻段落时间不能重叠
    for a, b in zip(groups, groups[1:]):
        assert a["end"] <= b["start"] + 1e-6


# ---- 渲染输出 ----

def test_render_paragraphs_format(sample):
    txt = render.render_paragraphs(sample, min_seconds=0)
    assert txt.count("**[") == 3
    assert "\n\n" in txt
    # 每段必须以时间范围开头
    for block in txt.split("\n\n"):
        assert block.startswith("**[")


def test_render_keeps_all_text(sample):
    txt = render.render_paragraphs(sample, min_seconds=0)
    for seg in sample:
        assert seg["text"] in txt, f"渲染后丢了: {seg['text']}"


# ---- 统计 ----

def test_stats_reduction(sample):
    groups = render.group_segments(sample, min_seconds=0)
    st = render.stats(sample, groups)
    assert st["segments"] == len(sample)
    assert st["groups"] == len(groups)
    assert st["ts_chars_after"] < st["ts_chars_before"]
    assert st["ts_ratio_after_pct"] < st["ts_ratio_before_pct"]


def test_stats_empty():
    st = render.stats([], [])
    assert st["reduction_pct"] == 0.0
    assert st["ts_ratio_before_pct"] == 0.0


# ---- 真实数据端到端 ----

def test_real_transcript_no_loss():
    """用真实转录验证：聚合后一字不丢。

    这是最关键的一条 —— 改排版绝不能伤内容。
    """
    import json
    from pathlib import Path

    p = Path("output/transcripts/20261116_170553_广州x升初鸡头凤尾如何选.json")
    if not p.exists():
        pytest.skip("真实转录样本不存在")

    d = json.loads(p.read_text(encoding="utf-8"))
    segs = d["segments"]
    groups = render.group_segments(segs)

    joined = "".join(g["text"] for g in groups)
    original = "".join(s["text"] for s in segs)
    assert joined == original, "真实数据聚合后内容不一致"
    assert sum(g["count"] for g in groups) == len(segs)

    # 断段效果：应该大幅减少条目数
    st = render.stats(segs, groups)
    assert st["reduction_pct"] > 70, f"聚合效果不佳: {st['reduction_pct']}%"
    assert st["ts_ratio_after_pct"] < st["ts_ratio_before_pct"]
