"""按位置增量读音频的回归测试（2026-11-23 真机实测后重写）。

真实数据（本机 ffmpeg 实测）：
    7.8s  -> 262144B (8.19s)
   16.1s  -> 524288B (16.38s)
   24.1s  -> 786432B (24.58s)
每 8 秒落一块，每块正好 8 秒音频。

旧实现（read_tail_window 每 3 秒读末尾 4.5 秒）在这个节奏下：
  - 8 秒周期里只有 1 次读到新数据，另 2 次空转
  - 4.5 秒窗口只覆盖 8 秒块的一半，另一半永远丢

真机后果：150 分钟直播，静音检测只成功跑了 **3 次**，判停完全失效，
最后靠播放地址 404 才收工，白录一小时以上。

新实现 read_since(path, from_sec) 从上次位置读到现在，
每块音频都被完整分析一次。合成场景实测 40s 静音在 48s 准确判停。
"""

from __future__ import annotations

import struct

import numpy as np
import pytest

from wxlive.livesilence import LiveSilenceMonitor, read_since

SR = 16000


def _write_wav(path, chunks):
    """写一个"边写边录"形态的 WAV（头部 data size 声明为 0，真实长度为准）。"""
    def hdr(n):
        return (b"RIFF" + struct.pack("<I", 36 + n) + b"WAVE"
                + b"fmt " + struct.pack("<IHHIIHH", 16, 1, 1, SR, SR * 2, 2, 16)
                + b"data" + struct.pack("<I", n))

    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("wb") as f:
        f.write(hdr(0))
        for c in chunks:
            f.write(c)


def _silence(seconds):
    return np.zeros(int(SR * seconds), dtype=np.float32).astype("<i2").tobytes()


def _tone(seconds, amp=0.3):
    a = np.full(int(SR * seconds), amp, dtype=np.float32)
    return (a * 32767).astype("<i2").tobytes()


def test_read_since_returns_nothing_when_no_new_data(tmp_path):
    p = tmp_path / "a.wav"
    _write_wav(p, [_tone(2.0)])
    audio, sr, start = read_since(p, 2.0)
    assert audio.size == 0


def test_read_since_covers_entire_new_block(tmp_path):
    """整块 8 秒必须一次读全 —— 这是零丢弃的核心。"""
    p = tmp_path / "b.wav"
    _write_wav(p, [_tone(2.0), _silence(8.0)])
    audio, sr, start = read_since(p, 2.0)
    assert sr == SR
    assert start == pytest.approx(2.0, abs=1e-6)
    assert audio.size == 8 * SR, "没有读全整块（会漏掉声音/静音）"


def test_read_since_no_gap_between_consecutive_reads(tmp_path):
    """连续读两次，区间必须首尾相接，不重不漏。"""
    p = tmp_path / "c.wav"
    _write_wav(p, [_tone(4.0), _silence(8.0), _tone(8.0)])

    # read_since 默认 max_seconds=60，20 秒数据一次读完；
    # 这里故意用 10 秒上限，逼出"分两次读"的行为来验证拼接。
    a1, s1, st1 = read_since(p, 0.0, max_seconds=10.0)
    nxt = st1 + a1.size / float(s1)
    a2, s2, st2 = read_since(p, nxt, max_seconds=10.0)

    assert st1 == 0.0
    assert st2 == pytest.approx(nxt, abs=1e-6)
    assert a1.size == 10 * SR
    assert a1.size + a2.size == 20 * SR


def test_read_since_respects_max_seconds(tmp_path):
    """一次读太多要截断，由下一轮继续（防内存爆）。"""
    p = tmp_path / "d.wav"
    _write_wav(p, [_silence(60.0)])
    audio, sr, _ = read_since(p, 0.0, max_seconds=10.0)
    assert audio.size == 10 * SR


def test_read_since_handles_lying_header(tmp_path):
    """ffmpeg 边写边录时头里的 data size 常是 0 —— 必须按真实文件长度算。"""
    p = tmp_path / "e.wav"
    _write_wav(p, [_silence(5.0)])
    audio, sr, _ = read_since(p, 0.0)
    assert audio.size == 5 * SR


def test_chunked_growth_triggers_stop_accurately(tmp_path):
    """端到端：8 秒块节奏下，40 秒静音必须准确判停。

    旧实现在同场景要 81 秒才停（慢 33 秒）；实测真机更糟，
    150 分钟只跑通 3 次检测。这里钉住 44-56 秒的合理区间。
    """
    p = tmp_path / "f.wav"
    _write_wav(p, [_tone(6.0)])          # 先有 6 秒声音

    monitor = LiveSilenceMonitor(silence_rms=0.01, stop_after_seconds=40.0,
                                 require_sound_before=True)
    analyzed = 0.0
    wrote = 6.0
    t = 0.0
    stopped_at = None

    while t < 90:
        t += 3.0
        while wrote + 8.0 <= t + 0.01:
            with p.open("ab") as f:
                f.write(_silence(8.0))
            wrote += 8.0

        audio, sr, start = read_since(p, analyzed)
        if audio.size == 0 or sr <= 0:
            continue
        v = monitor.poll(audio, sr, audio_offset=start)
        analyzed = start + audio.size / float(sr)
        if v.should_stop:
            stopped_at = t
            break

    assert stopped_at is not None, "40 秒静音没有触发停止（回归了）"
    assert 44 <= stopped_at <= 56, f"判停时刻异常：{stopped_at}"


def test_no_stop_when_blocks_contain_sound(tmp_path):
    """块里一直有声音就不该停 —— 别把正常直播掐了。"""
    p = tmp_path / "g.wav"
    _write_wav(p, [_tone(6.0)])

    monitor = LiveSilenceMonitor(silence_rms=0.01, stop_after_seconds=40.0,
                                 require_sound_before=True)
    analyzed = 0.0
    wrote = 6.0
    t = 0.0
    while t < 90:
        t += 3.0
        while wrote + 8.0 <= t + 0.01:
            with p.open("ab") as f:
                f.write(_tone(8.0))
            wrote += 8.0
        audio, sr, start = read_since(p, analyzed)
        if audio.size == 0 or sr <= 0:
            continue
        v = monitor.poll(audio, sr, audio_offset=start)
        analyzed = start + audio.size / float(sr)
        assert not v.should_stop, f"有声音却判停了（t={t}）"
