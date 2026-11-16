"""质量检查模块的单元测试（wxlive/quality.py）。

这一层的价值全在"判得准"：漏判会让幻觉进正文，误判会丢掉主播真实说的话。
所以这里既测"该抓的抓到"，也测"不该抓的别碰"——后者更重要。
"""

from __future__ import annotations

import pytest

from wxlive import quality

# 真实产物里出现过的幻觉原文（2026-11-16，可追溯）
REAL_HALLUCINATIONS = [
    "抱歉，我无法访问或接收您提到的音频文件 `[attachment audio/wav]`，"
    "当前对话中未包含实际音频内容。",
    "未收到有效的音频文件，仅有 `[attachment audio/wav]` 占位文本，无法进行转写。",
    "无法完成转写，当前会话中仅包含 `[attachment audio/wav]` 的文字占位。",
    "无法完成转写。当前会话中仅包含 `[attachment audio/wav]` 的文字占位，"
    "并且我当前没有可读取音频文件的工具。",
    "抱歉，当前会话中未包含可访问的音频文件，只显示了 `[attachment audio/wav]` "
    "占位文本，我无法提取音频内容进行转写。",
]

# 绝对不能被误杀的句子：升学直播里真的会出现这些说法
NORMAL_SPEECH = [
    "那个，东风实验的青云A班，应该它的重点班是吧？",
    "我无法理解家长为什么这么执着于这个学校。",
    "这个音频设备好像有点问题，刚才声音小了一点。",
    "没有这个学校，我说的是白云区那一所。",
    "好，下一个问题。西雅的岭南怎么选？",
    "我无法转写这个概念，但是可以给你讲讲大概思路。",  # 含"无法"+"转写"但不匹配
    "文件我发群里了，你自己看一下。",
    "抱歉，刚才说错了，是民办不是公办。",
]


@pytest.mark.parametrize("text", REAL_HALLUCINATIONS)
def test_real_hallucinations_are_detected(text):
    assert quality.is_hallucination(text), f"漏判真实幻觉: {text[:40]}"


@pytest.mark.parametrize("text", NORMAL_SPEECH)
def test_normal_speech_is_not_flagged(text):
    """误杀比漏判更糟——会丢掉主播真实说过的话。"""
    assert not quality.is_hallucination(text), f"误杀正常口播: {text}"


def test_empty_text_is_not_hallucination():
    assert not quality.is_hallucination("")
    assert not quality.is_hallucination(None)  # type: ignore[arg-type]


def test_looks_truncated_detects_density_mismatch():
    """110 秒块只出 86 字（0.8 字/秒）——这是幻觉块的典型特征。"""
    assert quality.looks_truncated("很短" * 43, 110.0)


def test_looks_truncated_accepts_normal_density():
    """真实直播 110s 块 580~670 字（约 5.5 字/秒），不能触发。"""
    assert not quality.looks_truncated("正常内容" * 145, 110.0)


def test_looks_truncated_ignores_short_blocks():
    """30 秒以下的块没有统计意义，不该判断。"""
    assert not quality.looks_truncated("嗯。", 10.0)


def test_looks_truncated_handles_zero_duration():
    assert not quality.looks_truncated("内容", 0.0)
    assert not quality.looks_truncated("内容", -5.0)


def test_bad_output_combines_both_signals():
    assert quality.is_bad_output("抱歉，我无法访问音频文件。", 110.0)
    assert quality.is_bad_output("短", 110.0)
    assert not quality.is_bad_output("正常内容" * 145, 110.0)


def test_placeholder_format():
    p = quality.placeholder_text(1310.0, 1420.0)
    assert "转写失败" in p
    assert "00:21:50" in p and "00:23:40" in p
    assert "110 秒" in p


def test_placeholder_under_one_hour():
    p = quality.placeholder_text(65.0, 125.0)
    assert "01:05" in p and "02:05" in p


def test_placeholder_is_stable_for_word_lexicon():
    """占位文案不能被词表改坏（实测过一遍，这里锁死）。"""
    from wxlive import lexicon

    p = quality.placeholder_text(1310.0, 1420.0)
    assert lexicon.expand_codes(p, keep_original=True) == p
    assert lexicon.correct_text(p) == p
