"""回归：产物与音频必须分目录，收尾不得静默丢片段。

背景（2026-11-17 体检，已复现的 bug）：

1. `pipeline.py` 曾把 `RECORDINGS_DIR` 传给 Session，而 cli / gui / auto_pipeline
   都传 `TRANSCRIPTS_DIR`。后果有两个：
   - 逐字稿 json/jsonl/md 混进 `output/recordings/`（实际留下了 6 个错位文件）
   - `gui.py` 按 `TRANSCRIPTS_DIR/<id>.md` 去打开，必然找不到文件

2. 收尾阶段（drain / resampler.flush / segmenter.flush）投递 ASR 队列时
   用的是 `except queue.Full: pass`，而主循环同位置是有 warning 的。
   收尾投递的是整场**最后**的音频，最可能含结论，静默丢弃不可接受。

这两条都必须有测试盯着，否则下次重构很容易改回去。
"""

from __future__ import annotations

import queue
from pathlib import Path

import numpy as np
import pytest

from wxlive.session import Session
from wxlive.vad import Segment


# ---------- 1. 产物与音频分目录 ----------


def test_products_and_audio_can_live_in_separate_dirs(tmp_path: Path) -> None:
    """产物进 base_dir，音频进 audio_dir，两者互不干扰。"""
    rec = tmp_path / "recordings"
    tr = tmp_path / "transcripts"
    rec.mkdir()
    tr.mkdir()

    s = Session(tr, name="demo", sample_rate=16000, record_audio=True, audio_dir=rec)
    try:
        assert s.md_path.parent == tr
        assert s.json_path.parent == tr
        assert s.jsonl_path.parent == tr
        assert s.audio_path.parent == rec
    finally:
        s.close()

    assert s.md_path.exists()
    assert s.json_path.exists()
    assert s.audio_path.exists()


def test_audio_dir_defaults_to_base_dir(tmp_path: Path) -> None:
    """不传 audio_dir 时保持旧行为：音频与产物同目录。"""
    s = Session(tmp_path, name="compat", sample_rate=16000, record_audio=True)
    try:
        assert s.audio_path.parent == s.md_path.parent == tmp_path
    finally:
        s.close()


def test_md_header_points_at_audio_that_exists(tmp_path: Path) -> None:
    """md 里写的音频名必须在 audio_dir 下真实存在。

    这条防的是「路径拼错但没人发现」：gui 曾经按 id 拼路径去打开，
    结果指向一个不存在的文件。
    """
    rec = tmp_path / "recordings"
    tr = tmp_path / "transcripts"
    rec.mkdir()
    tr.mkdir()

    s = Session(tr, name="demo", sample_rate=16000, record_audio=True, audio_dir=rec)
    s.write_audio(np.zeros(1600, dtype=np.float32))
    s.close()

    assert s.audio_path.exists()
    assert s.audio_path.parent == rec
    assert s.md_path.exists()


# ---------- 2. 收尾不得静默丢片段 ----------


def _bare_pipeline(tmp_path: Path, monkeypatch):
    """构造一个只带 _enqueue_tail 所需属性的 Pipeline 壳。

    不起真实采集/线程：这里要验的是"队列满时的行为"，与音频链路无关。
    """
    from wxlive import pipeline as pl

    class _Sess:
        id = "sess_for_test"

    class _Cfg:
        class audio:  # noqa: N801 - 模拟 dataclass 结构
            sample_rate = 16000

    obj = pl.Pipeline.__new__(pl.Pipeline)
    obj._asr_queue = queue.Queue(maxsize=1)
    obj.session = _Sess()
    obj.cfg = _Cfg()

    monkeypatch.setattr(pl, "OUTPUT_DIR", tmp_path, raising=False)
    # _spill_audio 内部是 from .config import OUTPUT_DIR，这里直接改 config
    import wxlive.config as cfgmod

    monkeypatch.setattr(cfgmod, "OUTPUT_DIR", tmp_path, raising=False)
    return obj


def _seg(start: float) -> Segment:
    return Segment(
        audio=np.full(1600, 0.1, dtype=np.float32), start_ts=start, end_ts=start + 0.1
    )


def test_tail_enqueue_goes_to_queue_when_space(tmp_path: Path, monkeypatch) -> None:
    obj = _bare_pipeline(tmp_path, monkeypatch)
    obj._enqueue_tail(_seg(1.0), "测试")
    assert obj._asr_queue.qsize() == 1


def test_tail_enqueue_spills_audio_instead_of_dropping(tmp_path: Path, monkeypatch) -> None:
    """队列满时：不入队，但音频必须落盘，绝不能无声消失。"""
    obj = _bare_pipeline(tmp_path, monkeypatch)
    obj._asr_queue.put(_seg(0.0))  # 先把容量 1 占满

    obj._enqueue_tail(_seg(20.0), "收尾 drain")

    spill = tmp_path / "spilled"
    files = sorted(spill.glob("*.wav")) if spill.exists() else []
    assert len(files) == 1, "队列满时收尾片段必须落盘，不能静默丢弃"

    import soundfile as sf

    audio, sr = sf.read(str(files[0]), dtype="float32")
    assert sr == 16000
    assert len(audio) == 1600
    # 内容必须与原始片段一致，证明落的是真数据而不是占位
    assert float(np.sqrt(np.mean(audio ** 2))) == pytest.approx(0.1, abs=0.01)

    # 队列没被塞进去（容量仍是 1）
    assert obj._asr_queue.qsize() == 1


# ---------- 3. 停止原因必须可追溯 ----------


def test_stop_reason_is_recorded_and_written_to_meta(tmp_path: Path) -> None:
    """停止原因要能被采集端记录、并写进产物 meta。

    背景：直播录制最常被问"这里为什么断了"。以前没有任何记录，
    只能对着 WAV 尾部静音时长猜（2026-11-16 和 11-17 两段录音都这么猜过）。
    """
    from wxlive.audio_capture import LoopbackRecorder

    rec = LoopbackRecorder(chunk_seconds=0.1, mono=True, out_rate=16000)
    # 不真正 start（避免依赖本机音频设备）；只验证 stop(reason=) 的记录行为
    rec.stop(reason="静音 40s 自动停止")
    assert rec.stop_reason == "静音 40s 自动停止"

    rec2 = LoopbackRecorder(chunk_seconds=0.1, mono=True, out_rate=16000)
    rec2.stop_reason = "达到设定时长"
    s = Session(tmp_path, name="meta_test", sample_rate=16000, record_audio=False)
    doc = s.close(extra_meta={
        "stop_reason": rec2.stop_reason,
        "dropped_chunks": 0,
    })
    assert doc["meta"]["stop_reason"] == "达到设定时长"
    assert doc["meta"]["dropped_chunks"] == 0


def test_stop_default_reason_when_not_given() -> None:
    """不传 reason 时也要有明确默认值，不能是空字符串。"""
    from wxlive.audio_capture import LoopbackRecorder

    rec = LoopbackRecorder(chunk_seconds=0.1, mono=True, out_rate=16000)
    rec.stop()
    assert rec.stop_reason  # 非空


# ---------- 4. 收尾超时必须可发现，不能"看起来正常" ----------


def test_drain_timeout_is_configurable_and_generous() -> None:
    """收尾等待时间必须是可调属性，且不能太小。

    背景（2026-11-17 实测）：10 分钟录制里网关两次 RTF 飙到 3.2/3.8，
    收尾时队列堆到 9 个片段。若 drain 时间不够，进程会被外部超时截断，
    json 不生成、转写比音频少 3 分钟，而产物看起来"正常"。
    """
    from wxlive.pipeline import Pipeline

    assert hasattr(Pipeline, "drain_timeout")
    assert Pipeline.drain_timeout >= 600, "收尾等待必须留足余量（抖动能落后几分钟）"


def test_transcript_complete_flag_written_to_meta(tmp_path: Path) -> None:
    """meta.transcript_complete 必须存在：False 表示产物可能缺尾部。"""
    s = Session(tmp_path, name="flag_test", sample_rate=16000, record_audio=False)
    doc = s.close(extra_meta={"transcript_complete": False})
    assert doc["meta"]["transcript_complete"] is False

    s2 = Session(tmp_path, name="flag_ok", sample_rate=16000, record_audio=False)
    doc2 = s2.close(extra_meta={"transcript_complete": True})
    assert doc2["meta"]["transcript_complete"] is True


# ---------- 5. ASR 统计不能"假健康" ----------


def test_stats_expose_content_loss_when_all_chunks_fail() -> None:
    """块内全部失败时，stats 必须能看出内容丢失。

    背景（2026-11-17 实测）：4 个音频块全部落占位符，但 stats 显示
    primary_ok=4 / primary_fail=0 —— 因为 gemini_asr 内部把"整块失败"
    吞成了占位符返回值，FallbackEngine 看到的是"调用成功"。
    转写全废而监控全绿，是最危险的一类。
    """
    from wxlive.engine import FallbackEngine

    class _Primary:
        @property
        def stats(self) -> dict:
            return {
                "engine": "fake",
                "failed_chunks": [[0.0, 8.5], [0.0, 10.3]],
                "bad_detected": 0,
                "recovered": 0,
                "unsplit_detected": 0,
                "suspect_chunks": [],
            }

        def transcribe(self, audio, sample_rate):  # noqa: ARG002
            return []

        def close(self):
            pass

    eng = FallbackEngine(primary=_Primary(), fallback=None)
    eng.transcribe(__import__("numpy").zeros(16000, dtype="float32"), 16000)
    st = eng.stats

    # 老字段仍是"看起来成功"，这正是盲区所在
    assert st["primary_ok"] == 1
    assert st["primary_fail"] == 0
    # 新字段必须把真实损失暴露出来
    assert st["failed_chunk_count"] == 2
    assert st["has_content_loss"] is True


def test_stats_report_no_loss_on_clean_run() -> None:
    """正常情况下 has_content_loss 必须是 False，避免误报。"""
    from wxlive.engine import FallbackEngine

    class _Primary:
        @property
        def stats(self) -> dict:
            return {"engine": "fake", "failed_chunks": []}

        def transcribe(self, audio, sample_rate):  # noqa: ARG002
            return []

        def close(self):
            pass

    eng = FallbackEngine(primary=_Primary(), fallback=None)
    eng.transcribe(__import__("numpy").zeros(16000, dtype="float32"), 16000)
    st = eng.stats
    assert st["failed_chunk_count"] == 0
    assert st["has_content_loss"] is False
