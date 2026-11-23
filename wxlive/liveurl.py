"""从本机运行中的 ffmpeg 进程里抠出视频号直播流地址。

## 为什么需要这个模块

微信视频号直播的流地址**不落任何数据库**。实测抓流工具（wxcd v261106）虽然会在
`download_task` 表里建一条直播任务，但 `source_url` 字段是**空的** ——
真实的带签名 FLV 地址只存在于它自己拉起的 ffmpeg 进程命令行里：

    ffmpeg ... -i http://pull-m1.wxlivecdn.com/trtc_xxx/orig_xxx.flv?txSecret=..&txTime=.. ...

所以「点一次下载按钮 → 自动把地址交给 NAS」这个体验，唯一可靠的取址点就是
**扫描 ffmpeg 进程的 command line**。这比让人去找那个藏起来的下拉箭头靠谱得多。

## 跨平台

- Windows：`Get-CimInstance Win32_Process -Filter "Name='ffmpeg.exe'"`（PowerShell）
- Linux/macOS：读 `/proc/<pid>/cmdline`（Linux）或 `ps -eo pid,command`（macOS）

两边都只做**只读**查询，不启动/不杀死任何进程。

## 时间戳与去重

`txSecret` / `txTime` 每次重连都会变，所以**不能按完整 URL 去重**。
用去掉 query string 的部分当指纹（稳定不变的「流路径」）：

    http://pull-m1.wxlivecdn.com/trtc_1400419933/orig_2042980977478071617_orig.flv
    └─────────────────────────── stream_key（稳定）────────────────────────────┘
"""

from __future__ import annotations

import hashlib
import sys
from dataclasses import dataclass, field
from pathlib import Path

from . import winproc
from .stream_capture import is_url

# 只认这些域名的流，避免把别的 ffmpeg 任务误当成视频号直播
_LIVE_HOST_HINTS = (
    "wxlivecdn.com",
    "finder.video.qq.com",
    "liveplay.myqcloud.com",
    "pull-m",
)

# 微信视频号直播地址的路径特征：/trtc_<数字>/orig_<id>_orig.flv
_LIVE_PATH_HINTS = ("/trtc_", "orig_", "_orig.flv")


@dataclass
class LiveStream:
    """一条正在进行的直播流。"""

    url: str
    pid: int = 0
    source: str = ""          # ffmpeg / unknown
    extra: dict = field(default_factory=dict)

    @property
    def stream_key(self) -> str:
        """去掉 query string 的流路径。同一条直播在重连后该值不变。"""
        return self.url.split("?", 1)[0]

    @property
    def stream_id(self) -> str:
        return hashlib.sha1(self.stream_key.encode("utf-8")).hexdigest()[:12]

    @property
    def title_hint(self) -> str:
        """从 URL 路径里猜一个名字，给「场次名称」做默认值。"""
        tail = self.stream_key.rsplit("/", 1)[-1]
        for suffix in (".flv", ".m3u8", ".ts"):
            if tail.lower().endswith(suffix):
                tail = tail[: -len(suffix)]
        return tail or "live"

    @property
    def has_signature(self) -> bool:
        return "txSecret=" in self.url or "txTime=" in self.url


def looks_like_live_url(url: str) -> bool:
    """这条地址是不是视频号直播流（而不是普通视频/音频文件）。"""
    if not is_url(url):
        return False
    low = url.lower()
    host_hit = any(h in low for h in _LIVE_HOST_HINTS)
    path_hit = any(p in low for p in _LIVE_PATH_HINTS)
    # 域名命中或路径命中，且不是明显的静态媒体文件
    if host_hit or path_hit:
        return True
    return False


def extract_urls_from_cmdline(cmdline: str) -> list[str]:
    """从一条 ffmpeg 命令行里取出所有候选 URL（按出现顺序、去重）。

    纯字符串处理，不碰进程 —— 这样可以用测试样例覆盖各种真实命令行形态，
    包括 `-i "url"` / `-i url` / 带 `\\"` 转义 / 多个 `-i` 的情形。
    """
    text = (cmdline or "").replace('\\"', '"').replace("\\'", "'")
    urls: list[str] = []

    i = 0
    n = len(text)
    while i < n:
        j = text.find("-i ", i)
        if j < 0:
            break
        k = j + 3
        while k < n and text[k] == " ":
            k += 1
        if k >= n:
            break
        if text[k] in "\"'":
            quote = text[k]
            end = text.find(quote, k + 1)
            token = text[k + 1: end] if end > 0 else text[k + 1:]
            i = (end + 1) if end > 0 else n
        else:
            end = k
            while end < n and not text[end].isspace():
                end += 1
            token = text[k:end]
            i = end
        token = token.strip()
        if is_url(token) and token not in urls:
            urls.append(token)
    return urls


def live_streams_from_cmdlines(
    cmdlines: list[tuple[int, str]]
) -> list[LiveStream]:
    """从 [(pid, cmdline), ...] 里筛出视频号直播流（按 stream_key 去重）。

    同一场直播可能被拉起多个 ffmpeg（录制 + HLS 预览），
    保留最先出现的那条即可。
    """
    seen: set[str] = set()
    out: list[LiveStream] = []
    for pid, cmd in cmdlines:
        for url in extract_urls_from_cmdline(cmd):
            if not looks_like_live_url(url):
                continue
            stream = LiveStream(url=url, pid=pid, source="ffmpeg")
            if stream.stream_key in seen:
                continue
            seen.add(stream.stream_key)
            out.append(stream)
    return out


# ---------------------------------------------------------------- 进程枚举


def _cmdlines_windows() -> list[tuple[int, str]]:
    ps = (
        "Get-CimInstance Win32_Process -Filter \"Name='ffmpeg.exe'\" | "
        "ForEach-Object { \"$($_.ProcessId)`t$($_.CommandLine)\" }"
    )
    # 必须走 winproc：GUI 由 pythonw 启动时没有控制台，
    # 直接 subprocess 会让 Windows 给 powershell 新分配一个黑框闪一下。
    # （项目有 test_no_console_flash 专门盯着这条）
    r = winproc.run(
        ["powershell", "-NoProfile", "-Command", ps],
        capture_output=True, text=True, encoding="utf-8", errors="replace",
        timeout=20,
    )
    out: list[tuple[int, str]] = []
    for line in (r.stdout or "").splitlines():
        if "\t" not in line:
            continue
        pid_s, cmd = line.split("\t", 1)
        try:
            out.append((int(pid_s.strip()), cmd))
        except ValueError:
            continue
    return out


def _cmdlines_linux() -> list[tuple[int, str]]:
    out: list[tuple[int, str]] = []
    proc = Path("/proc")
    if not proc.is_dir():
        return out
    for d in proc.iterdir():
        if not d.name.isdigit():
            continue
        try:
            raw = (d / "cmdline").read_bytes()
        except (OSError, PermissionError):
            continue
        if b"ffmpeg" not in raw:
            continue
        parts = [p for p in raw.split(b"\x00") if p]
        if not parts:
            continue
        cmd = " ".join(p.decode("utf-8", "replace") for p in parts)
        out.append((int(d.name), cmd))
    return out


def _cmdlines_macos() -> list[tuple[int, str]]:
    r = winproc.run(
        ["ps", "-eo", "pid=,command="],
        capture_output=True, text=True, encoding="utf-8", errors="replace",
        timeout=20,
    )
    out: list[tuple[int, str]] = []
    for line in (r.stdout or "").splitlines():
        line = line.strip()
        if not line or "ffmpeg" not in line:
            continue
        pid_s, _, cmd = line.partition(" ")
        try:
            out.append((int(pid_s), cmd.strip()))
        except ValueError:
            continue
    return out


def running_cmdlines() -> list[tuple[int, str]]:
    """列出本机所有 ffmpeg 进程的 (pid, commandline)。失败时返回空列表。"""
    try:
        if sys.platform == "win32":
            return _cmdlines_windows()
        if sys.platform == "darwin":
            return _cmdlines_macos()
        return _cmdlines_linux()
    except Exception:  # noqa: BLE001
        return []


def find_live_streams() -> list[LiveStream]:
    """本机正在录制的视频号直播流（按 stream_key 去重）。"""
    return live_streams_from_cmdlines(running_cmdlines())
