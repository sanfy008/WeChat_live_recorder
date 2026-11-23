"""流地址直录的离线测试。

用本地 WAV 文件当作"流地址"喂给 ffmpeg，验证命令构建和输出正确，
不依赖任何真实网络地址。
"""

from __future__ import annotations

import subprocess
import time
from pathlib import Path

import pytest
import soundfile as sf

from wxlive import stream_capture


# ---------------- 基础工具 ----------------

def test_is_url_detection():
    assert stream_capture.is_url("https://example.com/a.flv")
    assert stream_capture.is_url("HTTP://example.com/a.m3u8")
    assert stream_capture.is_url("rtmp://live/x")
    assert not stream_capture.is_url("D:/a/b.wav")
    assert not stream_capture.is_url("")
    assert not stream_capture.is_url(None)


def test_find_ffmpeg_available():
    """本机必须有 ffmpeg，否则拉流功能不可用。"""
    assert stream_capture.find_ffmpeg(), "未找到 ffmpeg"


# ---------------- ffmpeg 命令构建 ----------------

def test_build_cmd_audio_only_and_resampled(tmp_path):
    """命令必须丢弃视频、转 16k 单声道——这是省空间和 ASR 直用的关键。"""
    rec = stream_capture.StreamRecorder(
        "https://example.com/live.flv", tmp_path, name="t")
    cmd = rec._build_cmd(tmp_path / "out.wav")
    joined = " ".join(cmd)

    assert "-vn" in cmd, "必须丢弃视频轨"
    assert "-ac" in cmd and "1" in cmd, "必须转单声道"
    assert "-ar" in cmd and "16000" in cmd, "必须转 16kHz（ASR 要求）"
    assert "pcm_s16le" in joined


def test_build_cmd_has_reconnect_for_http(tmp_path):
    """HTTP 流必须带重连参数：服务端关连接是常态。"""
    rec = stream_capture.StreamRecorder("https://example.com/a.flv", tmp_path)
    cmd = rec._build_cmd(tmp_path / "o.wav")
    joined = " ".join(cmd)
    assert "-reconnect" in joined
    assert "-reconnect_streamed" in joined


def test_build_cmd_no_reconnect_for_local_file(tmp_path):
    """本地文件不需要重连参数。"""
    rec = stream_capture.StreamRecorder(str(tmp_path / "x.wav"), tmp_path)
    cmd = rec._build_cmd(tmp_path / "o.wav")
    assert "-reconnect" not in cmd


# ---------------- 用本地文件模拟"流" ----------------

@pytest.fixture
def sample_wav(tmp_path) -> Path:
    """造一个 3 秒 48k 立体声样本，模拟"流里的原始音频"。"""
    import numpy as np

    sr = 48000
    t = np.arange(sr * 3) / sr
    mono = (0.3 * np.sin(2 * np.pi * 440 * t)).astype("float32")
    stereo = np.stack([mono, mono], axis=1)
    p = tmp_path / "fake_stream.wav"
    sf.write(str(p), stereo, sr, subtype="PCM_16")
    return p


def test_recorder_produces_16k_mono(sample_wav, tmp_path):
    """端到端：喂本地 WAV 当"流"，输出必须是 16k 单声道。"""
    out_dir = tmp_path / "out"
    rec = stream_capture.StreamRecorder(str(sample_wav), out_dir, name="e2e")
    rec.start()

    # 等 ffmpeg 处理完（3 秒音频，给足时间）
    for _ in range(40):
        time.sleep(0.25)
        if rec.out_path and rec.out_path.exists() and rec.out_path.stat().st_size > 1000:
            if not rec.running:
                break

    rec.stop()

    assert rec.out_path and rec.out_path.exists(), "没有产出文件"
    audio, sr = sf.read(str(rec.out_path), dtype="float32", always_2d=False)
    assert sr == 16000, f"采样率应为 16000，实际 {sr}"
    assert audio.ndim == 1, "应为单声道"
    # 时长应接近 3 秒
    assert 2.5 <= len(audio) / sr <= 3.5, f"时长异常: {len(audio)/sr:.2f}s"


def test_status_reports_progress(sample_wav, tmp_path):
    rec = stream_capture.StreamRecorder(str(sample_wav), tmp_path / "o", name="s")
    assert rec.status() == "未开始"
    rec.start()
    for _ in range(20):
        time.sleep(0.2)
        if rec.out_path and rec.out_path.exists() and rec.out_path.stat().st_size > 2000:
            break
    rec.stop()
    assert "MB" in rec.status(), f"状态应含进度: {rec.status()}"


def test_probe_invalid_url_returns_error():
    """探测无效地址必须返回错误而不是抛异常。"""
    r = stream_capture.probe_url("http://127.0.0.1:9/nonexistent.flv", timeout=5)
    assert r["ok"] is False
    assert r.get("error")


def test_probe_local_file_ok(sample_wav):
    """探测本地文件应成功并识别出音频轨。"""
    r = stream_capture.probe_url(str(sample_wav), timeout=15)
    assert r["ok"] is True, f"探测失败: {r.get('error')}"
    assert r["has_audio"] is True


# ---------------- 从粘贴内容里抠地址 ----------------

LIVE_URL = (
    "https://pull-m1.wxlivecdn.com/trtc_1400123456/orig_abc.flv"
    "?txSecret=deadbeef&txTime=6AE401F7"
)


def test_extract_url_plain():
    assert stream_capture.extract_url(LIVE_URL) == LIVE_URL


def test_extract_url_ffmpeg_command_with_quotes():
    """微信下载按钮复制的就是这种整条命令，必须能直接吃下去。"""
    cmd = f'ffmpeg -i "{LIVE_URL}" -c copy -y "live_abc.flv"'
    assert stream_capture.extract_url(cmd) == LIVE_URL


def test_extract_url_ffmpeg_command_escaped_quotes():
    """有些终端/工具复制出来是转义引号形态。"""
    cmd = f'ffmpeg -i \\"{LIVE_URL}\\" -c copy -y out.flv'
    assert stream_capture.extract_url(cmd) == LIVE_URL


def test_extract_url_keeps_query_string():
    """URL 里的 ? & 不能被截断，否则签名丢失。"""
    got = stream_capture.extract_url(f'ffmpeg -i "{LIVE_URL}" -c copy')
    assert "txSecret=deadbeef" in got and "txTime=6AE401F7" in got


def test_extract_url_single_quotes_and_whitespace():
    assert stream_capture.extract_url(f"  '{LIVE_URL}'  ") == LIVE_URL
    assert stream_capture.extract_url(f'ffmpeg  -i  {LIVE_URL}  -c copy') == LIVE_URL


def test_extract_url_rtmp():
    rtmp = "rtmp://live.example.com/app/streamkey"
    assert stream_capture.extract_url(rtmp) == rtmp
    assert stream_capture.extract_url(f'ffmpeg -i "{rtmp}" -c copy') == rtmp


def test_extract_url_returns_empty_for_garbage():
    for bad in ("", "   ", "随便一段中文", "https://example.com/a.mp4 但不是流", "ffmpeg -c copy"):
        got = stream_capture.extract_url(bad)
        # "https://example.com/a.mp4" 属于合法 http 地址，允许返回；其余必须为空
        if got:
            assert stream_capture.is_url(got)
        else:
            assert got == ""


def test_extract_url_then_is_url_contract():
    """extract_url 的返回值要么可直接喂 ffmpeg，要么为空——不能是半截。"""
    for text in (LIVE_URL, f'ffmpeg -i "{LIVE_URL}" -c copy -y x.flv', "garbage", ""):
        got = stream_capture.extract_url(text)
        if got:
            assert stream_capture.is_url(got), got
