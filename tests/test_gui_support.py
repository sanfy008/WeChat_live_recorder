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


def test_build_digest_feeds_paragraphs_not_sentences(monkeypatch):
    """摘要输入必须是段落聚合版，而不是逐句 + 句级时间戳。

    实测 81 分钟转录：逐句喂 34205 字符（触发 30000 分块），
    段落聚合喂 26065 字符（不分块）。分块会丢全局视野，是质量损失。
    """
    captured = {}

    def spy(model, prompt, *a, **k):
        captured["prompt"] = prompt
        return "摘要结果"

    monkeypatch.setattr(postprocess, "_call", spy)
    segs = [
        {"start": 0, "end": 5, "text": "第一句话"},
        {"start": 5, "end": 10, "text": "第二句话"},
        {"start": 10, "end": 15, "text": "第三句话"},
    ]
    postprocess.build_digest(segs)

    p = captured["prompt"]
    # 三句应该被聚合成一段（同一话题，无转折词，总长 < 60s）
    assert "第一句话第二句话第三句话" in p, f"未按段落聚合: {p[-200:]}"
    # 时间戳应是段级范围 mm:ss-mm:ss，不是句级 HH:MM:SS
    assert "00:00-00:15" in p, f"段级时间范围缺失: {p[-200:]}"


def test_build_digest_content_not_lost(monkeypatch):
    """聚合不能丢任何一个字——这是硬约束。"""
    captured = {}

    def spy(model, prompt, *a, **k):
        captured["prompt"] = prompt
        return "ok"

    monkeypatch.setattr(postprocess, "_call", spy)
    segs = [{"start": i, "end": i + 1, "text": f"句{i}"} for i in range(20)]
    postprocess.build_digest(segs)

    for i in range(20):
        assert f"句{i}" in captured["prompt"], f"第 {i} 句丢失了"


# ---------------- 系统代理诊断：上游 10808 探活 ----------------

def test_diagnose_flags_dead_upstream_behind_tool(monkeypatch):
    """2023 活着但 10808 挂了：代理链已断，不能假报正常。"""
    from wxlive import systemproxy as sp

    monkeypatch.setattr(sp, "_tcp_alive",
                        lambda host, port, timeout=0.5: port == 2023)
    diag = sp.diagnose({"enabled": True, "server": sp.TOOL_PROXY,
                        "override": ""})

    assert diag.startswith("⚠"), diag
    assert "10808" in diag
    assert "不通" in diag


def test_diagnose_flags_dead_fixed_proxy(monkeypatch):
    """系统代理就是 10808 但没人监听：必须报警。"""
    from wxlive import systemproxy as sp

    monkeypatch.setattr(sp, "_tcp_alive", lambda *a, **k: False)
    diag = sp.diagnose({"enabled": True, "server": sp.FIXED_PROXY,
                        "override": ""})

    assert diag.startswith("⚠"), diag
    assert "10808" in diag


def test_diagnose_ok_when_tool_and_upstream_alive(monkeypatch):
    from wxlive import systemproxy as sp

    monkeypatch.setattr(sp, "_tcp_alive", lambda *a, **k: True)
    diag = sp.diagnose({"enabled": True, "server": sp.TOOL_PROXY,
                        "override": ""})

    assert not diag.startswith("⚠"), diag
    assert "正常" in diag


# ---------------- 直播探测：代理环境剥离与错误分类 ----------------

def _stub_probe(monkeypatch, rc: int, stderr: str = "", stdout: str = ""):
    """替换 ffprobe 的查找与执行，返回捕获到的 kwargs。"""
    import subprocess

    from wxlive import fetch as F

    seen = {"env": None}
    monkeypatch.setattr(F.shutil, "which", lambda name: "ffprobe")

    def fake_run(cmd, **kw):
        seen["env"] = kw.get("env")
        return subprocess.CompletedProcess(cmd, rc, stdout, stderr)

    monkeypatch.setattr(F.winproc, "run", fake_run)
    return seen


def test_probe_stream_strips_proxy_env(monkeypatch):
    """ffprobe 不能继承 http_proxy/https_proxy/all_proxy（大小写都剥）。"""
    monkeypatch.setenv("HTTP_PROXY", "http://127.0.0.1:10808")
    monkeypatch.setenv("https_proxy", "http://127.0.0.1:10808")
    monkeypatch.setenv("ALL_PROXY", "socks5://127.0.0.1:7890")
    monkeypatch.setenv("NO_PROXY", "127.0.0.1")   # 这个不剥，留着无害

    from wxlive import fetch as F

    seen = _stub_probe(monkeypatch, rc=0, stdout="")
    F.probe_stream("http://x/y.flv")

    env = seen["env"]
    assert env is not None, "探测子进程没有传 env，等于继承了代理变量"
    assert "HTTP_PROXY" not in env
    assert "https_proxy" not in env
    assert "ALL_PROXY" not in env
    assert env.get("NO_PROXY") == "127.0.0.1"


def test_probe_stream_403_means_expired(monkeypatch):
    from wxlive import fetch as F

    _stub_probe(monkeypatch, rc=1,
                stderr="http://x/y.flv: Server returned 403 Forbidden")
    ok, why = F.probe_stream("http://x/y.flv")

    assert ok is False
    assert "失效" in why and "下播" in why


def test_probe_stream_network_error_is_not_offline_host(monkeypatch):
    """连接被拒/代理异常必须报网络问题，不能误导成"主播下播"。"""
    from wxlive import fetch as F

    _stub_probe(monkeypatch, rc=1,
                stderr="[http @ 0x1] Connection refused")
    ok, why = F.probe_stream("http://x/y.flv")

    assert ok is False
    assert "网络/代理异常" in why
    assert "不是主播下播" in why


def test_probe_stream_timeout_is_network_class(monkeypatch):
    import subprocess

    from wxlive import fetch as F

    monkeypatch.setattr(F.shutil, "which", lambda name: "ffprobe")

    def boom(cmd, **kw):
        raise subprocess.TimeoutExpired(cmd, 20)

    monkeypatch.setattr(F.winproc, "run", boom)
    ok, why = F.probe_stream("http://x/y.flv", timeout=3)

    assert ok is False
    assert "网络/代理异常" in why


def test_probe_stream_success_detects_audio(monkeypatch):
    from wxlive import fetch as F

    _stub_probe(monkeypatch, rc=0, stdout="codec_type=audio\n")
    ok, why = F.probe_stream("http://x/y.flv")

    assert ok is True and "音轨" in why
