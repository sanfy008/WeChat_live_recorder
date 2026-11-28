"""`scripts/audit_nas_job.py` 的判定逻辑测试。

## 为什么测这个脚本

它是「直播录完到底行不行」的唯一自动化判据。判据本身写错，比没有判据更危险 ——
会给出一个看起来严谨的错结论。

本轮实测就踩到两个「照字面判会判错」的字段：

1. **`truncated` 不能一刀切看 True/False。**
   代码里 `COMPLETE_STOP_REASONS = {"user_stop", "silence_timeout", "stream_ended"}`，
   其余原因（如 `stream_ended_404`）**刻意**判为截断：没看到静音就 404，
   多半是签名地址先失效、录音可能不完整。这是设计行为。
   所以判据是「停止原因是否属于正常收工」，不是 `truncated` 的字面值。

2. **`meta.quality` 可能整个不存在。**
   老镜像产出的 json 里没有这一段（quality 是后加的）。
   不能当成"检查通过"，也不能当成"检查失败"，必须报「未取到数据」。

这两条都属于「静默偏差」：字段存在与否不影响脚本跑完，但结论会反。
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))

import audit_nas_job as A  # noqa: E402


# ---------------- 桩 ----------------

def _job(**kw):
    base = {
        "id": "J1", "status": "completed", "title": "t",
        "audio_seconds": 3600.0, "audio_bytes": 110 * 1048576,
        "md_path": "/x/a.md", "json_path": "/x/a.json",
        "sentence_count": 500, "truncated": False,
        "llm_status": "ok", "error": "",
        "stop_reason": "silence_timeout", "stop_reason_text": "",
        "done_seconds": 3600.0,
    }
    base.update(kw)
    return base


@pytest.fixture
def run(monkeypatch, capsys):
    """跑一次 main()，返回 (退出码, stdout)。"""
    def _run(job, meta=None):
        monkeypatch.setattr(A, "fetch_job", lambda op, base, jid: job)
        monkeypatch.setattr(A, "fetch_artifact_meta", lambda op, base, jid: meta or {})
        code = A.main(["--job", "J1", "--base", "http://127.0.0.1:1"])
        return code, capsys.readouterr().out
    return _run


# ---------------- 用例 ----------------

def test_normal_stop_passes_structure(run):
    """正常收工 + 产物齐全 → 结构层通过。"""
    code, out = run(_job())
    assert code == 0
    assert "结构层: 通过" in out


def test_stream_ended_404_counts_as_abnormal_stop(run):
    """`stream_ended_404` 是刻意的截断判定，不能被当成正常收工。

    这是本轮实测踩到的：历史任务 truncated=True 看起来像 bug，
    实际是代码有意为之（地址先失效 → 录音可能不完整）。
    """
    code, out = run(_job(truncated=True, stop_reason="stream_ended_404"))
    assert code == 1
    assert "结构层: 未通过" in out
    assert "非常规收工" in out


def test_missing_quality_is_unknown_not_pass(run):
    """没有 quality 字段时必须报「未取到数据」，不能报通过。

    老镜像产物就是这种情况。把它当通过 = 用结构层绿灯推断内容层。
    """
    code, out = run(_job(), meta={"transcript_complete": True})
    assert "内容层: 未取到数据" in out
    assert "内容层: 通过" not in out
    assert "没有 quality 字段" in out


def test_quality_ok_passes_content(run):
    """quality.ok=True 且残留为 0 → 内容层通过。"""
    meta = {
        "quality": {"ok": True, "trad_residual": 0, "hallucination_residual": 0},
        "transcript_complete": True,
        "tail_gap_seconds": 0.9,
    }
    code, out = run(_job(), meta=meta)
    assert "内容层: 通过" in out
    assert code == 0


def test_residual_traditional_fails_content(run):
    """残留繁体字 > 0 必须判不通过（这是真实踩过的缺陷）。"""
    meta = {
        "quality": {"ok": False, "trad_residual": 574, "hallucination_residual": 0},
        "transcript_complete": True,
    }
    code, out = run(_job(), meta=meta)
    assert "内容层: 未通过" in out


def test_residual_hallucination_fails_content(run):
    """幻觉残留 > 0 必须判不通过。"""
    meta = {
        "quality": {"ok": False, "trad_residual": 0, "hallucination_residual": 2},
        "transcript_complete": True,
    }
    code, out = run(_job(), meta=meta)
    assert "内容层: 未通过" in out


def test_tail_gap_uses_meta_value_when_present(run):
    """有 meta.tail_gap_seconds 时优先用它，而不是自己算 done_seconds。"""
    meta = {"transcript_complete": True, "tail_gap_seconds": 999.0}
    code, out = run(_job(done_seconds=3600.0), meta=meta)
    assert "tail_gap_seconds=999.0s" in out
    assert "❌ [tail_gap]" in out


def test_llm_partial_fails_structure(run):
    """长直播下 llm_status=partial 说明摘要没跑完，结构层不通过。"""
    code, out = run(_job(llm_status="partial"))
    assert code == 1
    assert "结构层: 未通过" in out


def test_missing_sentences_fails_structure(run):
    """句子数为 0 = 转写没产出，必须判不通过。"""
    code, out = run(_job(sentence_count=0))
    assert code == 1


def test_job_not_found_returns_2(monkeypatch):
    """任务不存在时退出码 2（区别于"跑通了但不合格"的 1）。"""
    monkeypatch.setattr(A, "fetch_job", lambda op, base, jid: None)
    assert A.main(["--job", "nope", "--base", "http://127.0.0.1:1"]) == 2
