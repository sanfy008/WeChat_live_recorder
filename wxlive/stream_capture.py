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
import ipaddress
import re
import shutil
import socket
import subprocess
import sys
import threading
import time
import urllib.parse
from pathlib import Path

from . import winproc

AUDIO_EXTS = (".wav", ".m4a", ".mp3", ".aac", ".flac", ".ogg", ".opus")


def find_ffmpeg() -> str | None:
    """找 ffmpeg。

    优先级：环境变量 WXLIVE_FFMPEG → PATH → 常见安装位置。

    为什么要环境变量优先（实测 2026-11-23）：
      NAS 容器里 Debian 自带的 ffmpeg 7.1.5 解不了视频号直播流的视频轨
      （flv demuxer 报 "Video codec (c) is not implemented"，视频走完
      也不报致命错，但整条流解析不出来、录音产出 0 字节）。
      fnOS 自带的 /usr/trim/lib/mediasrv/ffmpeg 8.1.1-mediasrv 可以。
      所以镜像里那份旧版必须能被显式覆盖掉 —— 光靠 PATH 顺序不够可靠，
      因为镜像里的 /usr/bin/ffmpeg 在 PATH 里必然排前面。
    """
    env = os.environ.get("WXLIVE_FFMPEG", "").strip()
    if env and Path(env).exists():
        return env

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
    probe = os.environ.get("WXLIVE_FFPROBE", "").strip()
    if not (probe and Path(probe).exists()):
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
    #
    # ---- 为什么用 ffmpeg 探测而不是 ffprobe（真机实测 2026-11-23）----
    # NAS 用 Docker 部署，容器里的解析结果是 IPv6 优先，而视频号 CDN
    # （voipfinderliveplaythird.wxqcloud.qq.com）的 IPv6 **不通**。
    # ffprobe 不像 curl 会自动回退到 IPv4，它会一直等到超时。
    # 实测对比：
    #   宿主机 shell 跑 ffprobe 同一条命令   0 秒出结果
    #   容器内 /api/probe                    稳定 15 秒超时
    #   容器内换成 IPv4 直连                  0.13 秒返回
    #
    # 但 **ffprobe 没有 -4 选项**（实测 "Option not found"），
    # 只有 ffmpeg 有。所以探测改用 ffmpeg -4 -i <url> -t 0 -f null -，
    # 它同样只解析头部、不下载整个流，且能强制 IPv4。
    return _probe_with_ffmpeg(ff, url, timeout)


def _force_ipv4_url(url: str) -> str:
    """把 URL 里的主机名换成它的 IPv4 地址，返回新 URL（失败则原样返回）。

    为什么要这一步（真机实测 2026-11-23）：
      NAS 用 Docker 部署，容器里 `voipfinderliveplaythird.wxqcloud.qq.com`
      解析出 IPv6 优先，而该 CDN 的 IPv6 **不通**（实测 curl -6 -> HTTP 000）。
      ffmpeg / ffprobe 都**没有 -4 选项**（实测两边构建都没有），
      也不像 curl 会自动回退到 IPv4 —— 于是它一直在 IPv6 上等到超时。
      唯一可靠的办法是自己在 Python 层解析出 IPv4，再把地址塞给 ffmpeg。

    保留原始 Host 的方式不可行（ffmpeg 不支持自定义 Host 头），
    但视频号 CDN 是 IP 直连可用的（实测把主机名换成 14.116.174.61
    后 0.13 秒就返回），所以直接替换主机名是安全的。
    """
    try:
        parts = urllib.parse.urlsplit(url)
    except Exception:  # noqa: BLE001
        return url
    host = parts.hostname or ""
    if not host or _is_ip_literal(host):
        return url

    ip = ""
    try:
        infos = socket.getaddrinfo(host, parts.port or 80,
                                   socket.AF_INET, socket.SOCK_STREAM)
        for info in infos:
            cand = info[4][0]
            addr = ipaddress.ip_address(cand)
            # 只接受公网地址。必须排除：
            #   - 回环 / 私有 / 链路本地：本机 DNS 被抓流工具 MITM 时，
            #     视频号 CDN 域名会解析成 127.208.0.126 之类的假地址
            #     （实测踩过：拿它去拉流得到 403，排查了很久）
            #   - 任何非全局地址
            if addr.is_global and not addr.is_multicast:
                ip = cand
                break
    except Exception:  # noqa: BLE001
        return url
    if not ip:
        return url

    netloc = ip
    if parts.port:
        netloc = f"{ip}:{parts.port}"
    return urllib.parse.urlunsplit(
        (parts.scheme, netloc, parts.path, parts.query, parts.fragment))


def _is_ip_literal(host: str) -> bool:
    try:
        ipaddress.ip_address(host)
        return True
    except ValueError:
        return False


def _probe_with_ffmpeg(ff: str, url: str, timeout: int) -> dict:
    """用 ffmpeg 探测流，失败时自动换 IPv4 重试一次。

    判据不看 returncode：ffmpeg 把版本横幅也打到 stderr，且 rc 可能是 0，
    连不存在的地址都会"看起来成功"（实测踩过）。只有解析出
    「Input #0」才是真的连上了。
    """
    first = _probe_once(ff, url, timeout)
    if first.get("ok") or not first.get("timeout"):
        return first

    # 超时：很可能是 IPv6 黑洞。换成 IPv4 地址再试一次。
    v4 = _force_ipv4_url(url)
    if v4 == url:
        return first
    second = _probe_once(ff, v4, timeout)
    if second.get("ok"):
        second["via_ipv4"] = True
    return second


def _probe_once(ff: str, url: str, timeout: int) -> dict:
    """单次探测（不重试）。

    必须流式读 stderr：直播流是**无限长**的，用 run() 等它结束会挂到超时
    （实测踩过：一个验证脚本跑了 150 秒还没返回）。
    实测 `Input #0` 在 0.24 秒就出现了，所以看到它就立刻 kill 进程。
    """
    # -vn 丢掉视频：我们只要音频，而容器里的 ffmpeg 是 Debian 自带的
    # 5.1.9，解码不了视频号这条流的 hevc 变体（实测报
    # "Video codec (c) is not implemented"）。不加 -vn 会把这个
    # **与音频无关**的解码失败误判成"流不可用"。
    cmd = [ff, "-hide_banner", "-nostats", "-v", "info",
           "-vn",
           "-i", url,
           "-f", "null", "-"]

    proc = None
    lines: list[str] = []
    got_input = False
    deadline = time.time() + timeout
    try:
        proc = winproc.popen(cmd, stdout=subprocess.DEVNULL,
                             stderr=subprocess.PIPE,
                             text=True, encoding="utf-8", errors="replace")

        def _drain() -> None:
            nonlocal got_input
            try:
                for line in proc.stderr:            # type: ignore[union-attr]
                    t = line.strip()
                    if not t:
                        continue
                    lines.append(t)
                    if t.startswith("Input #0"):
                        got_input = True
                    # 拿到 Input + 至少一个 Stream 就够了，别再等
                    if got_input and any(x.startswith("Stream #") for x in lines):
                        return
                    if len(lines) > 200:
                        return
            except Exception:  # noqa: BLE001
                pass

        th = threading.Thread(target=_drain, daemon=True)
        th.start()
        th.join(timeout=max(1.0, timeout))
    except Exception as e:  # noqa: BLE001
        return {"ok": False, "error": str(e)[:200]}
    finally:
        if proc is not None and proc.poll() is None:
            try:
                proc.kill()
            except Exception:  # noqa: BLE001
                pass

    if not got_input:
        err = next((x for x in reversed(lines)
                    if any(k in x for k in ("Error", "Invalid", "No route",
                                            "refused", "timed out", "404",
                                            "403", "401", "Failed"))), "")
        if not err and lines:
            err = lines[-1]
        timed_out = (not lines) or time.time() >= deadline
        return {"ok": False, "timeout": bool(timed_out),
                "error": (err or "探测失败（未解析出输入流）")[:200]}

    has_audio = any("Audio:" in x for x in lines)
    has_video = any("Video:" in x for x in lines)
    if not has_audio and not has_video:
        return {"ok": False, "error": "连上了但没有任何音视频流"}
    # 只要流里有音频就够用（我们录的是音轨）。视频编码解不了不影响结论，
    # 但要如实告诉用户，免得以为是音频有问题。
    warn = ""
    if not has_audio and has_video:
        warn = "（只有视频轨，没有音频）"

    brief = " | ".join(x for x in lines if x.startswith(("Input #0", "Stream #0")))
    return {"ok": True, "info": brief[:300],
            "has_audio": has_audio, "has_video": has_video}


def _probe_url_ffprobe_legacy(probe: str, url: str, timeout: int) -> dict:
    """保留 ffprobe 版本作为参考（当前不在主路径上）。

    留着是因为某些环境 ffmpeg 的 -t 0 行为可能不同，
    排查时可以一键切回来对照。
    """
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
        # 超时有两种截然不同的原因，过去一律说"地址可能已过期"，会误导排查
        # 方向（实测踩过：其实是 NAS 容器 IPv6 不通，与地址无关）。
        return {"ok": False,
                "error": (f"探测超时（{timeout}s）。两种可能：\n"
                          f"· 主播已下播，或地址签名过期\n"
                          f"· NAS 到该 CDN 的网络不通（IPv6 黑洞 / 出口被限）"),
                "timeout": True}
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
        # 实际交给 ffmpeg 的地址（可能是换成 IPv4 后的版本）
        self._effective_url = self.url

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

        # 注意：ffmpeg 没有 -4 选项（实测两边的构建都不认），
        # IPv4 的处理在 _build_cmd 末尾把 url 换掉，见下方 self._effective_url。
        cmd = [ff, "-hide_banner", "-loglevel", "warning", "-stats"]

        # 断流自动重连：这是直播拉流必须的，服务端关连接是常态
        if self.url.lower().startswith(("http://", "https://")):
            cmd += [
                "-reconnect", "1",
                "-reconnect_streamed", "1",
                "-reconnect_delay_max", "10",
                "-rw_timeout", "30000000",  # 30s 无数据视为断流
            ]

        # 注意：这里**不能**把主机名换成 IP。
        # 视频号 CDN 会对签名 URL 校验 Host 头，换了主机名会直接 403
        # （实测 2026-11-23：换成 127.208.0.126 后 ffmpeg 报
        #   "HTTP error 403 Forbidden"）。而且本机的 DNS 被抓流工具
        # MITM 劫持，解析出来的是假地址。
        # IPv6 不通的问题用 WXLIVE_FFMPEG 指定宿主机 ffmpeg 解决，
        # 不需要动 URL。
        self._effective_url = self.url
        cmd += ["-i", self._effective_url]

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
        self._stderr_thread = threading.Thread(target=self._read_stderr, daemon=True)
        self._stderr_thread.start()
        self._thread = threading.Thread(target=self._monitor, daemon=True)
        self._thread.start()

    def _read_stderr(self) -> None:
        """独立线程消费 ffmpeg stderr，防止 readline() 阻塞 _monitor() 看门狗。"""
        proc = self._proc
        if proc is None or proc.stderr is None:
            return
        try:
            for raw in iter(proc.stderr.readline, b""):
                text = raw.decode("utf-8", errors="replace").strip()
                if text and not text.startswith(("size=", "frame=", "  ")):
                    self.errors.append(text)
                    if len(self.errors) > 50:
                        self.errors.pop(0)
                    if any(k in text.lower() for k in
                           ("error", "failed", "403", "404", "invalid", "timed out")):
                        self._log(f"[stream] ffmpeg: {text[:160]}")
        except Exception:
            pass

    def _monitor(self) -> None:
        """监测文件增长、判断是否还在录（不再被 stderr readline 阻塞）。"""
        assert self._proc is not None
        last_size = 0
        last_grow = time.time()
        stale_restarts = 0

        while not self._stop.is_set():
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
                        # 必须真的杀掉进程：只 break 会留下"进程活着但不再写数据"的僵尸，
                        # `running` 仍为 True，上层 watcher 会永远等下去（对立性审查 H1）。
                        self._kill_proc()
                        break
                    last_grow = time.time()

            if self.progress_cb and self.out_path and self.out_path.exists():
                try:
                    self.progress_cb(size)
                except Exception:
                    pass

            if self._proc.poll() is not None and size == last_size:
                break

    def _kill_proc(self) -> None:
        """终止 ffmpeg 进程（幂等）。

        为什么单独抽出来：`_monitor` 判定断流后如果只 `break`，
        进程还活着但不再写数据 —— `running` 属性（看 `poll() is None`）
        仍然是 True，上层 watcher 会永远循环等它结束。
        """
        proc = self._proc
        if proc is None:
            return
        if proc.poll() is not None:
            return
        try:
            proc.terminate()
            proc.wait(timeout=8)
        except Exception:
            try:
                proc.kill()
            except Exception:
                pass

    def stop(self) -> None:
        self._stop.set()
        self._kill_proc()
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


_FFMPEG_CMD_RE = re.compile(
    r"""(?:^|\s)-i\s+(?:"(?P<dq>[^"]+)"|'(?P<sq>[^']+)'|(?P<plain>[^\s"']+))""",
    re.IGNORECASE,
)


def extract_url(text: str) -> str:
    """从用户粘贴的内容里抠出可拉的流地址。

    为什么要这个：微信里点下载按钮给出的是一整条 ffmpeg 命令，
    第三方工具（yt-dlp / IDM / 手机端抓包 App）给的也常带附加参数。
    直接把它整段贴进输入框是最自然的动作，我们不该让人先手工裁剪。

    支持三种形态：
      1. 纯 URL                       https://pull-m1.wxlivecdn.com/...flv?txTime=..
      2. 完整 ffmpeg 命令              ffmpeg -i "https://..." -c copy -y out.flv
      3. 带转义引号的命令              ffmpeg -i \\"https://...\\" -c copy
    """
    raw = (text or "").strip()
    if not raw:
        return ""

    # 形态 1：本身就是 URL（先剥掉复制时常带的包裹引号/反斜杠）
    candidate = raw.strip().strip("\"'")
    if is_url(candidate):
        return candidate

    normalized = raw.replace('\\"', '"').replace("\\'", "'")

    # 形态 2/3：从 -i 后面取参数（带引号优先，避免 URL 里的 # & 被截断）
    m = _FFMPEG_CMD_RE.search(normalized)
    if m:
        url = (m.group("dq") or m.group("sq") or m.group("plain") or "").strip()
        if is_url(url):
            return url

    # 兜底：正文里出现的第一条 http(s)/rtmp 地址
    m = re.search(r"(?:https?|rtmp|rtsp)://[^\s\"'<>]+", normalized, re.IGNORECASE)
    if m:
        return m.group(0).strip().strip("\"'")
    return ""
