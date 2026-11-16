"""回归：句级时间戳必须标注可信度（ts_source）。

背景（2026-11-16 实测）：Gemini 不返回逐句时间戳，我们按字数比例摊。
用 faster-whisper 真实对齐做基准，110 秒块内起点绝对偏差
中位 19.3s、平均 23.9s、最大 59.0s。

所以句级时间戳是**估算值**，必须显式标注，不能让它冒充精确。
块起点（VAD / 切片 offset）来自音频实际位置，是可信的，标 exact。
"""

from __future__ import annotations

from pathlib import Path

import numpy as np

from wxlive.session import Session


def _sess(tmp_path: Path) -> Session:
    return Session(tmp_path, name="t", sample_rate=16000, record_audio=False)


def test_estimated_ts_source_is_preserved(tmp_path):
    """云端引擎标 estimated 的要原样落进 json。"""
    s = _sess(tmp_path)
    s.add_segments([
        {"start": 0.0, "end": 2.0, "text": "一", "ts_source": "estimated"},
        {"start": 2.0, "end": 4.0, "text": "二", "ts_source": "estimated"},
    ])
    doc = s.close()

    assert all(x["ts_source"] == "estimated" for x in doc["segments"])


def test_missing_ts_source_defaults_to_exact(tmp_path):
    """本地 whisper 有真实对齐，不传就是 exact（保持向后兼容）。"""
    s = _sess(tmp_path)
    s.add_segments([{"start": 0.0, "end": 2.0, "text": "一"}])
    doc = s.close()

    assert doc["segments"][0]["ts_source"] == "exact"


def test_ts_source_survives_jsonl(tmp_path):
    """jsonl 也要带上，断点续转时不能丢这个字段。"""
    import json

    s = _sess(tmp_path)
    s.add_segments([{"start": 0.0, "end": 2.0, "text": "一", "ts_source": "estimated"}])
    jsonl = s.jsonl_path.read_text(encoding="utf-8").strip()
    s.close()

    rec = json.loads(jsonl.splitlines()[0])
    assert rec["ts_source"] == "estimated"


def test_placeholder_text_is_stored_verbatim(tmp_path):
    """转写失败占位要原样落盘，方便读者立刻发现缺口。"""
    from wxlive.quality import placeholder_text

    s = _sess(tmp_path)
    p = placeholder_text(0, 110)
    s.add_segments([
        {"start": 0.0, "end": 110.0, "text": p, "ts_source": "estimated"},
    ])
    doc = s.close()

    assert "转写失败" in doc["segments"][0]["text"]
    assert "110 秒" in doc["segments"][0]["text"]
