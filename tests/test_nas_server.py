"""NAS 服务端（NasJobRunner + FastAPI server + 跨平台兼容）测试。"""

from __future__ import annotations

import io
import json
import time
from pathlib import Path

import numpy as np
import pytest
import soundfile as sf
from fastapi.testclient import TestClient

from wxlive import audio_capture, postprocess
from wxlive.nas_runner import NasJobRunner
from wxlive.server import create_app


class _FakeEngine:
    def __init__(self) -> None:
        self.calls = 0

    def transcribe(self, audio: np.ndarray, sample_rate: int = 16000) -> list[dict]:
        self.calls += 1
        dur = len(audio) / float(sample_rate)
        return [
            {"start": 0.0, "end": round(dur / 2, 2), "text": "今天说说SS和HF的指标到校。"},
            {"start": round(dur / 2, 2), "end": round(dur, 2), "text": "家长要注意时间节点。"},
        ]

    @property
    def stats(self) -> dict:
        return {"backend": "fake", "rtf": 0.05}


def _make_wav_bytes(seconds: float = 2.0, sr: int = 16000) -> bytes:
    buf = io.BytesIO()
    t = np.arange(int(seconds * sr)) / sr
    pcm = (0.15 * np.sin(2 * np.pi * 440 * t)).astype(np.float32)
    sf.write(buf, pcm, sr, format="WAV", subtype="PCM_16")
    return buf.getvalue()


def test_audio_capture_graceful_without_pyaudiowpatch(monkeypatch):
    monkeypatch.setattr(audio_capture, "pyaudio", None)
    with pytest.raises(RuntimeError, match="pyaudiowpatch"):
        audio_capture.list_loopback_devices()
    with pytest.raises(RuntimeError, match="pyaudiowpatch"):
        audio_capture.pick_device("")


def test_runner_recovers_interrupted_jobs_on_startup(tmp_path: Path):
    jobs_file = tmp_path / "jobs.json"
    jobs_file.write_text(
        json.dumps(
            {
                "j1": {"id": "j1", "title": "旧直播", "status": "recording", "logs": []},
                "j2": {"id": "j2", "title": "已完成", "status": "completed", "logs": []},
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    runner = NasJobRunner(data_dir=tmp_path, engine_factory=lambda t, l: _FakeEngine())
    try:
        j1 = runner.get_job("j1")
        j2 = runner.get_job("j2")
        assert j1 is not None and j1["status"] == "interrupted"
        assert j2 is not None and j2["status"] == "completed"
    finally:
        runner.shutdown()


def test_settings_masking_and_persistence(tmp_path: Path):
    runner = NasJobRunner(data_dir=tmp_path, engine_factory=lambda t, l: _FakeEngine())
    try:
        s1 = runner.update_settings(
            {
                "local_threads": 6,
                "enable_llm": True,
                "llm_endpoint": "https://example.com/v1/chat/completions",
                "llm_api_key": "sk-secret-1234567890",
                "llm_reasoning_effort": "low",
            }
        )
        assert s1["local_threads"] == 6
        assert s1["has_api_key"] is True
        assert "1234567890" not in s1["llm_api_key_masked"]
        assert s1["llm_api_key_masked"].startswith("sk-s")

        # 局部更新且不传 api_key，或传掩码时，不覆盖原 key
        s2 = runner.update_settings({"llm_api_key": s1["llm_api_key_masked"], "local_threads": 4})
        assert s2["has_api_key"] is True
        assert s2["local_threads"] == 4

        s3 = runner.update_settings({"clear_api_key": True})
        assert s3["has_api_key"] is False
        assert s3["llm_api_key_masked"] == ""
    finally:
        runner.shutdown()


def test_fastapi_upload_transcribe_download_and_repostprocess(tmp_path: Path, monkeypatch):
    runner = NasJobRunner(data_dir=tmp_path, engine_factory=lambda t, l: _FakeEngine())
    app = create_app(runner=runner)

    try:
        with TestClient(app) as client:
            r_home = client.get("/")
            assert r_home.status_code == 200
            assert "WXLive NAS" in r_home.text

            r_health = client.get("/api/health")
            assert r_health.status_code == 200
            assert r_health.json()["ok"] is True

            # 无效 URL 探测与启动
            r_probe = client.post("/api/probe", json={"url": "not-a-url"})
            assert r_probe.status_code == 200
            assert r_probe.json()["ok"] is False

            r_bad_stream = client.post("/api/jobs/stream", json={"url": "bad"})
            assert r_bad_stream.status_code == 400

            # 整条 ffmpeg 命令应能被识别（微信下载按钮复制出来的就是这个形态）
            r_cmd = client.post(
                "/api/jobs/stream",
                json={"url": 'ffmpeg -i "https://127.0.0.1:9/nope.flv" -c copy -y x.flv'},
            )
            # 地址不可拉会失败，但绝不能因为"看不懂命令"被判 400
            assert r_cmd.status_code in (200, 500), r_cmd.text
            if r_cmd.status_code == 200:
                job = r_cmd.json()
                client.post(f"/api/jobs/{job['id']}/stop",
                            json={"transcribe": False})

            # 上传 WAV 并自动完成假 ASR 转写
            wav_bytes = _make_wav_bytes(seconds=2.0)
            r_up = client.post(
                "/api/jobs/upload",
                files={"file": ("demo.wav", wav_bytes, "audio/wav")},
                data={"name": "测试场次"},
            )
            assert r_up.status_code == 200
            job_id = r_up.json()["id"]

            final = None
            for _ in range(40):
                cur = client.get(f"/api/jobs/{job_id}").json()
                if cur["status"] in ("completed", "failed"):
                    final = cur
                    break
                time.sleep(0.1)

            assert final is not None and final["status"] == "completed", final
            assert final["sentence_count"] == 2
            assert final["llm_status"] == "skipped: llm_disabled"
            assert final["has_md"] and final["has_json"] and final["has_audio"]

            tr = client.get(f"/api/jobs/{job_id}/transcript").json()
            assert "省实" in tr["md_text"]  # SS 暗语展开验证

            dl = client.get(f"/api/jobs/{job_id}/download/md")
            assert dl.status_code == 200
            assert "微信直播逐字稿" in dl.text

            # 启用 LLM 并 mock 后处理重跑
            monkeypatch.setattr(
                postprocess,
                "fix_errors",
                lambda lines, **kw: [line.replace("时间节点", "报名时间节点") for line in lines],
            )
            monkeypatch.setattr(
                postprocess,
                "build_digest",
                lambda segs, **kw: (
                    kw["out_stats"].update({"ok": True}) or "## 一句话概括\n测试整理成功。"
                ),
            )
            client.put(
                "/api/settings",
                json={"enable_llm": True, "llm_endpoint": "http://127.0.0.1:9999/v1/chat/completions"},
            )
            r_re = client.post(f"/api/jobs/{job_id}/postprocess")
            assert r_re.status_code == 200
            assert r_re.json()["llm_status"] == "ok"
            assert r_re.json()["changed_count"] == 1

            tr2 = client.get(f"/api/jobs/{job_id}/transcript").json()
            assert "AI 整理要点" in tr2["md_text"]
            assert "报名时间节点" in tr2["md_text"]

            r_del = client.delete(f"/api/jobs/{job_id}?delete_files=true")
            assert r_del.status_code == 200
            assert client.get(f"/api/jobs/{job_id}").status_code == 404
    finally:
        runner.shutdown()


# ---------------- 磁盘保留策略 API ----------------

def test_cleanup_settings_roundtrip(tmp_path: Path):
    runner = NasJobRunner(data_dir=tmp_path, engine_factory=lambda t, l: _FakeEngine())
    try:
        s = runner.update_settings({
            "retention_days": 14,
            "cleanup_delete_transcripts": True,
            "cleanup_interval_hours": 12,
        })
        assert s["retention_days"] == 14
        assert s["cleanup_delete_transcripts"] is True
        assert s["cleanup_interval_hours"] == 12

        # 负数要被钳到 0，间隔至少 1 小时
        s2 = runner.update_settings({"retention_days": -5, "cleanup_interval_hours": 0})
        assert s2["retention_days"] == 0
        assert s2["cleanup_interval_hours"] == 1
    finally:
        runner.shutdown()


def test_cleanup_plan_and_run_via_api(tmp_path: Path):
    """API 层的预览与执行：预览不删文件，执行才删。"""
    import time as _t

    rec = tmp_path / "recordings"
    rec.mkdir(parents=True)
    audio = rec / "old.wav"
    audio.write_bytes(b"\0" * 4096)

    runner = NasJobRunner(data_dir=tmp_path, engine_factory=lambda t, l: _FakeEngine())
    try:
        old_ts = _t.time() - 60 * 86400
        with runner._lock:
            runner._jobs["j_old"] = {
                "id": "j_old", "title": "老场次", "status": "completed",
                "audio_path": str(audio), "md_path": "",
                "created_at": old_ts, "logs": [],
            }
            runner._save_jobs()

        runner.update_settings({"retention_days": 30})
        app = create_app(runner=runner)
        with TestClient(app) as client:
            p = client.post("/api/cleanup/plan").json()
            assert p["enabled"] is True
            assert p["count"] == 1
            assert p["total_bytes"] == 4096
            assert p["total_human"].endswith("KB")
            assert audio.exists(), "预览阶段绝不能删文件"

            r = client.post("/api/cleanup/run", json={"dry_run": False}).json()
            assert r["ok"] is True
            assert r["deleted_count"] == 1
            assert r["freed_human"].endswith("KB")
            assert not audio.exists()

            # 默认 dry_run=True：不带 body 也不能真删
            audio.write_bytes(b"\0" * 100)
            r2 = client.post("/api/cleanup/run").json()
            assert r2["dry_run"] is True
            assert audio.exists()
    finally:
        runner.shutdown()


def test_cleanup_status_endpoint(tmp_path: Path):
    runner = NasJobRunner(data_dir=tmp_path, engine_factory=lambda t, l: _FakeEngine())
    try:
        runner.update_settings({"retention_days": 0})
        app = create_app(runner=runner)
        with TestClient(app) as client:
            s = client.get("/api/cleanup/status").json()
            assert s["retention_days"] == 0
            assert s["auto_enabled"] is False
    finally:
        runner.shutdown()


def test_library_listing_and_export_zip(tmp_path: Path):
    runner = NasJobRunner(data_dir=tmp_path, engine_factory=lambda t, l: _FakeEngine())
    try:
        d = runner.transcripts_dir / "session_abc"
        d.mkdir(parents=True)
        (d / "20261123_101010_nas_low.md").write_text(
            "# 标题\n\n- 会话 ID：x\n\n这是正文。\n", encoding="utf-8")
        (d / "20261123_101010_nas_low.json").write_text(
            '{"segments":[]}', encoding="utf-8")

        app = create_app(runner=runner)
        with TestClient(app) as client:
            lib = client.get("/api/library").json()
            assert lib["count"] == 1
            item = lib["items"][0]
            assert item["stem"] == "session_abc"
            assert item["has_md"] and item["has_json"]
            assert "这是正文" in item["preview"]

            z = client.get("/api/library/export")
            assert z.status_code == 200
            assert z.headers["content-type"] == "application/zip"
            assert "attachment" in z.headers["content-disposition"]

            import io as _io
            import zipfile
            with zipfile.ZipFile(_io.BytesIO(z.content)) as zf:
                names = zf.namelist()
            assert any(n.endswith(".md") for n in names)
            assert any(n.endswith(".json") for n in names)

            dl = client.get(f"/api/library/{item['stem']}/download/md")
            assert dl.status_code == 200
            assert "这是正文" in dl.text
    finally:
        runner.shutdown()


def test_library_export_empty_returns_404(tmp_path: Path):
    runner = NasJobRunner(data_dir=tmp_path, engine_factory=lambda t, l: _FakeEngine())
    try:
        app = create_app(runner=runner)
        with TestClient(app) as client:
            assert client.get("/api/library/export").status_code == 404
    finally:
        runner.shutdown()


@pytest.mark.parametrize("stem", [
    "..", "../..", "../../etc", "a/b", "con", "", "x" * 200,
    # 反斜杠用 chr(92) 拼出来，避免源码里的  被当成退格符
    "a" + chr(92) + "b",
    ".." + chr(92) + ".." + chr(92) + "windows",
])
def test_library_download_rejects_path_traversal(tmp_path: Path, stem: str):
    runner = NasJobRunner(data_dir=tmp_path, engine_factory=lambda t, l: _FakeEngine())
    try:
        app = create_app(runner=runner)
        with TestClient(app) as client:
            r = client.get(f"/api/library/{stem}/download/md")
            assert r.status_code in (400, 404), f"{stem!r} 应该被拒绝，实际 {r.status_code}"
    finally:
        runner.shutdown()


def test_export_respects_size_cap(tmp_path: Path, monkeypatch):
    """导出上限：超过阈值要明确拒绝（413），而不是把 NAS 内存吃满。"""
    runner = NasJobRunner(data_dir=tmp_path, engine_factory=lambda t, l: _FakeEngine())
    try:
        # 把上限压到极小，避免真造 256MB 文件
        monkeypatch.setattr(type(runner), "EXPORT_MAX_BYTES", 100)

        d = runner.transcripts_dir / "big"
        d.mkdir(parents=True)
        (d / "a.md").write_text("x" * 500, encoding="utf-8")

        app = create_app(runner=runner)
        with TestClient(app) as client:
            r = client.get("/api/library/export")
            assert r.status_code == 413
            assert "导出上限" in r.json()["detail"]
    finally:
        runner.shutdown()


def test_export_skips_symlinks(tmp_path: Path):
    """符号链接不能被打进 zip（可能指向库外文件）。"""
    runner = NasJobRunner(data_dir=tmp_path, engine_factory=lambda t, l: _FakeEngine())
    try:
        outside = tmp_path / "secret.txt"
        outside.write_text("do not export", encoding="utf-8")

        d = runner.transcripts_dir / "s1"
        d.mkdir(parents=True)
        (d / "ok.md").write_text("hello", encoding="utf-8")
        link = d / "leak.md"
        try:
            link.symlink_to(outside)
        except OSError:
            pytest.skip("当前系统不支持创建符号链接")

        import io as _io
        import zipfile

        data, _ = runner.export_library()
        # zip 是压缩的，必须解包再比对内容（直接搜明文会假阴性）
        with zipfile.ZipFile(_io.BytesIO(data)) as zf:
            names = zf.namelist()
            joined = b"".join(zf.read(n) for n in names)
        assert "s1/ok.md" in names
        assert not any("leak" in n for n in names), f"符号链接被打包了：{names}"
        assert b"do not export" not in joined
        assert b"hello" in joined
    finally:
        runner.shutdown()


# ---------------- 回归：herdr 对立性审查 C1/C2/C3 ----------------

def test_c1_corrupt_job_entry_does_not_orphan_all_audio(tmp_path: Path):
    """C1 回归：一条畸形任务记录不能拖垮整份任务表（否则老录音全被当孤儿删）。"""
    import os

    rec = tmp_path / "recordings"
    rec.mkdir(parents=True)
    audio = rec / "20260101_120000_老直播_stream.wav"
    audio.write_bytes(b"\0" * 4096)
    old = time.time() - 100 * 86400
    os.utime(audio, (old, old))

    # 一条正常 + 一条畸形
    (tmp_path / "jobs.json").write_text(json.dumps({
        "j1": {"id": "j1", "title": "老直播", "status": "completed",
               "audio_path": str(audio), "md_path": "",
               "created_at": old, "logs": []},
        "j2": "oops",
    }, ensure_ascii=False), encoding="utf-8")

    runner = NasJobRunner(data_dir=tmp_path, engine_factory=lambda t, l: _FakeEngine())
    try:
        assert len(runner._jobs) == 1, "健康任务记录不该被畸形条目带走"
        assert runner._jobs_load_error, "应记录损坏告警"
        # 坏条目已备份
        assert (tmp_path / "jobs.corrupt.json").exists()

        # 真删必须被拒绝
        res = runner.run_cleanup(dry_run=False, trigger="test")
        assert res["ok"] is False
        assert "损坏" in res["error"]
        assert audio.exists(), "损坏状态下文件被删了"
    finally:
        runner.shutdown()


def test_c1_empty_jobs_but_audio_present_refuses_delete(tmp_path: Path):
    """C1 另一半：任务表为空但有音频 → 疑似损坏，拒绝真删。"""
    rec = tmp_path / "recordings"
    rec.mkdir(parents=True)
    (rec / "20260101_120000_x_stream.wav").write_bytes(b"\0" * 1024)

    runner = NasJobRunner(data_dir=tmp_path, engine_factory=lambda t, l: _FakeEngine())
    try:
        runner.update_settings({"retention_days": 30})
        res = runner.run_cleanup(dry_run=False, trigger="test")
        assert res["ok"] is False
        assert "疑似任务表损坏" in res["error"]
    finally:
        runner.shutdown()


def test_c2_retention_disabled_by_default(tmp_path: Path):
    """C2 回归：新装默认不自动删（破坏性操作必须由人显式开启）。"""
    runner = NasJobRunner(data_dir=tmp_path, engine_factory=lambda t, l: _FakeEngine())
    try:
        s = runner.get_settings()
        assert s["retention_days"] == 0, f"默认值应为 0（不自动删），实际 {s['retention_days']}"
        st = runner.cleanup_status()
        assert st["auto_enabled"] is False
    finally:
        runner.shutdown()


def test_c2_cleanup_history_records_paths(tmp_path: Path):
    """C2 另一半：删除清单要落盘，能回答"我哪天那场录音哪去了"。"""
    rec = tmp_path / "recordings"
    rec.mkdir(parents=True)
    audio = rec / "20260101_120000_gone_stream.wav"
    audio.write_bytes(b"\0" * 2048)
    old = time.time() - 100 * 86400
    import os
    os.utime(audio, (old, old))

    runner = NasJobRunner(data_dir=tmp_path, engine_factory=lambda t, l: _FakeEngine())
    try:
        with runner._lock:
            runner._jobs["j1"] = {
                "id": "j1", "title": "被清的场次", "status": "completed",
                "audio_path": str(audio), "md_path": "",
                "created_at": old, "logs": [],
            }
            runner._save_jobs()
        runner.update_settings({"retention_days": 30})

        res = runner.run_cleanup(dry_run=False, trigger="manual")
        assert res["deleted_count"] == 1
        assert not audio.exists()

        hist = tmp_path / "cleanup_history"
        assert hist.is_dir(), "没有清理历史目录"
        files = list(hist.glob("*.json"))
        assert files, "没有写清理清单"
        doc = json.loads(files[0].read_text(encoding="utf-8"))
        paths = [d["name"] for d in doc.get("deleted", [])]
        assert "20260101_120000_gone_stream.wav" in paths
    finally:
        runner.shutdown()


def test_m2_cleanup_thread_starts_when_enabled_later(tmp_path: Path):
    """M2 回归：启动后把保留天数从 0 改成 >0，定时线程要能起来。"""
    runner = NasJobRunner(data_dir=tmp_path, engine_factory=lambda t, l: _FakeEngine())
    try:
        assert runner.cleanup_status()["worker_alive"] is False
        runner.update_settings({"retention_days": 30})
        for _ in range(30):
            if runner.cleanup_status()["worker_alive"]:
                break
            time.sleep(0.05)
        assert runner.cleanup_status()["worker_alive"] is True
    finally:
        runner.shutdown()


def test_m9_zero_rms_threshold_clamped(tmp_path: Path):
    """M9 回归：silence_rms_threshold=0 会让判停静默失效，必须钳住。"""
    runner = NasJobRunner(data_dir=tmp_path, engine_factory=lambda t, l: _FakeEngine())
    try:
        s = runner.update_settings({"silence_rms_threshold": 0})
        assert s["silence_rms_threshold"] >= 1e-6
    finally:
        runner.shutdown()


def test_c3_token_required_for_mutating_endpoints(tmp_path: Path):
    """C3 回归：开了 token 后，写操作必须带令牌；GET 保持开放。"""
    runner = NasJobRunner(data_dir=tmp_path, engine_factory=lambda t, l: _FakeEngine())
    try:
        app = create_app(runner=runner, require_token="secret-token")
        with TestClient(app) as client:
            # GET 不需要 token
            assert client.get("/api/health").status_code == 200

            # 写操作没 token → 401
            r = client.post("/api/cleanup/run", json={"dry_run": True})
            assert r.status_code == 401

            # 带上就放行
            r = client.post("/api/cleanup/run", json={"dry_run": True},
                            headers={"X-WXLIVE-Token": "secret-token"})
            assert r.status_code == 200
    finally:
        runner.shutdown()


def test_c3_rejects_foreign_host_header(tmp_path: Path):
    """C3 另一半：陌生 Host 头（DNS rebinding）要被挡住。"""
    runner = NasJobRunner(data_dir=tmp_path, engine_factory=lambda t, l: _FakeEngine())
    try:
        app = create_app(runner=runner)
        with TestClient(app) as client:
            r = client.get("/api/health", headers={"Host": "evil.example.com"})
            assert r.status_code == 400
            # IP 与 localhost 正常放行
            assert client.get("/api/health", headers={"Host": "127.0.0.1"}).status_code == 200
            assert client.get("/api/health", headers={"Host": "localhost"}).status_code == 200
    finally:
        runner.shutdown()


def test_h2_recording_limits_defaults(tmp_path: Path):
    """H2：录制上限必须有默认值（防永不结束的流写满磁盘）。"""
    runner = NasJobRunner(data_dir=tmp_path, engine_factory=lambda t, l: _FakeEngine())
    try:
        s = runner.get_settings()
        assert s["max_record_hours"] > 0
        assert s["max_record_gb"] > 0
        assert s["min_record_seconds"] == 300
    finally:
        runner.shutdown()


def test_min_record_and_limits_settings_api_roundtrip(tmp_path: Path):
    """min_record_seconds 与录制上限通过 PUT /api/settings 可读写，首页含四标签页。"""
    runner = NasJobRunner(data_dir=tmp_path, engine_factory=lambda t, l: _FakeEngine())
    try:
        app = create_app(runner=runner)
        with TestClient(app) as client:
            r = client.put(
                "/api/settings",
                json={
                    "min_record_seconds": 600,
                    "max_record_hours": 6,
                    "max_record_gb": 3,
                },
            )
            assert r.status_code == 200
            data = r.json()
            assert data["min_record_seconds"] == 600
            assert data["max_record_hours"] == 6
            assert data["max_record_gb"] == 3

            home = client.get("/").text
            for marker in ("view-workspace", "view-library", "view-storage", "view-settings", "最短录制保护"):
                assert marker in home
    finally:
        runner.shutdown()


def test_is_placeholder_title():
    from wxlive.nas_runner import is_placeholder_title

    assert is_placeholder_title("")
    assert is_placeholder_title("直播_20261123_114405")
    assert is_placeholder_title("orig_2042980977478071617_orig")
    assert is_placeholder_title("音频_20261123_120000")
    assert not is_placeholder_title("北大文辉讲学习 - 欢迎来到我的直播间")


def test_suggest_title_cleans_llm_output(monkeypatch):
    from wxlive import postprocess

    monkeypatch.setattr(
        postprocess,
        "_call",
        lambda **kw: '标题：「积极人生家庭教育 - 厌学六大解法」\n说明：不需要',
    )
    got = postprocess.suggest_title(
        "这是一场关于青少年厌学五大类型与六大解法的家庭教育直播。",
        endpoint="http://fake/v1/chat/completions",
        api_key="k",
    )
    assert got == "积极人生家庭教育 - 厌学六大解法"


def test_rename_and_auto_title_job_endpoints(tmp_path: Path, monkeypatch):
    """手动重命名 + 自动/手动 AI 识别命名会同步更新任务、逐字稿与文件库。"""
    from wxlive import postprocess

    wav_bytes = _make_wav_bytes(seconds=1.0)

    monkeypatch.setattr(postprocess, "fix_errors", lambda lines, **kw: lines)
    monkeypatch.setattr(postprocess, "build_digest", lambda segs, **kw: "## 一句话概括\n测试厌学解法直播。")
    monkeypatch.setattr(
        postprocess,
        "suggest_title",
        lambda src, **kw: "积极人生 - 厌学六大解法",
    )

    runner = NasJobRunner(data_dir=tmp_path, engine_factory=lambda t, l: _FakeEngine())
    try:
        runner.update_settings({"enable_llm": True, "llm_endpoint": "http://fake", "auto_title": True})
        app = create_app(runner=runner)
        with TestClient(app) as client:
            # 1) 占位名（orig_xxx_orig）在转写整理后应自动被 AI 命名替换
            r = client.post(
                "/api/jobs/upload",
                data={"name": "orig_2079009824565503324_orig", "auto_transcribe": "true"},
                files={"file": ("sample.wav", io.BytesIO(wav_bytes), "audio/wav")},
            )
            assert r.status_code == 200
            jid = r.json()["id"]

            deadline = time.time() + 10.0
            job = {}
            while time.time() < deadline:
                job = client.get(f"/api/jobs/{jid}").json()
                if job["status"] in ("completed", "failed"):
                    break
                time.sleep(0.05)

            assert job["status"] == "completed"
            assert job["title"] == "积极人生 - 厌学六大解法"
            assert job.get("auto_named") is True

            # 文件库和 .md 标题行也同步更新
            lib = client.get("/api/library").json()
            assert any(it["title"] == "积极人生 - 厌学六大解法" for it in lib["items"])
            md_text = client.get(f"/api/jobs/{jid}/transcript").json()["md_text"]
            assert md_text.splitlines()[0] == "# 微信直播逐字稿（积极人生 - 厌学六大解法）"

            # 2) 手动重命名 PATCH /api/jobs/{id}
            r2 = client.patch(f"/api/jobs/{jid}", json={"title": "人工改好的直播名"})
            assert r2.status_code == 200
            assert r2.json()["title"] == "人工改好的直播名"
            assert r2.json()["auto_named"] is False

            # 3) 手动点击 AI 识别命名 POST /api/jobs/{id}/auto-title
            monkeypatch.setattr(
                postprocess,
                "suggest_title",
                lambda src, **kw: "积极人生家庭教育 - 六大解法",
            )
            r3 = client.post(f"/api/jobs/{jid}/auto-title")
            assert r3.status_code == 200
            assert r3.json()["title"] == "积极人生家庭教育 - 六大解法"
            assert r3.json()["auto_named"] is True
    finally:
        runner.shutdown()


def test_custom_name_not_overwritten_by_auto_title(tmp_path: Path, monkeypatch):
    """用户明确指定的场次名称，不应在转写完成后被自动覆盖。"""
    from wxlive import postprocess

    wav_bytes = _make_wav_bytes(seconds=1.0)

    monkeypatch.setattr(postprocess, "fix_errors", lambda lines, **kw: lines)
    monkeypatch.setattr(postprocess, "build_digest", lambda segs, **kw: "## 一句话概括\n测试。")
    monkeypatch.setattr(postprocess, "suggest_title", lambda src, **kw: "不该覆盖这个名字")

    runner = NasJobRunner(data_dir=tmp_path, engine_factory=lambda t, l: _FakeEngine())
    try:
        runner.update_settings({"enable_llm": True, "llm_endpoint": "http://fake", "auto_title": True})
        app = create_app(runner=runner)
        with TestClient(app) as client:
            r = client.post(
                "/api/jobs/upload",
                data={"name": "家长必看专场", "auto_transcribe": "true"},
                files={"file": ("sample.wav", io.BytesIO(wav_bytes), "audio/wav")},
            )
            jid = r.json()["id"]
            deadline = time.time() + 10.0
            job = {}
            while time.time() < deadline:
                job = client.get(f"/api/jobs/{jid}").json()
                if job["status"] in ("completed", "failed"):
                    break
                time.sleep(0.05)

            assert job["status"] == "completed"
            assert job["title"] == "家长必看专场"
    finally:
        runner.shutdown()

