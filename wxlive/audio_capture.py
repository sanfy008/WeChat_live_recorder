"""WASAPI loopback 音频采集。

设计要点：
1. **打开**流必须用设备原生采样率（WASAPI loopback 不接受别的值，
   实测传 16000/32000/44100 全部抛 -9997）。需要降采样时用 out_rate，
   在**消费侧**（read()）用 StreamResampler 转换 —— 不在 PortAudio 回调里做，
   回调里做重计算会有爆音/丢块风险。
2. 采集在独立线程的回调里进行，回调内只做最小动作（追加到队列），
   绝不做 IO 或转写，否则会阻塞音频缓冲导致丢帧。
3. 断流/设备切换时自动重连（对应调研文档里「断流是常态」的教训）。
"""

from __future__ import annotations

import queue
import sys
import threading
import time
from dataclasses import dataclass

import numpy as np

try:
    import pyaudiowpatch as pyaudio
except ImportError:  # 非 Windows 或无声卡依赖环境（如 NAS Docker）
    pyaudio = None  # type: ignore[assignment]

from .resample import StreamResampler


def _require_pyaudio():
    if pyaudio is None:
        raise RuntimeError(
            "当前环境未安装 pyaudiowpatch（WASAPI 声卡回环采集仅支持 Windows）"
        )
    return pyaudio


@dataclass
class DeviceInfo:
    index: int
    name: str
    rate: int
    channels: int


def list_loopback_devices() -> list[DeviceInfo]:
    """列出所有可用的 WASAPI loopback 设备（即"能录到扬声器声音"的设备）。"""
    pa = _require_pyaudio()
    p = pa.PyAudio()
    try:
        out: list[DeviceInfo] = []
        try:
            wasapi = p.get_host_api_info_by_type(pa.paWASAPI)
        except OSError:
            return []
        for i in range(wasapi["deviceCount"]):
            idx = p.get_host_api_info_by_type(pa.paWASAPI)["index"]
            dev = p.get_device_info_by_host_api_device_index(idx, i)
            if dev.get("isLoopbackDevice") and dev.get("maxInputChannels", 0) > 0:
                out.append(
                    DeviceInfo(
                        index=int(dev["index"]),
                        name=str(dev["name"]),
                        rate=int(dev["defaultSampleRate"]),
                        channels=int(dev["maxInputChannels"]),
                    )
                )
        return out
    finally:
        p.terminate()


def pick_device(hint: str = "") -> DeviceInfo:
    """按关键字挑选 loopback 设备；hint 为空则取系统默认输出对应的 loopback。"""
    devices = list_loopback_devices()
    if not devices:
        raise RuntimeError(
            "未找到 WASAPI loopback 设备。请确认系统有可用的音频输出设备，"
            "且当前为 Windows 平台。"
        )
    if hint:
        matched = [d for d in devices if hint.lower() in d.name.lower()]
        if matched:
            return matched[0]
        print(f"[warn] 未匹配到设备 '{hint}'，回退到默认设备", file=sys.stderr)

    pa = _require_pyaudio()
    p = pa.PyAudio()
    try:
        default = p.get_default_wasapi_loopback()
        for d in devices:
            if d.index == int(default["index"]):
                return d
    except Exception:
        pass
    finally:
        p.terminate()
    return devices[0]


class LoopbackRecorder:
    """把系统播放的声音持续读进一个队列，供上层消费。

    回调里只做 `queue.put_nowait`，绝不阻塞。
    """

    def __init__(
        self,
        device: DeviceInfo | None = None,
        device_hint: str = "",
        chunk_seconds: float = 0.1,
        mono: bool = True,
        max_queue_chunks: int = 3000,
        out_rate: int | None = None,
    ) -> None:
        """out_rate=None 保持旧行为（输出设备原生采样率）；
        指定后 read() 返回的块是重采样后的 out_rate。
        """
        self.device = device or pick_device(device_hint)
        self.chunk_seconds = chunk_seconds
        self.mono = mono
        self.queue: queue.Queue[np.ndarray | None] = queue.Queue(maxsize=max_queue_chunks)

        self._pa: pyaudio.PyAudio | None = None
        self._stream = None
        self._running = False
        self._thread: threading.Thread | None = None
        self._out_rate = out_rate
        # frames_per_buffer 必须按原生率算：这是给 PortAudio 的采集参数，
        # 与输出采样率无关，重采样只改变样本数，不改变时间轴。
        self._frames_per_buffer = int(self.device.rate * chunk_seconds)
        self.dropped_chunks = 0
        # 停止原因（由 stop(reason=...) 写入），用于事后归因
        self.stop_reason = ""

        self._resampler = self._make_resampler()
        self._lock = threading.Lock()
        self._final: list[np.ndarray] = []  # 结束时 flush 出的尾部样本
        self._eof = False
        self._stopping = False

    # ---------- 采样率 ----------

    @property
    def out_rate(self) -> int:
        """调用方实际会拿到的采样率（未指定 out_rate 时等于设备原生率）。"""
        return self._out_rate if self._out_rate is not None else self.device.rate

    def _make_resampler(self) -> StreamResampler | None:
        if self._out_rate is None or self._out_rate == self.device.rate:
            return None
        return StreamResampler(self.device.rate, self._out_rate)

    def _process(self, chunk: np.ndarray) -> np.ndarray:
        rs = self._resampler
        return chunk if rs is None else rs.process(chunk)

    def _flush_resampler(self) -> np.ndarray:
        """取出重采样器内部残余样本；幂等，重复调用返回空数组。

        调用方必须持有 self._lock。
        """
        rs = self._resampler
        if rs is None:
            return np.zeros(0, dtype=np.float32)
        self._resampler = None
        return rs.flush()

    # ---------- 生命周期 ----------

    def start(self) -> None:
        if self._running:
            return
        pa = _require_pyaudio()
        self._pa = pa.PyAudio()
        self._open_stream()
        self._running = True
        self._thread = threading.Thread(target=self._guard_loop, daemon=True, name="loopback-guard")
        self._thread.start()
        print(f"[audio] 开始采集: {self.device.name} @ {self.device.rate}Hz "
              f"{self.device.channels}ch  块={self.chunk_seconds}s  "
              f"输出={self.out_rate}Hz")

    def _open_stream(self) -> None:
        assert self._pa is not None
        self._stream = self._pa.open(
            format=pyaudio.paInt16,
            channels=self.device.channels,
            rate=self.device.rate,
            input=True,
            input_device_index=self.device.index,
            frames_per_buffer=self._frames_per_buffer,
            stream_callback=self._callback,
        )
        self._stream.start_stream()

    def _callback(self, in_data, frame_count, time_info, status):
        if in_data:
            a = np.frombuffer(in_data, dtype=np.int16)
            if self.mono and self.device.channels > 1:
                a = a.reshape(-1, self.device.channels).mean(axis=1)
            chunk = a.astype(np.float32) / 32768.0
            try:
                self.queue.put_nowait(chunk)
            except queue.Full:
                self.dropped_chunks += 1
        return (None, pyaudio.paContinue)

    def _guard_loop(self) -> None:
        """看门狗：stream 静默死亡（设备拔出/切换/微信独占）时自动重连。"""
        while self._running:
            time.sleep(1.0)
            if not self._running:
                break
            try:
                if self._stream is not None and not self._stream.is_active():
                    print("[audio] 检测到流已停止，尝试重连…", file=sys.stderr)
                    self._reconnect()
            except Exception as e:  # noqa: BLE001
                print(f"[audio] 看门狗异常，重连中: {e}", file=sys.stderr)
                self._reconnect()

    def _reconnect(self) -> None:
        try:
            if self._stream is not None:
                self._stream.close()
        except Exception:
            pass
        self._stream = None
        old_rate = self.device.rate
        for attempt in range(5):
            try:
                self.device = pick_device(self.device.name)
                if self.device.rate != old_rate and self._out_rate is not None:
                    # 换到了不同原生率的设备：重建重采样器（旧缓冲里的几十毫秒
                    # 不追补，重连本身已容忍丢帧）。
                    self._resampler = self._make_resampler()
                self._open_stream()
                print("[audio] 重连成功")
                return
            except Exception as e:  # noqa: BLE001
                print(f"[audio] 重连失败({attempt + 1}/5): {e}", file=sys.stderr)
                time.sleep(2)

    def stop(self, reason: str = "调用方请求") -> None:
        """停止采集。

        reason 用于事后归因：直播录制最常见的疑问是"为什么这里断了"——
        是用户点的、静音超时自动停的、还是采集异常。以前没有记录，
        只能对着 WAV 尾部的静音时长猜（2026-11-16/17 两段录音都遇到过）。
        现在每次停止都留下明确原因，写进日志与产物 meta。
        """
        if self._stopping:
            return
        self.stop_reason = reason
        self._stopping = True
        self._running = False
        try:
            if self._stream is not None:
                self._stream.stop_stream()
                self._stream.close()
        except Exception:
            pass
        if self._pa is not None:
            self._pa.terminate()
        # 注意：这里不能立刻 flush 重采样器 —— 队列里可能还有已采集、
        # 尚未被 read() 消费的原始块，先 flush 会让它们绕过后面的重采样。
        # 残余样本由 read() 遇到结束哨兵时统一 flush（见 read）。
        with self._lock:
            self._eof = True
        self.queue.put(None)  # 通知消费端结束
        print(f"[audio] 采集停止（原因：{reason}，丢弃块 {self.dropped_chunks}）")

    # ---------- 消费 ----------

    def read(self, timeout: float = 0.5) -> np.ndarray | None:
        """取一个音频块（已按 out_rate 重采样）。返回 None 表示采集已结束。"""
        with self._lock:
            if self._final:
                return self._final.pop(0)
            eof = self._eof
        try:
            # 结束后仍有排队未取的原始块：用非阻塞方式取完再返回 None，
            # 不能因为 _eof 就直接丢队列（stop 前回调已经放进去的音频要算数）。
            item = self.queue.get_nowait() if eof else self.queue.get(timeout=timeout)
        except queue.Empty:
            return None if eof else np.zeros(0, dtype=np.float32)

        with self._lock:
            if item is None:
                # 兜底 flush：stop() 已 flush 过的话这里返回空。
                tail = self._flush_resampler()
                if len(tail):
                    self._final.append(tail)
                self._eof = True
                return self._final.pop(0) if self._final else None
            return self._process(item)

    def __enter__(self):
        self.start()
        return self

    def __exit__(self, *exc):
        self.stop()
        return False
