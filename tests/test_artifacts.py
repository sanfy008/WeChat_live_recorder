"""产物自检的测试。

## 这些测试为什么存在

2026-11-26 复盘 7 份真实产物时抓到两类缺陷，旧验收（行数守恒 / 失败批 0 /
三份产物齐全）一条都查不出来：

1. 某场终稿有 574 句繁体（`to_simplified` 的兜底表缺 7 个高频繁体字）
2. 两场的 Gemini 幻觉句（「我无法访问音频」）进了终稿，还被 LLM 改通顺了

测试要锁住两件事：**能检出真缺陷**、**不误报干净文本**。
只测前者会写出一个「什么都报」的检测器，那个等于没有。
"""

from __future__ import annotations

import pytest

from wxlive.artifacts import check_segments, is_leaked, is_trad_residual
from wxlive.lexicon import to_simplified


# ---------------- 繁简残留 ----------------


@pytest.mark.parametrize("text", [
    "嗯好的就是一個信息的分享嘛",
    "就是給大家去做一個補齊信息差的一個動作啊",
    "好 進階老師怎麼聯繫",
    "所以呢下面我們來感受一下啊",
    "聊小聲出這個號",
])
def test_detects_traditional(text: str) -> None:
    """真实产物里出现过的繁体句必须检出。"""
    assert is_trad_residual(text) is True


@pytest.mark.parametrize("text", [
    "",
    "这是简体文本",
    "普通的句子",
    "年级老师和家长联系",       # 含「级师联」的简体，不能误报
    "小升初的寒假冲刺班",
    "2026年广州中考作文",
])
def test_does_not_flag_simplified(text: str) -> None:
    """简体文本一句都不能误报 —— 否则这个检查会被忽略掉。"""
    assert is_trad_residual(text) is False


def test_tc2sc_covers_measured_gap() -> None:
    """兜底表必须覆盖 2026-11-26 复盘测出的 7 个缺失字。

    这 7 个字在 7 份产物里共出现 186 次，且 to_simplified 原先全部修不掉。
    如果谁把表改回去，这条测试会立刻失败。
    """
    pairs = {
        "級": "级", "師": "师", "階": "阶", "體": "体",
        "聯": "联", "繫": "系", "韓": "韩",
    }
    for trad, simp in pairs.items():
        assert to_simplified(trad) == simp, f"{trad} 应转为 {simp}"


def test_simplified_text_is_idempotent() -> None:
    """对简体文本反复转换必须不变 —— 防止新加的字误伤正常文本。"""
    text = "年级老师和家长联系，寒假冲刺班报名"
    once = to_simplified(text)
    assert once == text
    assert to_simplified(once) == once


# ---------------- 幻觉残留 ----------------


@pytest.mark.parametrize("text", [
    "未检测到音频附件（仅收到 `[attachment audio/wav]` 文本占位符），无法进行音频转写。",
    "无法完成转写。当前会话中未包含可读取的音频文件（仅出现文本标记 `[attachment audio/wav]`）。",
    "我无法访问音频文件",
    "抱歉，我无法读取这段录音",
    "请先上传音频文件",
    "当前没有可用的音频输入",
    "As an AI, I cannot access the audio.",
])
def test_detects_hallucination_leak(text: str) -> None:
    assert is_leaked(text) is True


@pytest.mark.parametrize("text", [
    "",
    "学生要提高阅读速度，抓同义替换。",
    "这个文件很多家长都在等",
    "音频质量不错，听起来很清楚",
    "我们联系了老师",
])
def test_does_not_flag_normal_speech(text: str) -> None:
    """正常口播里出现「音频」「文件」「联系」都不该报。"""
    assert is_leaked(text) is False


# ---------------- 整体体检 ----------------


def _segs(*texts: str) -> list[dict]:
    return [{"start": i * 2.0, "end": i * 2.0 + 2.0, "text": t}
            for i, t in enumerate(texts)]


def test_clean_product_passes() -> None:
    rep = check_segments(_segs("今天讲小升初", "天河岗顶线下课", "寒假冲刺班"))
    assert rep.ok is True
    assert rep.trad_residual == 0
    assert rep.hallucination_residual == 0
    assert "通过" in rep.summary()


def test_flags_traditional_residual() -> None:
    rep = check_segments(_segs("今天讲小升初", "好 進階老師怎麼聯繫"))
    assert rep.ok is False
    assert rep.trad_residual == 1
    assert "trad_residual" in rep.samples


def test_flags_hallucination_residual() -> None:
    rep = check_segments(_segs("正常一句", "我无法访问音频文件"))
    assert rep.ok is False
    assert rep.hallucination_residual == 1
    assert "hallucination_residual" in rep.samples


def test_counts_both_kinds_separately() -> None:
    rep = check_segments(_segs("好 進階老師怎麼聯繫", "我无法访问音频文件", "正常"))
    assert rep.trad_residual == 1
    assert rep.hallucination_residual == 1


def test_empty_text_counted_but_not_fatal() -> None:
    """空文本单独计数，但不影响 ok —— 它是另一个维度的问题。"""
    rep = check_segments(_segs("正常一句", "", "  "))
    assert rep.empty_text == 2
    assert rep.ok is True


def test_meta_is_flat_and_numeric() -> None:
    """写进 meta 的形态要扁平、可跨场次比对。"""
    rep = check_segments(_segs("正常", "好 進階老師怎麼聯繫"))
    meta = rep.to_meta()
    assert meta["segments"] == 2
    assert meta["trad_residual"] == 1
    assert meta["ok"] is False
    assert all(isinstance(v, (int, bool)) for v in meta.values())


def test_report_does_not_mutate_input() -> None:
    """体检只读。改坏入参会让下游写出被污染的数据。"""
    segs = _segs("好 進階老師怎麼聯繫")
    before = [dict(s) for s in segs]
    check_segments(segs)
    assert segs == before


@pytest.mark.parametrize("bad", [None, [], [{}], [{"text": None}]])
def test_handles_degenerate_input(bad) -> None:
    """缺字段 / 空列表不能抛异常 —— 它在收尾路径上，抛了就丢产物。"""
    rep = check_segments(bad or [])
    assert rep.segments == len(bad or [])
