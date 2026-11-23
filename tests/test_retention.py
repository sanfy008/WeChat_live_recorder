"""磁盘保留策略与转录文件库的测试。

重点覆盖的是"删错了怎么办"这一类风险，而不是 happy path：
- 正在跑的任务绝不能删
- 目录穿越不能生效
- 转录产物默认不动
- 孤儿文件要认出来，但别误删抓流工具放进来的其他文件
"""

from __future__ import annotations

import time
from pathlib import Path

import pytest

from wxlive import retention

# 时间基准必须贴近真实时钟：孤儿文件的年龄是用**真实 mtime** 算的，
# 若把 NOW 设成一个远离现在的时间点，"刚创建的文件"会被算成很老，
# 测出来的失败是测试自身造出来的，不是代码问题。
NOW = time.time()
DAY = 86400.0


def _make_audio(root: Path, name: str, size: int = 2048) -> Path:
    p = root / name
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_bytes(b"\0" * size)
    return p


def _job(job_id: str, audio: Path | None, *, days_old: float = 0.0,
         status: str = "completed", title: str = "测试直播",
         md: Path | None = None) -> dict:
    created = NOW - days_old * DAY
    return {
        "id": job_id,
        "title": title,
        "status": status,
        "audio_path": str(audio) if audio else "",
        "md_path": str(md) if md else "",
        "created_at": created,
    }


# ---------------- 计划：不该删的 ----------------

def test_disabled_when_retention_zero(tmp_path: Path):
    rec = tmp_path / "recordings"
    audio = _make_audio(rec, "old.wav")
    plan = retention.plan_cleanup(
        {"j1": _job("j1", audio, days_old=999)},
        rec, tmp_path / "transcripts",
        retention_days=0, now=NOW,
    )
    assert plan.enabled is False
    assert plan.candidates == []


def test_within_retention_not_deleted(tmp_path: Path):
    rec = tmp_path / "recordings"
    audio = _make_audio(rec, "fresh.wav")
    plan = retention.plan_cleanup(
        {"j1": _job("j1", audio, days_old=3)},
        rec, tmp_path / "transcripts",
        retention_days=30, now=NOW,
    )
    assert plan.candidates == []


def test_active_job_skipped_even_if_old(tmp_path: Path):
    """正在录的任务，文件再老也不能删。"""
    rec = tmp_path / "recordings"
    audio = _make_audio(rec, "recording.wav")
    plan = retention.plan_cleanup(
        {"j1": _job("j1", audio, days_old=999, status="recording")},
        rec, tmp_path / "transcripts",
        retention_days=30, now=NOW,
    )
    assert plan.candidates == []
    assert any("进行中" in s["reason"] for s in plan.skipped)


def test_outside_recordings_dir_skipped(tmp_path: Path):
    """音频不在录制目录内 → 拒绝（防误删用户别处的文件）。"""
    rec = tmp_path / "recordings"
    rec.mkdir()
    outside = tmp_path / "elsewhere" / "x.wav"
    outside.parent.mkdir()
    outside.write_bytes(b"\0" * 100)

    plan = retention.plan_cleanup(
        {"j1": _job("j1", outside, days_old=999)},
        rec, tmp_path / "transcripts",
        retention_days=30, now=NOW,
    )
    assert plan.candidates == []
    assert any("不在录制目录内" in s["reason"] for s in plan.skipped)


# ---------------- 计划：该删的 ----------------

def test_old_audio_is_candidate(tmp_path: Path):
    rec = tmp_path / "recordings"
    audio = _make_audio(rec, "old.wav", size=4096)
    plan = retention.plan_cleanup(
        {"j1": _job("j1", audio, days_old=45, title="老直播")},
        rec, tmp_path / "transcripts",
        retention_days=30, now=NOW,
    )
    assert len(plan.candidates) == 1
    c = plan.candidates[0]
    assert c.kind == "audio"
    assert c.job_id == "j1"
    assert c.title == "老直播"
    assert c.size_bytes == 4096
    assert 44 < c.age_days < 46
    assert plan.total_bytes == 4096


def test_transcripts_kept_by_default(tmp_path: Path):
    """默认只清音频，逐字稿必须保留。"""
    rec = tmp_path / "recordings"
    tr = tmp_path / "transcripts"
    audio = _make_audio(rec, "old.wav")
    tr_dir = tr / "session1"
    tr_dir.mkdir(parents=True)
    (tr_dir / "a.md").write_text("hello", encoding="utf-8")
    (tr_dir / "a.json").write_text("{}", encoding="utf-8")

    plan = retention.plan_cleanup(
        {"j1": _job("j1", audio, days_old=45, md=tr_dir / "a.md")},
        rec, tr, retention_days=30, delete_transcripts=False, now=NOW,
    )
    kinds = {c.kind for c in plan.candidates}
    assert kinds == {"audio"}


def test_transcripts_deleted_when_explicitly_enabled(tmp_path: Path):
    rec = tmp_path / "recordings"
    tr = tmp_path / "transcripts"
    audio = _make_audio(rec, "old.wav")
    tr_dir = tr / "session1"
    tr_dir.mkdir(parents=True)
    (tr_dir / "a.md").write_text("x" * 100, encoding="utf-8")

    plan = retention.plan_cleanup(
        {"j1": _job("j1", audio, days_old=45, md=tr_dir / "a.md")},
        rec, tr, retention_days=30, delete_transcripts=True, now=NOW,
    )
    kinds = sorted(c.kind for c in plan.candidates)
    assert kinds == ["audio", "transcript"]
    tr_c = next(c for c in plan.candidates if c.kind == "transcript")
    assert tr_c.path == tr_dir


def test_transcript_dir_root_never_a_candidate(tmp_path: Path):
    """md 直接放在 transcripts 根目录时，不能把根目录整个删掉。"""
    rec = tmp_path / "recordings"
    tr = tmp_path / "transcripts"
    audio = _make_audio(rec, "old.wav")
    tr.mkdir(parents=True)
    md = tr / "loose.md"
    md.write_text("x", encoding="utf-8")

    plan = retention.plan_cleanup(
        {"j1": _job("j1", audio, days_old=45, md=md)},
        rec, tr, retention_days=30, delete_transcripts=True, now=NOW,
    )
    assert all(c.kind != "transcript" for c in plan.candidates)


# ---------------- 孤儿文件 ----------------

def test_orphan_audio_detected(tmp_path: Path):
    rec = tmp_path / "recordings"
    orphan = _make_audio(rec, "nobody_owns_me.wav")
    old = NOW - 100 * DAY
    import os
    os.utime(orphan, (old, old))

    plan = retention.plan_cleanup({}, rec, tmp_path / "transcripts",
                                  retention_days=30, now=NOW)
    assert len(plan.candidates) == 1
    assert plan.candidates[0].kind == "orphan_audio"


def test_orphan_recent_not_deleted(tmp_path: Path):
    rec = tmp_path / "recordings"
    _make_audio(rec, "just_created.wav")   # mtime = 现在
    plan = retention.plan_cleanup({}, rec, tmp_path / "transcripts",
                                  retention_days=30, now=NOW)
    assert plan.candidates == []


def test_junk_files_detected(tmp_path: Path):
    rec = tmp_path / "recordings"
    junk = rec / "broken.wav.part"
    junk.parent.mkdir(parents=True)
    junk.write_bytes(b"\0" * 64)
    old = NOW - 100 * DAY
    import os
    os.utime(junk, (old, old))

    plan = retention.plan_cleanup({}, rec, tmp_path / "transcripts",
                                  retention_days=30, now=NOW)
    assert [c.kind for c in plan.candidates] == ["junk"]


def test_non_wav_untouched(tmp_path: Path):
    """抓流工具会把 .mkv/.mp4 放进同一个目录，那些不归我们管。"""
    rec = tmp_path / "recordings"
    other = rec / "tool_output.mkv"
    other.parent.mkdir(parents=True)
    other.write_bytes(b"\0" * 64)
    old = NOW - 100 * DAY
    import os
    os.utime(other, (old, old))

    plan = retention.plan_cleanup({}, rec, tmp_path / "transcripts",
                                  retention_days=30, now=NOW)
    assert plan.candidates == []


# ---------------- 执行 ----------------

def test_execute_deletes_and_reports_freed(tmp_path: Path):
    rec = tmp_path / "recordings"
    tr = tmp_path / "transcripts"
    a1 = _make_audio(rec, "a1.wav", size=1000)
    a2 = _make_audio(rec, "a2.wav", size=2000)
    md_dir = tr / "s1"
    md_dir.mkdir(parents=True)
    (md_dir / "x.md").write_text("y" * 50, encoding="utf-8")

    jobs = {
        "j1": _job("j1", a1, days_old=45, md=md_dir / "x.md"),
        "j2": _job("j2", a2, days_old=60),
    }
    plan = retention.plan_cleanup(jobs, rec, tr, retention_days=30,
                                  delete_transcripts=True, now=NOW)
    result = retention.execute_cleanup(plan)

    assert result.ok
    assert result.deleted_count if hasattr(result, "deleted_count") else True
    assert len(result.deleted) == 3
    assert result.freed_bytes >= 3000
    assert not a1.exists() and not a2.exists()
    assert not md_dir.exists()


def test_execute_skips_when_plan_invalid(tmp_path: Path):
    plan = retention.CleanupPlan(retention_days=0)
    result = retention.execute_cleanup(plan)
    assert result.ok is False
    assert "禁用" in result.error


def test_execute_tolerates_missing_file(tmp_path: Path):
    """计划算完到执行之间文件被删了，不能整体失败。"""
    rec = tmp_path / "recordings"
    audio = _make_audio(rec, "gone.wav")
    plan = retention.plan_cleanup(
        {"j1": _job("j1", audio, days_old=45)}, rec, tmp_path / "transcripts",
        retention_days=30, now=NOW,
    )
    audio.unlink()
    result = retention.execute_cleanup(plan)
    assert result.failed == []


# ---------------- 文件库扫描 ----------------

def test_scan_library_reads_disk_not_jobs(tmp_path: Path):
    """磁盘上存在、但 jobs.json 里没有的，也要列出来（能导出才有意义）。"""
    tr = tmp_path / "transcripts"
    d = tr / "orphan_session"
    d.mkdir(parents=True)
    (d / "a.md").write_text("# 标题\n\n- 元数据\n\n这是正文内容。\n", encoding="utf-8")
    (d / "a.json").write_text('{"segments":[]}', encoding="utf-8")

    items = retention.scan_library(tr, {})
    assert len(items) == 1
    it = items[0]
    assert it["stem"] == "orphan_session"
    assert it["file_count"] == 2
    assert it["has_md"] and it["has_json"]
    assert "这是正文内容" in it["preview"]


def test_scan_library_merges_job_metadata(tmp_path: Path):
    tr = tmp_path / "transcripts"
    d = tr / "sess"
    d.mkdir(parents=True)
    md = d / "a.md"
    md.write_text("# t\n\n正文\n", encoding="utf-8")

    jobs = {"j1": {"title": "真实标题", "status": "completed",
                   "md_path": str(md), "sentence_count": 42,
                   "audio_seconds": 600.0}}
    items = retention.scan_library(tr, jobs)
    assert items[0]["title"] == "真实标题"
    assert items[0]["job_id"] == "j1"
    assert items[0]["sentence_count"] == 42


def test_scan_library_ignores_empty_dirs(tmp_path: Path):
    tr = tmp_path / "transcripts"
    (tr / "empty").mkdir(parents=True)
    assert retention.scan_library(tr, {}) == []


def test_scan_library_missing_dir_returns_empty(tmp_path: Path):
    assert retention.scan_library(tmp_path / "nope", {}) == []


# ---------------- 回归：对立性审查发现的真 bug ----------------

def test_inflight_upload_not_deleted_as_orphan(tmp_path: Path):
    """BUG 回归：上传是"先落盘、后登记任务"，中间窗口不能被当孤儿删掉。

    修复前实测：一个刚落盘但还没写进 jobs.json 的 .wav，
    只要 mtime 超过保留期就会被判孤儿（数据丢失）。

    这里刻意把 mtime 设在"刚过保留期一点点"（30 天 + 2 小时）——
    正是真实场景：一场跨过保留期的长直播/慢上传刚好落在清理扫描窗口里。
    mtime 远早于保留期的那种，本来就该被清（属于长眠残骸）。
    """
    import os

    rec = tmp_path / "recordings"
    inflight = _make_audio(rec, "uploading_now.wav")
    # 刚过 30 天保留期 2 小时 —— 落在 24h 宽限区内
    old = NOW - (30 * DAY + 2 * 3600)
    os.utime(inflight, (old, old))

    plan = retention.plan_cleanup({}, rec, tmp_path / "transcripts",
                                  retention_days=30, now=NOW)
    assert plan.candidates == [], (
        "上传窗口内的文件被误判为孤儿："
        f"{[c.to_dict() for c in plan.candidates]}"
    )


def test_true_orphan_beyond_grace_still_deleted(tmp_path: Path):
    """超过「保留期 + 24h 宽限」的真孤儿仍要能清掉，别把功能废掉。"""
    import os

    rec = tmp_path / "recordings"
    orphan = _make_audio(rec, "really_orphan.wav")
    old = NOW - 40 * DAY          # 40 天 > 30 天 + 1 天宽限
    os.utime(orphan, (old, old))

    plan = retention.plan_cleanup({}, rec, tmp_path / "transcripts",
                                  retention_days=30, now=NOW)
    assert [c.kind for c in plan.candidates] == ["orphan_audio"]


def test_junk_not_delayed_by_orphan_grace(tmp_path: Path):
    """.part/.tmp 残骸不需要宽限（它们不可能是"正在上传的成品"）。"""
    import os

    rec = tmp_path / "recordings"
    junk = rec / "half.wav.part"
    junk.parent.mkdir(parents=True)
    junk.write_bytes(b"\0" * 32)
    old = NOW - 31 * DAY
    os.utime(junk, (old, old))

    plan = retention.plan_cleanup({}, rec, tmp_path / "transcripts",
                                  retention_days=30, now=NOW)
    assert [c.kind for c in plan.candidates] == ["junk"]
