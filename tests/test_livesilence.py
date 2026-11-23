"""直播静音判停的测试。

这块的风险是"误杀"（还在播却停了）和"漏判"（播完了永远不停）。
所以两组都要覆盖，并且刻意构造容易误判的边界：

- 忽然一声响（咳嗽/翻页）夹在静音里 → 计数必须清零，不能累加
- 说话中带 1-3 秒自然停顿 → 绝不能判停
- 从头到尾没声音（地址错/空音轨）→ 不该"正常收工"去转写空文件
- 录制中的 WAV 头长度不可信 → 必须用文件实际大小反推
"""

from __future__ import annotations

import struct
import wave
from pathlib import Path

import numpy as np
import pytest

from wxlive import livesilence
from wxlive.livesilence import LiveSilenceMonitor, read_tail_mono

SR = 16000


def _tone(seconds: float, amp: float = 0.3, sr: int = SR) -> np.ndarray:
    t = np.arange(int(seconds * sr)) / sr
    return (amp * np.sin(2 * np.pi * 440 * t)).astype(np.float32)


def _silence(seconds: float, sr: int = SR) -> np.ndarray:
    return np.zeros(int(seconds * sr), dtype=np.float32)


def _write_wav(path: Path, audio: np.ndarray, sr: int = SR,
               header_frames: int | None = None) -> Path:
    """写 WAV。header_frames 用于模拟"录制中头部长度是占位值"。"""
    path.parent.mkdir(parents=True, exist_ok=True)
    pcm = (np.clip(audio, -1.0, 1.0) * 32767).astype("<i2").tobytes()
    with wave.open(str(path), "wb") as wf:
        wf.setnchannels(1)
        wf.setsampwidth(2)
        wf.setframerate(sr)
        if header_frames is None:
            wf.writeframes(pcm)
        else:
            # 手动写一个"声称有 header_frames 帧"的头部，但只填实际数据
            wf.writeframes(pcm)
    if header_frames is not None:
        # 篡改 RIFF/data 长度字段，模拟录制中未回填的状态
        raw = bytearray(path.read_bytes())
        struct.pack_into("<I", raw, 4, header_frames * 2 + 36)   # RIFF size
        struct.pack_into("<I", raw, 40, header_frames * 2)       # data size
        path.write_bytes(bytes(raw))
    return path


# ---------------- 监控器核心逻辑 ----------------

def test_no_stop_while_talking():
    mon = LiveSilenceMonitor(stop_after_seconds=40)
    for _ in range(20):                       # 20 轮 × 3 秒 = 60 秒有声
        v = mon.poll(_tone(3.0), SR)
    assert not v.should_stop
    assert v.heard_sound is True
    assert v.silent_seconds == 0.0


def test_stops_after_continuous_silence():
    mon = LiveSilenceMonitor(stop_after_seconds=40)
    mon.poll(_tone(3.0), SR)                  # 先听到声音
    stopped_at = None
    for i in range(30):                       # 30 × 3s = 90s 静音
        v = mon.poll(_silence(3.0), SR)
        if v.should_stop:
            stopped_at = (i + 1) * 3
            break
    assert stopped_at is not None, "连续静音超过阈值却没有判停"
    assert 39 <= stopped_at <= 45, f"判停时机异常：{stopped_at}s"


def test_sound_resets_silence_counter():
    """静音中途有一声响，计数必须清零（时长内不该判停）。"""
    mon = LiveSilenceMonitor(stop_after_seconds=40)
    mon.poll(_tone(3.0), SR)

    total = 0.0
    # 3 轮静音（9 秒）→ 一声 → 再来 3 轮（9 秒）→ 一声 …
    for _ in range(4):
        for _ in range(3):
            v = mon.poll(_silence(3.0), SR)
            total += 3.0
            assert not v.should_stop, f"{total}s 时误判停"
        v = mon.poll(_tone(3.0), SR)
        total += 3.0
        assert not v.should_stop
        assert v.silent_seconds == 0.0


def test_single_spike_inside_silence_resets():
    """一块音频里只有很短的响声，也必须清零。"""
    mon = LiveSilenceMonitor(stop_after_seconds=10)
    mon.poll(_tone(3.0), SR)
    v = mon.poll(_silence(2.5), SR)
    assert v.silent_seconds == pytest.approx(2.5, abs=0.6)
    # 2.5 秒静音后接 0.5 秒有声（同一块里混合）
    mixed = np.concatenate([_silence(2.5), _tone(0.5)])
    v = mon.poll(mixed, SR)
    assert v.silent_seconds == 0.0


def test_never_sound_does_not_stop():
    """从头到尾静音（地址错/空音轨）→ 不能"正常收工"。"""
    mon = LiveSilenceMonitor(stop_after_seconds=10, require_sound_before=True)
    for _ in range(20):
        v = mon.poll(_silence(3.0), SR)
    assert v.should_stop is False
    assert v.heard_sound is False
    assert "尚未检测到有效声音" in v.reason


def test_never_sound_can_stop_when_disabled_requirement():
    """关掉 require_sound_before 时，纯静音也能触发停止（供特殊场景用）。"""
    mon = LiveSilenceMonitor(stop_after_seconds=10, require_sound_before=False)
    stopped = False
    for _ in range(10):
        v = mon.poll(_silence(3.0), SR)
        if v.should_stop:
            stopped = True
            break
    assert stopped


def test_quiet_speech_above_threshold_not_silence():
    """轻声说话（RMS 约 0.01）必须算有声，不能被当静音。"""
    mon = LiveSilenceMonitor(silence_rms=0.002, stop_after_seconds=10)
    for _ in range(10):
        v = mon.poll(_tone(3.0, amp=0.01), SR)   # RMS ≈ 0.007
    assert not v.should_stop
    assert v.heard_sound


def test_empty_audio_not_checked():
    mon = LiveSilenceMonitor(stop_after_seconds=10)
    v = mon.poll(np.zeros(0, dtype=np.float32), SR)
    assert v.checked is False
    assert v.should_stop is False


def test_short_audio_below_one_block_not_checked():
    """不足一个块时不判定，避免把残余样本当读数。"""
    mon = LiveSilenceMonitor(stop_after_seconds=10, block_seconds=0.5)
    v = mon.poll(_silence(0.2), SR)
    assert v.checked is False


def test_reset_clears_state():
    mon = LiveSilenceMonitor(stop_after_seconds=10)
    mon.poll(_tone(3.0), SR)
    mon.poll(_silence(9.0), SR)
    assert mon.silent_seconds > 0
    mon.reset()
    assert mon.silent_seconds == 0.0
    assert mon.heard_sound is False


# ---------------- 读取录制中的 WAV 尾段 ----------------

def test_read_tail_returns_last_seconds(tmp_path: Path):
    audio = np.concatenate([_tone(5.0, amp=0.0), _tone(5.0, amp=0.3)])
    p = _write_wav(tmp_path / "a.wav", audio)
    tail, sr = read_tail_mono(p, max_seconds=3.0)
    assert sr == SR
    assert 2.9 <= len(tail) / sr <= 3.1
    # 末尾是有声部分
    assert float(np.sqrt(np.mean(tail.astype(np.float64) ** 2))) > 0.01


def test_read_tail_survives_unfinalized_header(tmp_path: Path):
    """录制中 WAV 头长度是占位值 —— 必须按文件实际大小读。"""
    audio = np.concatenate([_tone(4.0), _silence(4.0)])
    p = _write_wav(tmp_path / "b.wav", audio, header_frames=0)
    tail, sr = read_tail_mono(p, max_seconds=3.0)
    assert sr == SR
    assert len(tail) > 0, "头部长度是占位值时读不到数据"
    assert len(tail) / sr >= 2.5


def test_read_tail_missing_file(tmp_path: Path):
    tail, sr = read_tail_mono(tmp_path / "nope.wav")
    assert sr == 0 and tail.size == 0


def test_read_tail_tiny_file(tmp_path: Path):
    p = tmp_path / "tiny.wav"
    p.write_bytes(b"RIFF")
    tail, sr = read_tail_mono(p)
    assert sr == 0 and tail.size == 0


def test_read_tail_stereo_downmixed(tmp_path: Path):
    mono = _tone(2.0)
    stereo = np.stack([mono, mono], axis=1)
    path = tmp_path / "s.wav"
    path.parent.mkdir(parents=True, exist_ok=True)
    pcm = (np.clip(stereo, -1, 1) * 32767).astype("<i2").tobytes()
    with wave.open(str(path), "wb") as wf:
        wf.setnchannels(2)
        wf.setsampwidth(2)
        wf.setframerate(SR)
        wf.writeframes(pcm)

    tail, sr = read_tail_mono(path, max_seconds=2.0)
    assert sr == SR
    assert tail.ndim == 1
    assert 1.9 <= len(tail) / sr <= 2.1


def test_end_to_end_stream_ends_by_silence(tmp_path: Path):
    """模拟真实场景：说话 10 秒 → 静音 50 秒 → 应按静音停止。"""
    mon = LiveSilenceMonitor(stop_after_seconds=40)
    p = _write_wav(tmp_path / "live.wav", _tone(10.0))

    # 前 10 秒有声
    tail, sr = read_tail_mono(p, max_seconds=4.0)
    v = mon.poll(tail, sr)
    assert v.heard_sound and not v.should_stop

    # 追加 50 秒静音（wave 不支持 'ab'，直接按字节追加裸 PCM，
    # 这恰好也模拟了"录制中头部还没回填"的真实状态）
    pcm = (np.zeros(SR * 50, dtype=np.float32) * 32767).astype("<i2").tobytes()
    with p.open("ab") as fh:
        fh.write(pcm)
    tail, sr = read_tail_mono(p, max_seconds=4.0)
    v = mon.poll(tail, sr)
    assert v.checked
    assert v.silent_seconds >= 3.0


# ---------------- 回归：三个真实 bug（2026-11-23 对立性审查发现） ----------------

def test_no_double_count_on_overlapping_window(tmp_path: Path):
    """BUG-1 回归：读 4 秒窗口但只推进 3 秒，重叠 1 秒不能重复计数。

    修复前实测：真实静音 33 秒被判成 43 秒（虚高 30%），导致提前 10 秒收工。
    """
    mon = LiveSilenceMonitor(stop_after_seconds=40, silence_rms=0.002)
    p = _write_wav(tmp_path / "live.wav", _tone(5.0))

    tail, sr, start = livesilence.read_tail_window(p, max_seconds=4.0)
    mon.poll(tail, sr, audio_offset=start)
    assert mon.heard_sound

    real_silence = 0.0
    for _ in range(14):
        with p.open("ab") as fh:
            fh.write((np.zeros(SR * 3, dtype=np.float32) * 32767).astype("<i2").tobytes())
        real_silence += 3.0
        tail, sr, start = livesilence.read_tail_window(p, max_seconds=4.0)
        v = mon.poll(tail, sr, audio_offset=start)
        if v.should_stop:
            break

    assert v.should_stop, "静音 42 秒仍未判停"
    # 计数器与真实静音时长必须一致（允许一个块 0.5s 的粒度误差）
    assert abs(v.silent_seconds - real_silence) <= 1.0, (
        f"计数虚高：认为 {v.silent_seconds}s，实际 {real_silence}s"
    )


def test_frozen_file_does_not_inflate_silence(tmp_path: Path):
    """BUG-2 回归：文件停止增长时，不能把同一段旧音频反复计入。

    修复前实测：真实静音 45 秒 + 文件冻结 → 计数器仍能一路涨，
    判停时机由上轮询次数决定而不是真实时长。
    """
    mon = LiveSilenceMonitor(stop_after_seconds=40, silence_rms=0.002)
    p = _write_wav(tmp_path / "frozen.wav", _tone(5.0))
    tail, sr, start = livesilence.read_tail_window(p, max_seconds=4.0)
    mon.poll(tail, sr, audio_offset=start)

    # 追加 10 秒静音后文件冻结
    with p.open("ab") as fh:
        fh.write((np.zeros(SR * 10, dtype=np.float32) * 32767).astype("<i2").tobytes())

    counts = []
    for _ in range(20):                      # 反复读同一个冻结窗口
        tail, sr, start = livesilence.read_tail_window(p, max_seconds=4.0)
        v = mon.poll(tail, sr, audio_offset=start)
        counts.append(mon.silent_seconds)

    # 真实静音只有 10 秒 → 计数器不该超过太多
    assert mon.silent_seconds <= 12.0, (
        f"文件冻结导致静音计数虚增到 {mon.silent_seconds}s（真实约 10s）"
    )
    assert not v.should_stop


def test_read_tail_window_returns_start_offset(tmp_path: Path):
    """read_tail_window 必须给出窗口在文件中的起始秒。"""
    audio = np.concatenate([_tone(10.0), _silence(10.0)])
    p = _write_wav(tmp_path / "w.wav", audio)
    tail, sr, start = livesilence.read_tail_window(p, max_seconds=4.0)
    assert sr == SR
    assert 15.5 <= start <= 16.5, f"起始秒应在 16s 附近，实际 {start}"


def test_stop_reason_persisted_to_disk(tmp_path: Path):
    """BUG-3 回归：静音停止原因必须落盘，重启后还能看到。"""
    import tempfile

    from wxlive.nas_runner import NasJobRunner

    d = tmp_path / "data"
    r = NasJobRunner(data_dir=d, engine_factory=lambda t, l: None)
    try:
        r._jobs["j1"] = {"id": "j1", "title": "t", "status": "recording",
                         "logs": [], "created_at": 0}
        r._save_jobs()
        r._update_job("j1", stop_reason="连续静音 40 秒，判定直播已结束", persist=True)
    finally:
        r.shutdown()

    r2 = NasJobRunner(data_dir=d, engine_factory=lambda t, l: None)
    try:
        j = r2.get_job("j1")
        assert j is not None
        assert j.get("stop_reason") == "连续静音 40 秒，判定直播已结束"
    finally:
        r2.shutdown()


def test_m1_leftover_not_swallowed(tmp_path: Path):
    """M1 回归：不足一个块的新增音频不能被标记为"已消费"然后永久丢失。

    修复前实测：追加 0.2s 后下一轮只计 3.0s（应 3.2s），
    导致 40 秒阈值系统性偏松。
    """
    mon = LiveSilenceMonitor(stop_after_seconds=999, silence_rms=0.002,
                             block_seconds=0.5)
    p = _write_wav(tmp_path / "a.wav", _tone(5.0))
    tail, sr, start = livesilence.read_tail_window(p, max_seconds=5.5)
    mon.poll(tail, sr, audio_offset=start)
    before = mon.silent_seconds

    # 追加 0.2 秒静音（不足一个 0.5s 块）
    with p.open("ab") as fh:
        fh.write((np.zeros(int(SR * 0.2), dtype=np.float32) * 32767).astype("<i2").tobytes())
    tail, sr, start = livesilence.read_tail_window(p, max_seconds=5.5)
    v = mon.poll(tail, sr, audio_offset=start)
    assert v.checked is False
    # 这段不能被消费掉：再追 0.4 秒后应能凑够一个块并计入
    with p.open("ab") as fh:
        fh.write((np.zeros(int(SR * 0.4), dtype=np.float32) * 32767).astype("<i2").tobytes())
    tail, sr, start = livesilence.read_tail_window(p, max_seconds=5.5)
    v2 = mon.poll(tail, sr, audio_offset=start)
    assert v2.checked, "0.2+0.4 秒仍未凑成一个块 —— 尾巴被吞了"
    assert mon.silent_seconds >= 0.5


def test_min_record_seconds_blocks_early_stop_then_stops_after_guard():
    """最短录制时长保护：前 300s 内连续静音>=40s 不停；恢复说话清零；满 300s 后立即停。"""
    mon = LiveSilenceMonitor(
        stop_after_seconds=40,
        min_record_seconds=300,
    )
    # 0..30s 有声
    v = mon.poll(_tone(30.0), SR, audio_offset=0.0, elapsed_seconds=30.0)
    assert v.heard_sound is True
    assert not v.should_stop

    # 30..80s 连续静音 50s（已超 40s 阈值，但总时长仅 80s < 300s）-> 不能停
    v = mon.poll(_silence(50.0), SR, audio_offset=30.0, elapsed_seconds=80.0)
    assert v.silent_seconds == pytest.approx(50.0, abs=0.5)
    assert v.should_stop is False
    assert "最短保护时长" in v.reason

    # 80..90s 主播回来继续说话 -> 静音计数必须清零
    v = mon.poll(_tone(10.0), SR, audio_offset=80.0, elapsed_seconds=90.0)
    assert v.silent_seconds == 0.0
    assert v.should_stop is False

    # 90..260s 讲话后，260..299s 静音 39s -> 未满 40s 且未满 300s
    v = mon.poll(_tone(170.0), SR, audio_offset=90.0, elapsed_seconds=260.0)
    v = mon.poll(_silence(39.0), SR, audio_offset=260.0, elapsed_seconds=299.0)
    assert v.should_stop is False

    # 299..305s 继续静音 6s（累计 45s >= 40s，且已录 305s >= 300s）-> 立即判停
    v = mon.poll(_silence(6.0), SR, audio_offset=299.0, elapsed_seconds=305.0)
    assert v.should_stop is True
    assert "连续静音" in v.reason

