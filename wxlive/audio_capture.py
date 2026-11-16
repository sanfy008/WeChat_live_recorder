"""WASAPI loopback 音频采集。

设计要点：
1. 以设备原生采样率采集，不做任何重采样 —— 避免质量损失，
   重采样交给 faster-whisper 内部的 ffmpeg/av 完成（质量更好）。
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
import pyaudiowpatch as pyaudio


@dataclass
class DeviceInfo:
    index: int
    name: str
    rate: int
    channels: int


def list_loopback_devices() -> list[DeviceInfo]:
    """列出所有可用的 WASAPI loopback 设备（即"能录到扬声器声音"的设备）。"""
    p = pyaudio.PyAudio()
    try:
        out: list[DeviceInfo] = []
        try:
            wasapi = p.get_host_api_info_by_type(pyaudio.paWASAPI)
        except OSError:
            return []
        for i in range(wasapi["deviceCount"]):
            idx = p.get_host_api_info_by_type(pyaudio.paWASAPI)["index"]
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

    p = pyaudio.PyAudio()
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
    ) -> None:
        self.device = device or pick_device(device_hint)
        self.chunk_seconds = chunk_seconds
        self.mono = mono
        self.queue: queue.Queue[np.ndarray | None] = queue.Queue(maxsize=max_queue_chunks)

        self._pa: pyaudio.PyAudio | None = None
        self._stream = None
        self._running = False
        self._thread: threading.Thread | None = None
        self._frames_per_buffer = int(self.device.rate * chunk_seconds)
        self.dropped_chunks = 0

    # ---------- 生命周期 ----------

    def start(self) -> None:
        if self._running:
            return
        self._pa = pyaudio.PyAudio()
        self._open_stream()
        self._running = True
        self._thread = threading.Thread(target=self._guard_loop, daemon=True, name="loopback-guard")
        self._thread.start()
        print(f"[audio] 开始采集: {self.device.name} @ {self.device.rate}Hz "
              f"{self.device.channels}ch  块={self.chunk_seconds}s")

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
        for attempt in range(5):
            try:
                self.device = pick_device(self.device.name)
                self._open_stream()
                print("[audio] 重连成功")
                return
            except Exception as e:  # noqa: BLE001
                print(f"[audio] 重连失败({attempt + 1}/5): {e}", file=sys.stderr)
                time.sleep(2)

    def stop(self) -> None:
        self._running = False
        try:
            if self._stream is not None:
                self._stream.stop_stream()
                self._stream.close()
        except Exception:
            pass
        if self._pa is not None:
            self._pa.terminate()
        self.queue.put(None)  # 通知消费端结束
        print(f"[audio] 采集停止（丢弃块 {self.dropped_chunks}）")

    # ---------- 消费 ----------

    def read(self, timeout: float = 0.5) -> np.ndarray | None:
        """取一个音频块。返回 None 表示采集已结束。"""
        try:
            return self.queue.get(timeout=timeout)
        except queue.Empty:
            return np.zeros(0, dtype=np.float32)

    def __enter__(self):
        self.start()
        return self

    def __exit__(self, *exc):
        self.stop()
        return False
