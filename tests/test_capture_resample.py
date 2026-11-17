"""采集端降采样回归测试。

WASAPI loopback 只能按设备原生率打开（实测 16k/32k/44.1k 全部 -9997），
所以降采样必须在消费侧做。这里钉死四件事：
1. 重采样后时长不变（否则 WAV 与逐字稿时间轴对不上）
2. 只重采样一次（二次重采样会把时长变 1/3、频率变 3 倍）
3. 旧的 48k WAV 仍能正常转写
4. stop() 时 flush 尾部，结尾不少采样
"""

from __future__ import annotations

import argparse

import numpy as np
import soundfile as sf

from wxlive.audio_capture import DeviceInfo, LoopbackRecorder

DEV_RATE = 48000
OUT_RATE = 16000


def make_tone(freq: float, sec: float, sr: int, channels: int = 1) -> np.ndarray:
    t = np.arange(int(sr * sec)) / sr
    x = 0.5 * np.sin(2 * np.pi * freq * t)
    if channels > 1:
        x = np.repeat(x[:, None], channels, axis=1)
    return x.astype(np.float32)


def make_tone_pcm16(freq: float, sec: float, sr: int = DEV_RATE,
                    channels: int = 2) -> np.ndarray:
    return np.clip(make_tone(freq, sec, sr, channels) * 32767, -32768, 32767).astype(np.int16)


def fake_recorder(**kwargs) -> LoopbackRecorder:
    """不碰真实声卡：显式传 DeviceInfo，__init__ 不会调用 pyaudio。"""
    return LoopbackRecorder(
        device=DeviceInfo(index=-1, name="fake-loopback", rate=DEV_RATE, channels=2),
        **kwargs,
    )


def feed(rec: LoopbackRecorder, pcm: np.ndarray, chunk_sec: float = 0.1) -> None:
    """模拟 PortAudio 回调：按块推送 int16 字节。"""
    step = int(DEV_RATE * chunk_sec)
    for i in range(0, len(pcm), step):
        rec._callback(pcm[i:i + step].tobytes(), 0, None, None)


def drain(rec: LoopbackRecorder) -> np.ndarray:
    parts: list[np.ndarray] = []
    for _ in range(10000):
        c = rec.read(timeout=0.0)
        if c is None:
            break
        if len(c):
            parts.append(c)
    return np.concatenate(parts) if parts else np.zeros(0, dtype=np.float32)


def dominant_freq(x: np.ndarray, sr: int) -> float:
    if len(x) == 0:
        return 0.0
    spec = np.abs(np.fft.rfft(x * np.hanning(len(x))))
    freqs = np.fft.rfftfreq(len(x), 1 / sr)
    return float(freqs[int(np.argmax(spec))])


# ---------- 1. 时长不变 ----------

def test_duration_preserved_after_resample():
    rec = fake_recorder(out_rate=OUT_RATE, chunk_seconds=0.1)
    assert rec.out_rate == OUT_RATE
    feed(rec, make_tone_pcm16(440.0, 5.0))
    rec.stop()
    y = drain(rec)

    dur = len(y) / OUT_RATE
    assert abs(dur - 5.0) / 5.0 < 0.001, f"重采样后时长漂移: {dur:.4f}s"


def test_frequency_preserved_after_resample():
    """440Hz 进来必须还是 440Hz；二次重采样会变成 ~1320Hz。"""
    rec = fake_recorder(out_rate=OUT_RATE, chunk_seconds=0.1)
    feed(rec, make_tone_pcm16(440.0, 3.0))
    rec.stop()
    y = drain(rec)

    assert abs(dominant_freq(y, OUT_RATE) - 440.0) < 15.0


def test_default_out_rate_is_device_rate():
    rec = fake_recorder()
    assert rec.out_rate == DEV_RATE
    assert rec._resampler is None


# ---------- 2. 只重采样一次 ----------

def test_pipeline_wiring_resamples_only_once(monkeypatch):
    """Pipeline 的兜底 resampler 必须与 recorder 输出率一致（走 passthrough）。

    如果有人把 in_rate 改回 device.rate，16k 会被再当 48k 转一次：
    时长变 1/3、频率变 3 倍，ASR 输出全是幻觉。
    """
    from wxlive import pipeline as P
    from wxlive.config import Config

    rec = fake_recorder(out_rate=OUT_RATE)

    class FakeSession:
        def __init__(self, base, name="", sample_rate=16000, **kwargs):
            self.sample_rate = sample_rate

    monkeypatch.setattr(P, "LoopbackRecorder", lambda **kw: rec)
    monkeypatch.setattr(P, "Session", FakeSession)
    monkeypatch.setattr(P, "build_engine", lambda cfg: None)

    cfg = Config()
    p = P.Pipeline(cfg)

    assert p.recorder.out_rate == cfg.audio.sample_rate == 16000
    assert p.resampler._passthrough is True, "Pipeline 二次重采样了 16k 音频"
    assert p.session.sample_rate == 16000
    assert p.segmenter.sr == 16000

    # 功能验证：recorder 输出喂给 pipeline resampler，长度/频率都不变
    x = make_tone(440.0, 1.0, OUT_RATE)
    assert len(p.resampler.process(x)) == len(x)


# ---------- 3. 旧 48k WAV 仍可转写 ----------

def test_transcribe_old_48k_wav(tmp_path, monkeypatch):
    from wxlive import cli

    src = tmp_path / "old48k.wav"
    sf.write(str(src), make_tone(440.0, 3.0, 48000), 48000, subtype="PCM_16")

    captured: dict = {}

    class FakeEngine:
        stats = {}

        def transcribe(self, audio, sample_rate=16000):
            captured["sr"] = sample_rate
            # 分块转写时会调多次（含 flush 的尾部），时长要累加
            captured["dur"] = captured.get("dur", 0.0) + len(audio) / sample_rate
            return [{"start": 0.0, "end": len(audio) / sample_rate, "text": "测试"}]

    monkeypatch.setattr("wxlive.pipeline.build_engine", lambda cfg: FakeEngine())
    monkeypatch.setattr(cli, "TRANSCRIPTS_DIR", tmp_path / "transcripts")
    monkeypatch.setattr("wxlive.config.ensure_dirs", lambda: None)

    args = argparse.Namespace(
        input=str(src), engine="local", gemini_model="x", no_lexicon=True,
        lexicon_path="", model="small", device_type="cpu", compute_type="int8",
        language="zh", workers=1,
    )
    assert cli.cmd_transcribe(args) == 0
    assert captured["sr"] == 16000
    assert abs(captured["dur"] - 3.0) < 0.01, "48k 旧文件重采样后时长不对"


# ---------- 4. stop() 时 flush，结尾不丢采样 ----------

def test_stop_flushes_resampler_tail():
    rec = fake_recorder(out_rate=OUT_RATE, chunk_seconds=0.1)
    feed(rec, make_tone_pcm16(300.0, 2.0))

    # 先正常消费完队列（不含尾部）
    while len(rec.read(timeout=0.0)):
        pass
    # 流式重采样器此时应仍有内部残余（soxr 的滤波器延迟）
    assert rec._resampler is not None
    tail = rec._flush_resampler()
    assert len(tail) > 0, "soxr 流式重采样器没有残余样本，flush 逻辑无从验证"
    # 幂等：第二次调用返回空
    assert len(rec._flush_resampler()) == 0


def test_stop_tail_delivered_through_read():
    rec = fake_recorder(out_rate=OUT_RATE, chunk_seconds=0.1)
    feed(rec, make_tone_pcm16(300.0, 2.0))
    rec.stop()

    y = drain(rec)
    expected = 2.0 * OUT_RATE
    assert abs(len(y) - expected) <= 64, (
        f"结尾丢采样：得到 {len(y)}，期望 {expected}（flush 未生效）")
    assert rec.read(timeout=0.0) is None
