"""重复循环压制测试。

whisper 在解码退化时会反复吐同一句，是本项目实测到的真实问题。
但真实口语也常有重复（"对对对"、"好的好的"），不能误伤。
"""

from __future__ import annotations

import pytest

from wxlive.transcriber import _collapse_repeats


def test_repeated_long_phrase_collapsed():
    unit = "第二款是基础款如果单纯是299元其实不太必要去冲"
    text = unit * 3
    out = _collapse_repeats(text)
    assert out == unit, f"未压缩重复，结果: {out[:60]}"


def test_short_colloquial_repeat_preserved():
    """真实口语的短重复必须保留。"""
    for t in ["对对对", "好的好的", "嗯嗯", "是的是的"]:
        assert _collapse_repeats(t) == t


def test_normal_sentence_untouched():
    t = "各位朋友大家好，欢迎来到今天的直播间，今天我给大家介绍三款产品。"
    assert _collapse_repeats(t) == t


def test_two_identical_halves_collapsed():
    unit = "这款产品非常适合日常使用"
    assert _collapse_repeats(unit * 2) == unit


def test_parallel_structure_not_destroyed():
    """排比句（结构相似但内容不同）不能被误删。"""
    t = "第一款是基础款，第二款是进阶款，第三款是旗舰款"
    assert _collapse_repeats(t) == t


def test_empty_and_short_safe():
    assert _collapse_repeats("") == ""
    assert _collapse_repeats("你好") == "你好"
