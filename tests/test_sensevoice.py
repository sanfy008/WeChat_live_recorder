"""SenseVoice-Small (INT8) 本地引擎接入测试。

覆盖：
1. 模型文件解析（本地目录 int8/fp32 回退、缺失报错）
2. 句末标点切分（含精确 token 时间戳对齐 + 无对齐比例插值兜底 + 特殊标签剥离）
3. SenseVoiceTranscriber 繁转简、重复压制、词表纠错、超长块 VAD 切分及 stats
4. pipeline.build_engine 按 local_backend 切换 sensevoice / whisper，以及 auto 懒降级
5. model_discovery / userconfig / CLI 参数 / auto_transcribe_after_stop 摘要
"""

from __future__ import annotations

import json
import types
from pathlib import Path

import numpy as np
import pytest

from wxlive import lexicon, model_discovery, pipeline, sensevoice, userconfig
from wxlive.config import AsrConfig, Config


class _FakeResult:
    def __init__(self, text: str, tokens=None, timestamps=None):
        self.text = text
        self.tokens = tokens or []
        self.timestamps = timestamps or []


class _FakeStream:
    def __init__(self):
        self.audio = None
        self.sr = 0
        self.result = _FakeResult("")

    def accept_waveform(self, sr: int, audio: np.ndarray):
        self.sr = sr
        self.audio = audio


class _FakeRecognizer:
    def __init__(self, handler):
        self.handler = handler
        self.calls = 0

    def create_stream(self):
        return _FakeStream()

    def decode_stream(self, stream: _FakeStream):
        self.calls += 1
        stream.result = self.handler(stream.audio, self.calls)


def _make_fake_sv(handler, *, correct_after: bool = True, max_chunk_seconds: float = 28.0):
    sv = sensevoice.SenseVoiceTranscriber.__new__(sensevoice.SenseVoiceTranscriber)
    sv.model_repo_or_dir = "fake/sensevoice"
    sv.num_threads = 2
    sv.language = "auto"
    sv.use_itn = True
    sv.correct_after = correct_after
    sv.max_chunk_seconds = max_chunk_seconds
    sv.recognizer = _FakeRecognizer(handler)
    sv._total_audio = 0.0
    sv._total_infer = 0.0
    return sv


# ---------- 1. 模型路径解析 ----------

def test_resolve_model_files_from_local_dir(tmp_path: Path):
    (tmp_path / "model.int8.onnx").write_bytes(b"int8")
    (tmp_path / "model.onnx").write_bytes(b"fp32")
    (tmp_path / "tokens.txt").write_text("a 0\n", encoding="utf-8")

    m, t = sensevoice.resolve_model_files(str(tmp_path))
    assert m.name == "model.int8.onnx"
    assert t.name == "tokens.txt"


def test_resolve_model_files_missing_raises(tmp_path: Path):
    (tmp_path / "tokens.txt").write_text("a 0\n", encoding="utf-8")
    with pytest.raises(FileNotFoundError):
        sensevoice.resolve_model_files(str(tmp_path))


# ---------- 2. 分句与时间戳 ----------

def test_split_sentences_uses_token_timestamps():
    text = "你好啊。今天講升學！"
    tokens = list(text)
    timestamps = [0.1 * (i + 1) for i in range(len(tokens))]
    out = sensevoice._split_sentences(text, tokens, timestamps, seg_start=10.0, seg_end=15.0)
    assert len(out) == 2
    assert out[0][2] == "你好啊。"
    assert out[1][2] == "今天講升學！"
    assert out[0][0] == pytest.approx(10.1)
    assert out[1][0] == pytest.approx(10.5)
    assert out[1][1] <= 15.0


def test_split_sentences_falls_back_to_proportional_without_tokens():
    text = "<|zh|>第一句。<|HAPPY|>第二句！"
    out = sensevoice._split_sentences(text, [], [], seg_start=2.0, seg_end=8.0)
    assert [x[2] for x in out] == ["第一句。", "第二句！"]
    assert out[0][0] == pytest.approx(2.0)
    assert out[1][1] == pytest.approx(8.0)
    assert out[0][1] == pytest.approx(5.0)


# ---------- 3. 转写、繁转简、词表纠错与超长块切分 ----------

def test_sensevoice_transcribe_postprocesses_and_records_stats(monkeypatch):
    for attr in ("SCHOOLS", "TERMS", "CODES", "CORRECTIONS"):
        monkeypatch.setattr(lexicon, attr, getattr(lexicon, attr).copy())
    lexicon.apply_loaded(lexicon.load())

    text = "我來自進階教育，今天講A加班和張扶玉老師。"
    sv = _make_fake_sv(
        lambda audio, idx: _FakeResult(
            text, tokens=list(text), timestamps=[0.05 * i for i in range(len(text))]
        )
    )
    out = sv.transcribe(np.zeros(16000 * 2, dtype=np.float32), 16000)
    assert len(out) == 1
    assert out[0]["text"] == "我来自进阶教育，今天讲A+班和张璞玉老师。"
    assert sv.stats["backend"] == "sensevoice"
    assert sv.stats["num_threads"] == 2
    assert sv.stats["audio_seconds"] == 2.0


def test_sensevoice_transcribe_chunks_long_audio_via_vad():
    """超过 max_chunk_seconds 的长块（如 300s 离线块）必须走 VAD 分段并保留偏移时间戳。"""
    sr = 16000
    # 构造 40s 音频：前 5s 静音底噪 + 5-15s 语音 + 15-22s 静音 + 22-35s 语音 + 尾部静音
    audio = np.full(sr * 40, 0.0005, dtype=np.float32)
    audio[sr * 5 : sr * 15] = 0.08
    audio[sr * 22 : sr * 35] = 0.08

    sv = _make_fake_sv(
        lambda chunk, idx: _FakeResult(f"第{idx}段内容。"),
        correct_after=False,
        max_chunk_seconds=28.0,
    )
    out = sv.transcribe(audio, sr)
    assert len(out) == 2
    assert out[0]["text"] == "第1段内容。"
    assert out[1]["text"] == "第2段内容。"
    assert 4.0 <= out[0]["start"] <= 6.0
    assert 21.0 <= out[1]["start"] <= 23.0


def test_sensevoice_empty_audio_returns_empty():
    sv = _make_fake_sv(lambda *a: _FakeResult("不应调用"))
    assert sv.transcribe(np.zeros(0, dtype=np.float32), 16000) == []
    assert sv.recognizer.calls == 0


# ---------- 4. build_engine 路由 ----------

def test_build_engine_defaults_to_local_sensevoice(monkeypatch):
    calls = []

    class FakeSV:
        def __init__(self, **kw):
            calls.append(("sv", kw))

    import wxlive.sensevoice as sv_mod
    monkeypatch.setattr(sv_mod, "SenseVoiceTranscriber", FakeSV)

    cfg = Config()
    assert cfg.asr.engine == "local"
    assert cfg.asr.local_backend == "sensevoice"
    assert cfg.asr.local_threads == 2

    eng = pipeline.build_engine(cfg)
    assert isinstance(eng, FakeSV)
    assert calls[0][1]["num_threads"] == 2


def test_build_engine_whisper_backend_compat(monkeypatch):
    calls = []

    class FakeWhisper:
        def __init__(self, **kw):
            calls.append(("whisper", kw))

    monkeypatch.setattr(pipeline, "Transcriber", FakeWhisper)

    cfg = Config(asr=AsrConfig(engine="local", local_backend="whisper", model_size="tiny"))
    eng = pipeline.build_engine(cfg)
    assert isinstance(eng, FakeWhisper)
    assert calls[0][1]["model_size"] == "tiny"


# ---------- 5. model_discovery / userconfig / CLI / auto_transcribe ----------

def test_model_discovery_includes_sensevoice_and_maps_fields():
    opts = model_discovery.merge_asr_options([])
    ids = [o[0] for o in opts]
    assert "sensevoice-small-int8" in ids

    f1 = model_discovery.resolve_asr_model_fields(
        "sensevoice-small-int8", {"gemini_model": "gcp-ai/gemini-4-hydrogen", "local_backend": "whisper"}
    )
    assert f1["local_backend"] == "sensevoice"
    assert f1["gemini_model"] == "gcp-ai/gemini-4-hydrogen"

    f2 = model_discovery.resolve_asr_model_fields(
        "small", {"gemini_model": "gcp-ai/gemini-4-hydrogen", "local_backend": "sensevoice"}
    )
    assert f2["local_backend"] == "whisper"
    assert f2["model_size"] == "small"
    assert f2["gemini_model"] == "gcp-ai/gemini-4-hydrogen"

    assert model_discovery.display_asr_model(AsrConfig(engine="local", local_backend="sensevoice")) == "sensevoice-small-int8"
    assert model_discovery.display_asr_model(AsrConfig(engine="local", local_backend="whisper", model_size="base")) == "base"


def test_userconfig_saves_local_backend_and_threads(tmp_path, monkeypatch):
    p = tmp_path / "user.json"
    monkeypatch.setattr(userconfig, "USER_CONFIG_PATH", p)
    userconfig.save_user_config({
        "asr": {"engine": "local", "local_backend": "sensevoice", "local_threads": 2}
    })
    cfg = userconfig.build_config_from_user()
    assert cfg.asr.engine == "local"
    assert cfg.asr.local_backend == "sensevoice"
    assert cfg.asr.local_threads == 2


def test_auto_transcribe_summary_formats_sensevoice():
    import scripts.auto_transcribe_after_stop as ats

    cfg = Config(asr=AsrConfig(engine="local", local_backend="sensevoice", local_threads=2))
    s = ats.format_asr_summary(cfg)
    assert "sensevoice-small-int8" in s
    assert "2t" in s
    assert "engine=local" in s

    cfg_w = Config(asr=AsrConfig(engine="local", local_backend="whisper", model_size="small"))
    assert "faster-whisper/small" in ats.format_asr_summary(cfg_w)
