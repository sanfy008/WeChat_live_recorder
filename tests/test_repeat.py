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


def test_transcriber_converts_traditional_and_applies_lexicon(monkeypatch):
    """本地 Transcriber 输出必须自动繁转简并应用领域词表纠错。"""
    import numpy as np
    from wxlive import lexicon
    from wxlive.transcriber import Transcriber

    for attr in ("SCHOOLS", "TERMS", "CODES", "CORRECTIONS"):
        orig = getattr(lexicon, attr)
        monkeypatch.setattr(lexicon, attr, orig.copy())
    lexicon.apply_loaded(lexicon.load())

    class _Seg:
        def __init__(self, start, end, text):
            self.start = start
            self.end = end
            self.text = text
            self.words = []

    class _FakeModel:
        def transcribe(self, *a, **kw):
            return [
                _Seg(0.0, 2.0, "我來自進階教育，今天講一下A加班和張扶玉老師"),
            ], None

    tr = Transcriber.__new__(Transcriber)
    tr.language = "zh"
    tr.beam_size = 5
    tr.vad_filter = True
    tr.initial_prompt = ""
    tr.correct_after = True
    tr._total_audio = 0.0
    tr._total_infer = 0.0
    tr.model = _FakeModel()

    out = tr.transcribe(np.zeros(16000, dtype=np.float32), 16000)
    assert len(out) == 1
    assert out[0]["text"] == "我来自进阶教育，今天讲一下A+班和张璞玉老师"

