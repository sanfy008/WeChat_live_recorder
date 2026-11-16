"""从视频号下载/转写：API 取址 → 解密 → 抽音频。

依赖本地 wx_video_download（tools/wxcd）提供的 API：
    GET  /api/content/list                       列出已捕获的内容
    GET  /api/content/detail?id=wxchannels:<id>  取解密密钥 metadata.key

为什么不再让工具自己下：
    工具下载完会自动把 400MB 视频转成 MP3（ffmpeg -vn libmp3lame），
    对"只要音频来转写"的场景纯属浪费：转码几分钟 + 额外占空间。
    这里只拉一次流、本地解密、直接出 16k 单声道 WAV。
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import time
import urllib.parse
import urllib.request
from dataclasses import dataclass
from pathlib import Path

from .sphdecrypt import open_decrypted, probe_encryption

DEFAULT_API = "http://127.0.0.1:2022"


class FetchError(RuntimeError):
    """取址/下载失败。"""


@dataclass
class ContentItem:
    """一条已捕获的视频号内容。"""

    content_id: str          # 纯数字 id
    title: str
    url: str                 # 带签名的直链
    source_url: str = ""
    cover_url: str = ""

    @property
    def api_id(self) -> str:
        return f"wxchannels:{self.content_id}"


def _api_get(base: str, path: str, timeout: int = 20) -> dict:
    url = base.rstrip("/") + path
    # 本地 API，绝不能走系统代理
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    try:
        with opener.open(url, timeout=timeout) as r:
            return json.loads(r.read().decode("utf-8"))
    except Exception as exc:  # noqa: BLE001
        raise FetchError(
            f"无法访问 {path}。抓流工具是否在运行？"
            f"（应在 tools/wxcd 下用根命令启动）原始错误：{exc}"
        ) from exc


def list_contents(api: str = DEFAULT_API) -> list[ContentItem]:
    """列出工具已捕获的内容（新→旧）。"""
    data = _api_get(api, "/api/content/list")
    if data.get("code") not in (0, None):
        raise FetchError(f"接口返回错误：{data.get('msg')}")
    items = []
    for it in (data.get("data") or {}).get("list") or []:
        cid = str(it.get("external_id") or "").strip()
        url = str(it.get("url") or "").strip()
        if not cid or not url:
            continue
        items.append(ContentItem(
            content_id=cid,
            title=str(it.get("title") or "").strip() or cid,
            url=url,
            source_url=str(it.get("source_url") or ""),
            cover_url=str(it.get("cover_url") or ""),
        ))
    return items


def find_content(keyword: str, api: str = DEFAULT_API) -> ContentItem:
    """按标题关键词或 id 找内容。命中多条时取第一条。"""
    kw = keyword.strip()
    if not kw:
        raise FetchError("关键词不能为空")
    items = list_contents(api)
    for it in items:
        if it.content_id == kw or it.api_id == kw:
            return it
    hits = [it for it in items if kw in it.title]
    if not hits:
        titles = "、".join(f"《{it.title}》" for it in items[:5]) or "（空）"
        raise FetchError(f"没找到包含「{kw}」的内容。已捕获：{titles}")
    return hits[0]


def get_decrypt_key(item: ContentItem, api: str = DEFAULT_API) -> int:
    """取解密密钥。视频号把 key 放在 content.metadata 里（十进制字符串）。"""
    data = _api_get(api, "/api/content/detail?id=" +
                    urllib.parse.quote(item.api_id))
    if data.get("code") not in (0, None):
        raise FetchError(f"取详情失败：{data.get('msg')}")
    content = (data.get("data") or {}).get("content") or {}
    raw = content.get("metadata") or ""
    if not raw:
        raise FetchError("详情里没有 metadata，拿不到解密密钥")
    try:
        meta = json.loads(raw) if isinstance(raw, str) else raw
        return int(str(meta.get("key") or "0"))
    except (ValueError, TypeError) as exc:
        raise FetchError(f"解密密钥解析失败：{raw!r}") from exc


def _find_ffmpeg() -> str:
    exe = shutil.which("ffmpeg")
    if not exe:
        raise FetchError("找不到 ffmpeg，请先安装")
    return exe


def safe_filename(name: str) -> str:
    """去掉 Windows 文件名非法字符。"""
    bad = '<>:"/\\|?*'
    out = "".join("_" if c in bad else c for c in name)
    return out.strip().strip(".")[:120] or "untitled"


def download_decrypted(item: ContentItem, key: int,
                       dest: Path | None = None,
                       on_progress=None) -> Path:
    """下载并解密整段媒体，返回落地路径。

    必须先完整落盘再给 ffmpeg：视频号的 MP4 是分片结构，
    `moov` 在文件头之后，用管道喂 ffmpeg 时无法 seek，
    会报 "moov atom not found"。
    """
    flag, enc_len = probe_encryption(item.url)
    if flag and not key:
        raise FetchError("文件已加密但未拿到密钥，无法解密")

    if dest is None:
        from .config import RECORDINGS_DIR
        dest = Path(RECORDINGS_DIR) / (safe_filename(item.title) + ".mp4")
    dest = Path(dest)
    dest.parent.mkdir(parents=True, exist_ok=True)

    part = dest.with_suffix(dest.suffix + ".part")
    stream = open_decrypted(item.url, enc_len, key if flag else 0)
    total = 0
    started = time.time()
    try:
        with part.open("wb") as f:
            while True:
                chunk = stream.read(1 << 20)
                if not chunk:
                    break
                f.write(chunk)
                total += len(chunk)
                if on_progress:
                    on_progress(total, time.time() - started)
    finally:
        stream.close()

    if total == 0:
        part.unlink(missing_ok=True)
        raise FetchError("下载到 0 字节，地址可能已过期")
    os.replace(part, dest)
    return dest


def extract_audio(video: Path, wav: Path | None = None,
                  sample_rate: int = 16000, on_progress=None) -> Path:
    """从视频中抽出 16k 单声道 WAV（转写引擎的输入格式）。"""
    video = Path(video)
    if wav is None:
        from .config import RECORDINGS_DIR
        wav = Path(RECORDINGS_DIR) / (video.stem + ".wav")
    wav = Path(wav)
    wav.parent.mkdir(parents=True, exist_ok=True)

    cmd = [
        _find_ffmpeg(), "-hide_banner", "-loglevel", "error", "-y",
        "-i", str(video),
        "-vn", "-ac", "1", "-ar", str(sample_rate),
        "-c:a", "pcm_s16le", str(wav),
    ]
    r = subprocess.run(cmd, capture_output=True, text=True,
                       encoding="utf-8", errors="replace")
    if r.returncode != 0 or not wav.exists() or wav.stat().st_size == 0:
        tail = (r.stderr or "").strip()[-400:]
        raise FetchError(f"抽音频失败（rc={r.returncode}）：{tail}")
    return wav


def probe_media(path: Path) -> tuple[float, bool]:
    """返回 (时长秒, 是否有音轨)。"""
    exe = shutil.which("ffprobe")
    if not exe:
        return 0.0, False
    cmd = [exe, "-v", "error", "-show_entries", "format=duration",
           "-show_entries", "stream=codec_type",
           "-of", "default=noprint_wrappers=1", str(path)]
    r = subprocess.run(cmd, capture_output=True, text=True,
                       encoding="utf-8", errors="replace")
    dur = 0.0
    has_audio = False
    for line in (r.stdout or "").splitlines():
        if line.startswith("duration="):
            try:
                dur = float(line.split("=", 1)[1])
            except ValueError:
                pass
        elif line.strip() == "codec_type=audio":
            has_audio = True
    return dur, has_audio
