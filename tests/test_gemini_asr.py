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

    def fake_call(pcm):
        calls.append(len(pcm))
        return "测试一句"

    monkeypatch.setattr(g, "_call_once", fake_call)
    # 250 秒音频 → 应切成 3 片（110 + 110 + 30）
    audio = np.zeros(16000 * 250, dtype=np.float32)
    out = g.transcribe(audio, 16000)

    assert len(calls) == 3, f"应切 3 片，实际 {len(calls)} 片: {calls}"
    assert all(c <= 16000 * 110 for c in calls), "单片的长度超过 110s 上限"
    assert len(out) == 3, "每片应产出一句"


def test_timestamps_are_monotonic(monkeypatch):
    g = GeminiASR(model="dummy")
    monkeypatch.setattr(g, "_call_once", lambda pcm: "第一句话内容\n第二句话内容更长一些")
    audio = np.zeros(16000 * 200, dtype=np.float32)
    out = g.transcribe(audio, 16000)

    assert len(out) == 4  # 2 片 × 每片 2 句
    prev = -1.0
    for seg in out:
        assert seg["start"] >= prev - 1e-6, "时间戳出现倒序"
        assert seg["end"] > seg["start"]
        prev = seg["end"]
