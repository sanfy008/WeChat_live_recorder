"""文本后处理可追溯性 + 配置统一 + 产物版本化。

覆盖：
1. fix_errors / build_digest 的统计能落进 meta.text
2. 老 config/user.json 没有 text 段时可正常加载默认值
3. 非法 reasoning_effort 被显式拒绝（不静默接受）
4. 转写产物按音频 stem 建子目录、带参数指纹，旧扁平产物不受影响
"""

from __future__ import annotations

import hashlib
import json
import re

import pytest

from wxlive import config, postprocess, userconfig
from wxlive.session import Session


def _fake_call(model, prompt, endpoint, api_key, **kwargs):
    body = prompt.split("待修正：\n", 1)[1]
    lines = [re.sub(r"^\d+\. ", "", ln) for ln in body.split("\n") if ln.strip()]
    return "\n".join(f"已修正-{ln}" for ln in lines)


# ---------------- 1. 统计落进 meta ----------------

def test_fix_stats_counts_retries_and_failed_batches(monkeypatch):
    """一批重试后恢复、另一批重试用尽：retry_count 与 failed_batches 都要准。"""
    seen = {"b2": 0}

    def call(model, prompt, *a, **k):
        if "原句2" in prompt:
            seen["b2"] += 1
            if seen["b2"] == 1:
                return "只有一行"          # 第 1 次行数不符
        if "原句4" in prompt:
            raise OSError("HTTP Error 403: Forbidden")
        return _fake_call(model, prompt, *a, **k)

    monkeypatch.setattr(postprocess, "_call", call)
    lines = [f"原句{i}" for i in range(6)]
    stats: dict = {}

    out = postprocess.fix_errors(lines, batch_size=2, workers=1,
                                 retry_delays=(0.0, 0.0), out_stats=stats)

    assert len(out) == 6
    assert stats["model"] == postprocess.DEFAULT_MODEL
    assert stats["reasoning_effort"] == "low"
    assert stats["batches"] == 3
    assert stats["workers"] == 1
    # 第 2 批重试 1 次成功；第 3 批重试 2 次后用尽
    assert stats["retry_count"] == 3
    assert stats["failed_batches"] == [3]
    assert out == [
        "已修正-原句0", "已修正-原句1",
        "已修正-原句2", "已修正-原句3",
        "原句4", "原句5",
    ]


def test_fix_stats_empty_input(monkeypatch):
    stats: dict = {}
    assert postprocess.fix_errors([], out_stats=stats) == []
    assert stats["batches"] == 0
    assert stats["failed_batches"] == []


def test_digest_stats_records_model_effort_seconds(monkeypatch):
    monkeypatch.setattr(postprocess, "_call", lambda *a, **k: "摘要结果")
    stats: dict = {}
    segs = [{"start": 0, "end": 5, "text": "第一句"}]

    out = postprocess.build_digest(segs, model="test/model",
                                   reasoning_effort="high", out_stats=stats)

    assert out == "摘要结果"
    assert stats["model"] == "test/model"
    assert stats["reasoning_effort"] == "high"
    assert stats["ok"] is True
    assert isinstance(stats["seconds"], float) and stats["seconds"] >= 0


def test_digest_stats_records_failure(monkeypatch):
    def boom(*a, **k):
        raise RuntimeError("超时")

    monkeypatch.setattr(postprocess, "_call", boom)
    stats: dict = {}
    out = postprocess.build_digest([{"start": 0, "end": 1, "text": "x"}],
                                   out_stats=stats)
    assert "失败" in out
    assert stats["ok"] is False
    assert "超时" in stats["error"]


def test_session_meta_contains_text_section(tmp_path):
    s = Session(tmp_path, name="t", record_audio=False)
    s.add_segments([{"start": 0.0, "end": 1.0, "text": "一句"}])
    text = {
        "model": postprocess.DEFAULT_MODEL,
        "reasoning_effort": "low",
        "fix": {"batches": 27, "retry_count": 2, "failed_batches": []},
        "digest": {"model": postprocess.DEFAULT_MODEL,
                   "reasoning_effort": "low", "seconds": 49.0},
    }
    doc = s.close(extra_meta={"text": text})
    assert doc["meta"]["text"] == text


# ---------------- 2. 默认模型与向后兼容 ----------------

def test_default_text_model_is_stable_flash():
    assert config.DEFAULT_TEXT_MODEL == "workbuddy/deepseek-v4.1-flash"
    assert postprocess.DEFAULT_MODEL == config.DEFAULT_TEXT_MODEL


def test_old_user_config_without_text_section(tmp_path, monkeypatch):
    """老 user.json 没有 text 段：不能崩，必须用出厂默认值。"""
    p = tmp_path / "user.json"
    p.write_text(json.dumps({
        "asr": {"engine": "local"},
        "vad": {"min_silence_seconds": 1.1},
    }, ensure_ascii=False), encoding="utf-8")
    monkeypatch.setattr(userconfig, "USER_CONFIG_PATH", p)

    cfg = userconfig.build_config_from_user()
    assert cfg.asr.engine == "local"
    assert cfg.text.model == config.DEFAULT_TEXT_MODEL
    assert cfg.text.reasoning_effort == config.DEFAULT_REASONING_EFFORT


def test_text_section_roundtrip_and_whitelist(tmp_path, monkeypatch):
    p = tmp_path / "user.json"
    monkeypatch.setattr(userconfig, "USER_CONFIG_PATH", p)

    userconfig.save_user_config({
        "text": {"model": "some/model", "reasoning_effort": "high",
                 "internal_flag": True},
    })
    raw = json.loads(p.read_text(encoding="utf-8"))
    assert raw["text"] == {"model": "some/model", "reasoning_effort": "high"}
    assert "internal_flag" not in raw["text"]

    cfg = userconfig.build_config_from_user()
    assert cfg.text.model == "some/model"
    assert cfg.text.reasoning_effort == "high"


def test_build_config_overwrites_stale_text_values(tmp_path, monkeypatch):
    """配置里的值必须覆盖 Config 对象里的旧值，而不是被默认值盖回去。"""
    p = tmp_path / "user.json"
    p.write_text(json.dumps({"text": {"model": "saved/model",
                                      "reasoning_effort": "max"}}),
                 encoding="utf-8")
    monkeypatch.setattr(userconfig, "USER_CONFIG_PATH", p)
    cfg = userconfig.build_config_from_user()
    assert cfg.text.model == "saved/model"
    assert cfg.text.reasoning_effort == "max"


# ---------------- 3. 非法思考档位被拒 ----------------

@pytest.mark.parametrize("bad", ["medium", "", "LOWW", "0"])
def test_validate_reasoning_effort_rejects(bad):
    with pytest.raises(ValueError):
        config.validate_reasoning_effort(bad)


@pytest.mark.parametrize("ok", list(config.VALID_REASONING_EFFORTS))
def test_validate_reasoning_effort_accepts_legal_ones(ok):
    assert config.validate_reasoning_effort(ok) == ok
    assert config.validate_reasoning_effort(ok.upper()) == ok


def test_fix_errors_rejects_illegal_effort():
    with pytest.raises(ValueError):
        postprocess.fix_errors(["一句"], reasoning_effort="medium")


def test_build_digest_rejects_illegal_effort():
    with pytest.raises(ValueError):
        postprocess.build_digest([{"start": 0, "end": 1, "text": "x"}],
                                 reasoning_effort="medium")


def test_reasoning_effort_is_forwarded_to_gateway(monkeypatch):
    captured = {}

    def spy(model, prompt, endpoint, api_key, **kwargs):
        captured.update(kwargs)
        return "修正"

    monkeypatch.setattr(postprocess, "_call", spy)
    postprocess.fix_errors(["原一"], workers=1, reasoning_effort="high")
    assert captured["reasoning_effort"] == "high"


# ---------------- 4. 产物版本化 ----------------

def test_transcribe_artifacts_use_stem_dir_and_fingerprint(tmp_path):
    audio = tmp_path / "src" / "talk.wav"
    audio.parent.mkdir()
    audio.write_bytes(b"fake-audio-bytes")
    old = tmp_path / "20250101_000000_talk.json"
    old.write_text(json.dumps({
        "session_id": "20250101_000000_talk",
        "started_at": "2025-01-01T00:00:00",
        # 老产物的 audio_file 在顶层，meta 里没有 audio_sha1
        "audio_file": "talk.wav",
        "meta": {},
    }), encoding="utf-8")

    s = Session(tmp_path, name="talk", record_audio=False,
                audio_stem="talk", fingerprint="w12_low", audio_path=audio)
    s.add_segments([{"start": 0.0, "end": 1.0, "text": "一句"}])
    doc = s.close()

    assert s.stem_dir == tmp_path / "talk"
    assert s.json_path.parent == tmp_path / "talk"
    assert s.json_path.name.endswith("_w12_low.json")
    assert s.md_path.name.endswith("_w12_low.md")
    assert s.jsonl_path.name.endswith("_w12_low.jsonl")

    meta = doc["meta"]
    assert meta["audio_sha1"] == hashlib.sha1(b"fake-audio-bytes").hexdigest()
    assert meta["audio_bytes"] == len(b"fake-audio-bytes")
    assert "audio_mtime" in meta
    assert meta["rerun_of"] == "20250101_000000_talk"

    # 旧扁平产物必须原地保留
    assert old.exists()
    assert old.read_text(encoding="utf-8")


def test_second_run_of_same_audio_records_rerun_of(tmp_path):
    audio = tmp_path / "talk.wav"
    audio.write_bytes(b"same-audio")

    first = Session(tmp_path, name="talk", record_audio=False,
                    audio_stem="talk", fingerprint="w12_low", audio_path=audio)
    first.add_segments([{"start": 0.0, "end": 1.0, "text": "一"}])
    first_doc = first.close()

    second = Session(tmp_path, name="talk", record_audio=False,
                     audio_stem="talk", fingerprint="w12_high", audio_path=audio)
    second.add_segments([{"start": 0.0, "end": 1.0, "text": "一"}])
    second_doc = second.close()

    assert second_doc["meta"]["rerun_of"] == first_doc["session_id"]
    assert first.json_path.exists(), "旧产物不能被移动或覆盖"


def test_flat_session_layout_unchanged(tmp_path):
    """不传 audio_stem 时保持旧布局（录制模式 / 既有测试依赖）。"""
    s = Session(tmp_path, name="wechat_live", record_audio=False)
    assert s.stem_dir == tmp_path
    assert s.json_path.parent == tmp_path
    assert s.json_path.name.startswith(s.started_at.strftime("%Y%m%d_%H%M%S"))
