"""GUI 支撑模块测试（不启动窗口，只测逻辑）。

覆盖：配置持久化、模型发现与合并、后处理容错。
"""

from __future__ import annotations

import json

import pytest

from wxlive import model_discovery, postprocess, userconfig


# ---------------- 配置持久化 ----------------

def test_default_config_has_all_sections():
    d = userconfig.default_config_dict()
    for sec in ("audio", "vad", "asr", "output"):
        assert sec in d, f"缺少配置段 {sec}"


def test_save_and_load_roundtrip(tmp_path, monkeypatch):
    p = tmp_path / "user.json"
    monkeypatch.setattr(userconfig, "USER_CONFIG_PATH", p)

    userconfig.save_user_config({
        "asr": {"engine": "local", "gemini_model": "test/model",
                "gemini_api_key": "secret-key"},
        "vad": {"cloud_min_segment_seconds": 12.0},
    })
    loaded = userconfig.load_user_config()
    assert loaded["asr"]["engine"] == "local"
    assert loaded["vad"]["cloud_min_segment_seconds"] == 12.0


def test_only_whitelisted_keys_saved(tmp_path, monkeypatch):
    """白名单之外的字段不应写盘，避免把内部状态存进配置。"""
    p = tmp_path / "user.json"
    monkeypatch.setattr(userconfig, "USER_CONFIG_PATH", p)

    userconfig.save_user_config({
        "asr": {"engine": "auto", "some_internal_flag": True},
    })
    raw = json.loads(p.read_text(encoding="utf-8"))
    assert "some_internal_flag" not in raw["asr"]


def test_corrupted_config_falls_back_to_default(tmp_path, monkeypatch):
    p = tmp_path / "user.json"
    p.write_text("{ this is not json", encoding="utf-8")
    monkeypatch.setattr(userconfig, "USER_CONFIG_PATH", p)

    loaded = userconfig.load_user_config()
    assert loaded == {}, "损坏的配置应回退为空（用默认值），不能抛异常"


def test_build_config_applies_user_values(tmp_path, monkeypatch):
    p = tmp_path / "user.json"
    p.write_text(json.dumps({
        "asr": {"engine": "local", "gemini_model": "my/model"},
        "vad": {"min_silence_seconds": 1.1},
    }, ensure_ascii=False), encoding="utf-8")
    monkeypatch.setattr(userconfig, "USER_CONFIG_PATH", p)

    cfg = userconfig.build_config_from_user()
    assert cfg.asr.engine == "local"
    assert cfg.asr.gemini_model == "my/model"
    assert cfg.vad.min_silence_seconds == 1.1
    # 未覆盖的字段保持默认
    assert cfg.asr.language == "zh"


# ---------------- 模型发现 ----------------

def test_merge_asr_options_keeps_builtin_first():
    opts = model_discovery.merge_asr_options([])
    ids = [o[0] for o in opts]
    assert ids[0] == "google-vertex/gemini-4-hydrogen", "默认推荐应是 hydrogen"
    assert "tiny" in ids and "small" in ids, "本地 whisper 必须在列表里"


def test_merge_asr_options_adds_discovered_gemini():
    opts = model_discovery.merge_asr_options(["google-vertex/gemini-9-new"])
    ids = [o[0] for o in opts]
    assert "google-vertex/gemini-9-new" in ids


def test_merge_asr_options_ignores_non_asr_models():
    """非 gemini 的聊天模型不能混进转写候选。"""
    opts = model_discovery.merge_asr_options(["anyrouter/claude-opus-5-5"])
    ids = [o[0] for o in opts]
    assert "anyrouter/claude-opus-5-5" not in ids


def test_merge_text_options_includes_deepseek():
    opts = model_discovery.merge_text_options([])
    ids = [o[0] for o in opts]
    assert "workbuddy/deepseek-v4.1-flash" in ids


def test_merge_options_deduplicates():
    opts = model_discovery.merge_asr_options(
        ["google-vertex/gemini-4-hydrogen", "google-vertex/gemini-4-hydrogen"])
    ids = [o[0] for o in opts]
    assert len(ids) == len(set(ids)), "结果含重复项"


def test_fetch_models_failure_returns_empty():
    """连不上网关时必须返回空列表，不能抛异常阻断 GUI。"""
    got = model_discovery.fetch_models("http://127.0.0.1:9/v1", timeout=1)
    assert got == []


# ---------------- 后处理 ----------------

def test_fix_errors_empty_input():
    assert postprocess.fix_errors([]) == []


def test_fix_errors_keeps_original_on_failure(monkeypatch):
    """LLM 调用失败时必须保留原文，不能让后处理把数据搞丢。"""
    def boom(*a, **k):
        raise RuntimeError("模拟网络失败")

    monkeypatch.setattr(postprocess, "_call", boom)
    lines = ["第一句话", "第二句话"]
    out = postprocess.fix_errors(lines)
    assert out == lines, "失败时应原样返回"


def test_fix_errors_rejects_mismatched_line_count(monkeypatch):
    """LLM 返回行数不对时必须丢弃该批结果，否则会错位。"""
    monkeypatch.setattr(postprocess, "_call", lambda *a, **k: "只有一行")
    lines = ["句子一", "句子二", "句子三"]
    out = postprocess.fix_errors(lines, batch_size=10)
    assert out == lines, "行数不匹配应保留原文"


def test_fix_errors_accepts_matching_result(monkeypatch):
    monkeypatch.setattr(postprocess, "_call", lambda *a, **k: "修正一\n修正二")
    out = postprocess.fix_errors(["原一", "原二"], batch_size=10)
    assert out == ["修正一", "修正二"]


def test_build_digest_empty():
    assert postprocess.build_digest([]) == ""


def test_build_digest_survives_llm_failure(monkeypatch):
    def boom(*a, **k):
        raise RuntimeError("超时")

    monkeypatch.setattr(postprocess, "_call", boom)
    segs = [{"start": 0, "end": 1, "text": "测试"}]
    out = postprocess.build_digest(segs)
    assert "失败" in out, "摘要失败应给出提示而不是抛异常"
