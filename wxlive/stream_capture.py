"""流地址直录（URL → 音频）。

相比声卡回环的优势：
1. 拿到的是**原始音轨**，不经过声卡重编码，ASR 准确率更高
2. CPU/内存占用极低，可无人值守录几小时
3. 不受"静音/独占音频/其他程序抢音"影响
4. 不依赖播放器，可以静音播放甚至不开播放器

代价与风险（必须知道）：
- **需要先拿到流地址**。微信视频号不公开，必须靠 mitmproxy 抓包拿带签名的 FLV/HLS 地址
- 地址带过期时间（txTime），过期就 403，需要重新获取
- 服务端会主动断流（切片/负载均衡），必须自动重连

实测要点（来自 gui8912/wxchannels-downloader 的踩坑记录）：
- 重连时服务端会重发 13 字节 FLV 文件头，必须丢弃，否则时间戳错乱
- ffmpeg 的 -c copy 直接转封装最稳，不要转码
"""

from __future__ import annotations

import os
import re
import shutil
import subprocess
import sys
import threading
import time
from pathlib import Path

from . import winproc

AUDIO_EXTS = (".wav", ".m4a", ".mp3", ".aac", ".flac", ".ogg", ".opus")


def find_ffmpeg() -> str | None:
    """找 ffmpeg。优先 PATH，其次常见安装位置。"""
    p = shutil.which("ffmpeg")
    if p:
        return p
    for cand in (
        r"C:\ffmpeg\bin\ffmpeg.exe",
        r"C:\Program Files\ffmpeg\bin\ffmpeg.exe",
        os.path.expandvars(r"%LOCALAPPDATA%\Microsoft\WinGet\Links\ffmpeg.exe"),
    ):
        if Path(cand).exists():
            return cand
    return None


def probe_url(url: str, timeout: int = 20) -> dict:
    """探测流地址是否可拉，返回 {ok, info, error, duration, has_audio}。

    用 ffprobe 而不是直接下载，避免拉整个流。
    """
    ff = find_ffmpeg()
    if not ff:
        return {"ok": False, "error": "未找到 ffmpeg，请先安装并加入 PATH"}

    # 注意：chocolatey 装的 ffmpeg 是大写 .EXE，
    # 直接 replace("ffmpeg.exe") 匹配不到，会把 ffmpeg 当 ffprobe 调用而报
    # "Option not found"。所以用 Path 的父目录 + shutil.which 双保险。
    probe = shutil.which("ffprobe") or ""
    if not probe:
        for ext in (".exe", ".EXE", ""):
            cand = Path(ff).with_name("ffprobe" + ext)
            if cand.exists():
                probe = str(cand)
                break
    if not probe:
        return {"ok": False, "error": "未找到 ffprobe（ffmpeg 同目录下应有）"}

    # 注意：-show_entries 里 stream 和 format 用冒号分隔是正确的，
    # 但 ffprobe 不接受 "format=...,format_name" 这种重复前缀写法，
    # 必须分组写：format=duration,format_name:stream=codec_type,codec_name
    cmd = [probe, "-v", "error",
           "-show_entries", "format=duration,format_name:stream=codec_type,codec_name",
           "-of", "default=noprint_wrappers=1",
           "-i", url]
    try:
        r = winproc.run(cmd, capture_output=True, text=True,
                        timeout=timeout, encoding="utf-8", errors="replace")
        if r.returncode != 0:
            err = (r.stderr or "").strip().splitlines()
            return {"ok": False, "error": (err[-1] if err else "探测失败")[:200]}
        out = r.stdout or ""
        has_audio = "codec_type=audio" in out
        fmt = ""
        m = re.search(r"format_name=(\S+)", out)
        if m:
            fmt = m.group(1)
        return {"ok": True, "info": out.strip()[:300], "format": fmt,
                "has_audio": has_audio}
    except subprocess.TimeoutExpired:
        return {"ok": False, "error": f"探测超时（{timeout}s），地址可能无效或网络不通"}
    except Exception as e:  # noqa: BLE001
        return {"ok": False, "error": str(e)[:200]}


class StreamRecorder:
    """用 ffmpeg 把流里的音轨直接录成 16kHz 单声道 WAV。

    为什么直接输出 16k mono：
    - ASR 只需要 16k，省一半以上磁盘和后续重采样
    - -vn 丢弃视频轨，CPU 几乎为零
    - 不做转码以外的任何后处理，保持音质

    断流重连：
    直播流服务端会主动关闭连接（切片、负载均衡），这是常态不是故障。
    所以用 ffmpeg 的 -reconnect 系列参数让它自己扛，
    同时外层再包一层看门狗，长时间无输出就整进程重启并追加录制。
    """

    def __init__(
        self,
        url: str,
        out_dir: Path,
        name: str = "",
        progress_cb=None,
        log_cb=None,
    ) -> None:
        self.url = url
        self.out_dir = Path(out_dir)
        self.name = name or "stream"
        self.progress_cb = progress_cb
        self.log_cb = log_cb

        self._proc: subprocess.Popen | None = None
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self.out_path: Path | None = None
        self.errors: list[str] = []

    def _log(self, msg: str) -> None:
        if self.log_cb:
            try:
                self.log_cb(msg)
            except Exception:
                pass

    def _build_cmd(self, out: Path) -> list[str]:
        ff = find_ffmpeg()
        if not ff:
            raise RuntimeError("未找到 ffmpeg")

        cmd = [ff, "-hide_banner", "-loglevel", "warning", "-stats"]

        # 断流自动重连：这是直播拉流必须的，服务端关连接是常态
        if self.url.lower().startswith(("http://", "https://")):
            cmd += [
                "-reconnect", "1",
                "-reconnect_streamed", "1",
                "-reconnect_delay_max", "10",
                "-rw_timeout", "30000000",  # 30s 无数据视为断流
            ]

        cmd += ["-i", self.url]

        # 只保留音轨，直接出 16k 单声道 PCM，ASR 拿到就能用
        cmd += [
            "-vn",
            "-ac", "1",
            "-ar", "16000",
            "-c:a", "pcm_s16le",
            "-y", str(out),
        ]
        return cmd

    def start(self) -> None:
        self.out_dir.mkdir(parents=True, exist_ok=True)
        stamp = time.strftime("%Y%m%d_%H%M%S")
        safe = "".join(c for c in self.name if c.isalnum() or c in "-_") or "stream"
        self.out_path = self.out_dir / f"{stamp}_{safe}_stream.wav"

        cmd = self._build_cmd(self.out_path)
        self._log(f"[stream] 启动 ffmpeg 拉流 → {self.out_path.name}")
        self._log(f"[stream] 命令: {' '.join(cmd[:6])} … -i {self.url[:60]}…")

        # ffmpeg 是 console 程序，GUI（pythonw）下不隐藏就会闪黑框。
        # 统一走 winproc，别在这里手写 creationflags —— 漏一处就闪一次。
        self._proc = winproc.popen(
            cmd, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE,
        )
        self._thread = threading.Thread(target=self._monitor, daemon=True)
        self._thread.start()

    def _monitor(self) -> None:
        """读 stderr 抓错误、监测文件增长、判断是否还在录。"""
        assert self._proc is not None
        last_size = 0
        last_grow = time.time()
        stale_restarts = 0

        while not self._stop.is_set():
            # 读一行 stderr（非阻塞式，靠 Popen 的缓冲）
            line = b""
            try:
                if self._proc.stderr is not None:
                    line = self._proc.stderr.readline()
            except Exception:
                pass
            if line:
                text = line.decode("utf-8", errors="replace").strip()
                if text and not text.startswith(("size=", "frame=", "  ")):
                    self.errors.append(text)
                    if len(self.errors) > 50:
                        self.errors.pop(0)
                    # 只把关键错误抛给 UI，避免刷屏
                    if any(k in text.lower() for k in
                           ("error", "failed", "403", "404", "invalid", "timed out")):
                        self._log(f"[stream] ffmpeg: {text[:160]}")

            # 检查文件是否在增长
            time.sleep(1.0)
            size = 0
            if self.out_path and self.out_path.exists():
                try:
                    size = self.out_path.stat().st_size
                except Exception:
                    size = 0

            if size > last_size:
                last_size = size
                last_grow = time.time()
                stale_restarts = 0
            else:
                stale = time.time() - last_grow
                # 90 秒没有任何新数据 → 判定断流
                if stale > 90 and not self._stop.is_set():
                    rc = self._proc.poll()
                    if rc is not None:
                        self._log(f"[stream] ffmpeg 已退出（码 {rc}），录制结束")
                        break
                    stale_restarts += 1
                    self._log(f"[stream] {stale:.0f}s 无新数据，判定断流（第 {stale_restarts} 次）")
                    if stale_restarts >= 3:
                        self._log("[stream] 连续断流，停止。地址可能已过期，需重新获取。")
                        break
                    last_grow = time.time()

            if self.progress_cb and self.out_path and self.out_path.exists():
                try:
                    self.progress_cb(size)
                except Exception:
                    pass

            if self._proc.poll() is not None and size == last_size:
                break

    def stop(self) -> None:
        self._stop.set()
        if self._proc and self._proc.poll() is None:
            try:
                self._proc.terminate()
                self._proc.wait(timeout=8)
            except Exception:
                try:
                    self._proc.kill()
                except Exception:
                    pass
        self._log("[stream] 已停止")

    @property
    def running(self) -> bool:
        return self._proc is not None and self._proc.poll() is None

    def status(self) -> str:
        if not self.out_path or not self.out_path.exists():
            return "未开始"
        size = self.out_path.stat().st_size
        # 16k mono 16bit = 32000 B/s
        secs = max(0, (size - 44)) / 32000
        m, s = divmod(int(secs), 60)
        return f"{m:02d}:{s:02d} / {size/1024/1024:.1f} MB"


def is_url(text: str) -> bool:
    t = (text or "").strip().lower()
    return t.startswith(("http://", "https://", "rtmp://", "rtsp://"))
