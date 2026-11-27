"""\u4ECE\u89C6\u9891\u53F7\u4E0B\u8F7D/\u8F6C\u5199\uFF1AAPI \u53D6\u5740 \u2192 \u89E3\u5BC6 \u2192 \u62BD\u97F3\u9891\u3002

\u4F9D\u8D56\u672C\u5730 wx_video_download\uFF08tools/wxcd\uFF09\u63D0\u4F9B\u7684 API\uFF1A
    GET  /api/content/list    \u5217\u51FA\u5DF2\u6355\u83B7\u7684\u5185\u5BB9\uFF08\u652F\u6301 keyword/limit/offset/page\uFF09
    GET  /api/content/detail  \u53D6\u89E3\u5BC6\u5BC6\u94A5 metadata.key

\u4E3A\u4EC0\u4E48\u4E0D\u518D\u8BA9\u5DE5\u5177\u81EA\u5DF1\u4E0B\uFF1A
    \u5DE5\u5177\u4E0B\u8F7D\u5B8C\u4F1A\u81EA\u52A8\u628A 400MB \u89C6\u9891\u8F6C\u6210 MP3\uFF08ffmpeg -vn libmp3lame\uFF09\uFF0C
    \u5BF9"\u53EA\u8981\u97F3\u9891\u6765\u8F6C\u5199"\u7684\u573A\u666F\u7EAF\u5C5E\u6D6A\u8D39\uFF1A\u8F6C\u7801\u51E0\u5206\u949F + \u989D\u5916\u5360\u7A7A\u95F4\u3002
    \u8FD9\u91CC\u53EA\u62C9\u4E00\u6B21\u6D41\u3001\u672C\u5730\u89E3\u5BC6\u3001\u76F4\u63A5\u51FA 16k \u5355\u58F0\u9053 WAV\u3002

**\u8FD9\u4E2A\u5DE5\u5177\u6CA1\u6709\u5220\u9664\u5185\u5BB9\u7684 API**\uFF08\u6838\u5BF9\u8FC7 ltaoo/wx_channels_download \u7684
internal/api/routes.go\uFF0Ccontent \u53EA\u6709 list / detail / relations\uFF09\u3002
\u6240\u4EE5"\u5220\u9664/\u6E05\u7A7A\u5217\u8868"\u53EA\u80FD\u5728\u6211\u4EEC\u8FD9\u4FA7\u8BB0\u8D26\uFF08\u89C1 localstore.py\uFF09\uFF0C
\u5DE5\u5177\u81EA\u5DF1\u7684\u5E93\u4E0D\u53D7\u5F71\u54CD\u3002
"""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import sys
import time
import urllib.parse
import urllib.request
from dataclasses import dataclass
from pathlib import Path

from . import winproc
from .sphdecrypt import open_decrypted, probe_encryption

DEFAULT_API = "http://127.0.0.1:2022"


class FetchError(RuntimeError):
    """\u53D6\u5740/\u4E0B\u8F7D\u5931\u8D25\u3002"""


def _fmt_size(n: int) -> str:
    if n < 0:
        return "\u672A\u77E5"
    v = float(n)
    for unit in ("B", "KB", "MB", "GB"):
        if v < 1024 or unit == "GB":
            return f"{v:.0f} {unit}" if unit == "B" else f"{v:.1f} {unit}"
        v /= 1024.0
    return f"{v:.1f} GB"


def _fmt_ts(ts: int, ms: bool = False) -> str:
    """\u65F6\u95F4\u6233 \u2192 \u672C\u5730\u53EF\u8BFB\u65F6\u95F4\u30020 \u6216\u975E\u6CD5\u503C\u8FD4\u56DE\u7A7A\u4E32\u3002"""
    if not ts:
        return ""
    try:
        v = float(ts) / (1000.0 if ms else 1.0)
        if v <= 0:
            return ""
        return time.strftime("%m-%d %H:%M", time.localtime(v))
    except (ValueError, OSError, OverflowError):
        return ""


@dataclass
class ContentItem:
    """一条已捕获的视频号内容。

    is_live 为什么存在：直播项在工具库里 **url 恒为空**（视频号直播没有
    静态直链，真实地址只在 detail.resources[] 或工具日志里）。旧版
    _parse_item 把「url 为空」当脏数据丢弃，导致列表里永远看不到正在
    直播的项，也就没法推给 NAS —— 见 docs/直播取址根因-2026-11-23.md。
    """

    content_id: str          # 纯数字 id
    title: str
    url: str                 # 带签名直链（直播项可能为空，需 resolve_stream_url）
    source_url: str = ""
    cover_url: str = ""
    author: str = ""         # 作者昵称（工具从 accounts[].nickname 给）
    publish_time: int = 0    # 作品发布时刻（unix 秒）
    captured_at: int = 0     # 被工具捕获的时刻（unix 毫秒）
    duration_seconds: float = 0.0  # 只有解析（HEAD/整段探测）后才知道
    is_live: bool = False    # 直播流：地址要另取，不能直接当文件下载
    live_id: str = ""        # 直播场次 id（orig_<id>_ 里的那段）
    decrypt_key: int = 0     # 日志直接带回的 decodeKey（未入库回放无需再查 detail）

    @property
    def api_id(self) -> str:
        return f"wxchannels:{self.content_id}"

    @property
    def needs_stream_url(self) -> bool:
        """直播项且还没有地址 —— 推 NAS / 解析前必须先取址。"""
        return self.is_live and not self.url

    @property
    def publish_text(self) -> str:
        return _fmt_ts(self.publish_time, ms=False)

    @property
    def captured_text(self) -> str:
        return _fmt_ts(self.captured_at, ms=True)


@dataclass
class ContentPage:
    """一页列表结果。

    \u5DE5\u5177\u7684 /api/content/list \u652F\u6301 page/page_size/limit/offset/keyword\uFF0C
    \u5E76\u8FD4\u56DE total \u2014\u2014 \u53EA\u53D6 list \u4F1A\u628A\u603B\u6570\u4E22\u6389\uFF0C\u754C\u9762\u4E0A\u5C31\u53EA\u80FD\u663E\u793A
    "\u672C\u6B21\u8BFB\u5230 N \u6761"\uFF0C\u7FFB\u9875\u4E5F\u65E0\u4ECE\u5224\u65AD\u6709\u6CA1\u6709\u4E0B\u4E00\u9875\u3002
    """

    items: list[ContentItem]
    total: int = 0
    page: int = 1
    page_size: int = 0

    @property
    def has_more(self) -> bool:
        return self.page_size > 0 and self.page * self.page_size < self.total


@dataclass
class ProbeInfo:
    """\u4E0B\u8F7D\u524D\u89E3\u6790\uFF08HEAD \u63A2\u6D4B\uFF09\u7684\u7ED3\u8BBA\u3002

    \u4E0D\u89E3\u6790\u4E5F\u80FD\u4E0B\uFF0C\u4F46\u4EE3\u4EF7\u662F\uFF1A\u5730\u5740\u8FC7\u671F\uFF08\u5E26 sign/svrnonce \u6709\u65F6\u6548\uFF09\u8981\u7B49
    \u4E0B\u5230 0 \u5B57\u8282\u624D\u77E5\u9053\uFF1B\u4E0D\u77E5\u9053\u8981\u4E0D\u8981\u5148\u53D6\u89E3\u5BC6\u5BC6\u94A5\uFF0C\u53EF\u80FD\u767D\u62C9\u51E0\u767E MB \u5BC6\u6587\uFF1B
    \u8FDB\u5EA6\u6761\u6CA1\u6709\u603B\u6570\uFF0C\u53EA\u80FD\u662F\u8109\u51B2\u52A8\u753B\u3002\u89E3\u6790\u53EA\u8981\u4E00\u4E2A HEAD \u8BF7\u6C42\u3002
    """

    ok: bool = False
    status: int = 0
    encrypted: bool = False
    enc_len: int = 0
    size_bytes: int = -1
    reason: str = ""

    @property
    def size_text(self) -> str:
        return _fmt_size(self.size_bytes)

    @property
    def summary(self) -> str:
        if not self.ok:
            return self.reason or "\u89E3\u6790\u5931\u8D25"
        return ("\u52A0\u5BC6" if self.encrypted else "\u660E\u6587") + " / " + self.size_text


def _api_get(base: str, path: str, timeout: int = 20) -> dict:
    url = base.rstrip("/") + path
    # \u672C\u5730 API\uFF0C\u7EDD\u4E0D\u80FD\u8D70\u7CFB\u7EDF\u4EE3\u7406
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    try:
        with opener.open(url, timeout=timeout) as r:
            return json.loads(r.read().decode("utf-8"))
    except Exception as exc:  # noqa: BLE001
        raise FetchError(
            f"\u65E0\u6CD5\u8BBF\u95EE {path}\u3002\u6293\u6D41\u5DE5\u5177\u662F\u5426\u5728\u8FD0\u884C\uFF1F"
            f"\uFF08\u5E94\u5728 tools/wxcd \u4E0B\u7528\u6839\u547D\u4EE4\u542F\u52A8\uFF09\u539F\u59CB\u9519\u8BEF\uFF1A{exc}"
        ) from exc


def _parse_item(it: dict) -> ContentItem | None:
    """把工具返回的一条记录转成 ContentItem。

    只有「缺 id」才丢弃。**url 为空不再丢弃** —— 直播项本来就该是空，
    以前这条规则把正在直播的内容整条过滤掉，界面上一条都看不到。
    """
    cid = str(it.get("external_id") or "").strip()
    if not cid:
        return None

    url = str(it.get("url") or "").strip()
    is_live = str(it.get("type") or "").strip().lower() == "live"

    # 作者：accounts[] 里 role=owner 的是真正作者，其余是转发/合作号
    author = ""
    accounts = it.get("accounts") or []
    if isinstance(accounts, list) and accounts:
        pick = None
        for acc in accounts:
            if isinstance(acc, dict) and str(acc.get("role") or "") == "owner":
                pick = acc
                break
        if pick is None and isinstance(accounts[0], dict):
            pick = accounts[0]
        if pick:
            author = str(pick.get("nickname") or "").strip()

    def _int(key: str) -> int:
        try:
            return int(it.get(key) or 0)
        except (TypeError, ValueError):
            return 0

    return ContentItem(
        content_id=cid,
        title=str(it.get("title") or "").strip() or cid,
        url=url,
        source_url=str(it.get("source_url") or ""),
        cover_url=str(it.get("cover_url") or ""),
        author=author,
        publish_time=_int("publish_time"),
        captured_at=_int("created_at"),
        is_live=is_live,
        # 直播项的 url 恒为空，所以 live_id 直接取 external_id：
        # 它与流地址里 orig_<id>_ 的那段一致（实测两个 id 相同）。
        # 这个值是用来**精确匹配**候选地址的，不能留空 ——
        # 留空会退化成"随便取日志里最新一条"，那可能是别人的直播。
        live_id=cid if is_live else "",
    )


_LIVE_ID_RE = re.compile(r"/orig_([0-9]+)_")


def _live_id_from_url(url: str) -> str:
    """从视频号直播流地址里取场次 id。

    地址形如 .../trtc_1400419933/orig_2079009824565503324_orig.flv
    中间的 2079009824565503324 就是 live id（与 content.external_id 一致）。
    """
    m = _LIVE_ID_RE.search(url or "")
    return m.group(1) if m else ""


def _looks_like_live(url: str) -> bool:
    """这条地址是不是视频号**直播流** —— 必须比 liveurl 的判据更严。

    为什么不能直接用 liveurl.looks_like_live_url：那个判据是给「ffmpeg 命令行
    扫描」用的，只要域名命中 `finder.video.qq.com` 就算。但日志里同一个域名下
    还有**封面图**（/stodownload?encfilekey=...），实测被误当成直播流抓了出来
    （39 条候选里一半是封面图），推给 NAS 必然失败。

    直播流的可靠特征是路径里同时有 `/trtc_` 和 `.flv`：
        .../trtc_1400419933/orig_2079009794242106815_orig.flv
    封面/视频则走 /stodownload?encfilekey=... ，没有这两段。
    """
    low = (url or "").lower()
    if not low.startswith(("http://", "https://")):
        return False
    if "stodownload" in low or "encfilekey=" in low:
        return False
    return "/trtc_" in low and ".flv" in low


def _collect_live_urls(obj, out: list) -> None:
    """递归收集 dict/list 里所有像直播流的地址（保序、去重）。

    工具日志是深度嵌套 JSON，liveCdnUrl 既可能在 feed.live.liveSdkInfo，
    也可能在 profile.liveInfo 下。不写死路径，按「值像不像直播流」判断，
    这样上游改字段名也不会失效。
    """
    if isinstance(obj, dict):
        for val in obj.values():
            if isinstance(val, str):
                if _looks_like_live(val) and val not in out:
                    out.append(val)
            else:
                _collect_live_urls(val, out)
    elif isinstance(obj, list):
        for val in obj:
            _collect_live_urls(val, out)


def resolve_stream_url(item: ContentItem, api: str = DEFAULT_API,
                       timeout: int = 20) -> tuple:
    """取直播真实流地址，返回 (url, 来源说明)。

    两条路径，按「需不需要先点工具下载按钮」区分：

    1. /api/content/detail -> data.resources[].url
       只对**已入库**的直播有效。实测：没点过下载按钮的直播根本没入库，
       detail 直接返回 content not found。

    2. /api/logs -> 递归找 liveCdnUrl 形态的地址
       **不需要点任何按钮**。实测：在微信里打开直播间那一刻，前端就把带
       签名的 liveCdnUrl 打进了日志；14:07 的日志里已经有 14:10 才开始录
       的那条流。所以「点下载后自动解析」的正解是——根本不点下载。

    两条都失败才抛 FetchError，此时才是真的需要用户回微信重开直播间。
    """
    reasons = []

    # ---- 路径 1：detail（已入库的直播）----
    try:
        data = _api_get(api, "/api/content/detail?id=" +
                        urllib.parse.quote(item.api_id), timeout=timeout)
        payload = data.get("data") or {}
        if data.get("code") in (0, None) and payload:
            hit = ""
            for res in payload.get("resources") or []:
                if not isinstance(res, dict):
                    continue
                cand = (str(res.get("stream_url") or "").strip()
                        or str(res.get("url") or "").strip())
                if cand and _looks_like_live(cand):
                    hit = cand
                    break
            if hit:
                return hit, "工具详情"
            reasons.append("详情里没有直播地址")
        else:
            reasons.append("该直播未入库（还没点过工具下载按钮）")
    except Exception as exc:  # noqa: BLE001
        reasons.append("详情接口失败：" + type(exc).__name__)

    # ---- 路径 2：日志（未入库的直播也覆盖）----
    try:
        logs = _api_get(api, "/api/logs?page_size=800", timeout=timeout)
        entries = (logs.get("data") or {}).get("entries") or []
        found = []
        for entry in entries:
            raw = entry.get("raw") if isinstance(entry, dict) else None
            if not isinstance(raw, str):
                continue
            if ("liveCdnUrl" not in raw and "wxlivecdn" not in raw
                    and "wxqcloud" not in raw):
                continue
            try:
                obj = json.loads(raw)
            except (ValueError, TypeError):
                continue
            before = len(found)
            _collect_live_urls(obj, found)
            # 日志按时间倒序，新增的就是更新的场次，优先匹配同 live_id
            if len(found) > before and item.live_id:
                for cand in found[before:]:
                    if item.live_id in cand:
                        return cand, "工具日志"
        reasons.append("日志里没有进行中的直播地址")
    except Exception as exc:  # noqa: BLE001
        reasons.append("日志接口失败：" + type(exc).__name__)

    raise FetchError(
        "取不到直播地址（" + "；".join(reasons) + "）。\n\n"
        "请在微信里打开该直播间停留 3-5 秒，再点一次「刷新列表」。"
    )


_PROXY_ENV_KEYS = frozenset({"http_proxy", "https_proxy", "all_proxy"})

# 4xx：地址/签名层面失败，才是"主播下播 / 地址过期"。
_EXPIRED_HINTS = ("403", "404", "410", "forbidden")
# 连接/代理层面失败：不能据此说主播下播。
_NETWORK_HINTS = (
    "proxy", "connection refused", "connection reset",
    "network is unreachable", "no route to host", "failed to resolve",
    "name or service not known", "temporary failure in name resolution",
    "connection timed out", "operation timed out", "i/o error",
    "errno 101", "errno 111",
)


def _env_without_http_proxy() -> dict[str, str]:
    """复制当前环境，剥掉 http_proxy / https_proxy / all_proxy（大小写都剥）。

    ffmpeg/ffprobe 的 HTTP 协议层会读这些环境变量。本机出口 10808
    （Clash）异常时，ffprobe 连代理失败返回非 0，旧实现会把它统一说成
    "主播已下播"，把网络问题误判成内容问题。探测直播流应该直连 CDN，
    所以这里显式去掉代理环境（只影响本次子进程，不改全局）。
    """
    return {k: v for k, v in os.environ.items()
            if k.lower() not in _PROXY_ENV_KEYS}


def _last_error_line(stderr: str, limit: int = 200) -> str:
    for line in reversed((stderr or "").splitlines()):
        text = line.strip()
        if text:
            return text[:limit]
    return ""


def probe_stream(url: str, timeout: int = 12) -> tuple:
    """探测直播流当下是否真的能拉。返回 (ok, 说明)。

    为什么必须有这一步：工具库里的直播项**不会过期下线**
    （用户 11:26 打开过的直播间，到 14:36 还在列表里），
    但它的签名地址早就连不上了。直接把这种地址推给 NAS，
    NAS 会拉 30 秒空流再报"文件未增长"，用户还得回来重来一遍。

    用 ffprobe 而不用 HTTP HEAD：直播是持续流，没有 Content-Length，
    HEAD 只能判断 403/404，判不出"签名有效但主播已下播"这种最常见的情况。
    ffprobe 能真的读到音轨 = 主播此刻在播。

    错误分类很关键：只有 403/404/410 才提示"主播已下播/签名过期"；
    连接被拒、超时、DNS 失败、代理异常等一律报"网络/代理异常"，
    否则用户会被误导回微信重开一个本来还能拉的直播间。
    """
    exe = shutil.which("ffprobe")
    if not exe:
        return True, "没有 ffprobe，跳过探测"

    cmd = [
        exe, "-v", "error",
        "-rw_timeout", str(int(timeout * 1000000)),
        "-show_entries", "stream=codec_type",
        "-of", "default=noprint_wrappers=1",
        url,
    ]
    try:
        r = winproc.run(cmd, capture_output=True, text=True,
                        encoding="utf-8", errors="replace",
                        timeout=timeout + 8,
                        env=_env_without_http_proxy())
    except subprocess.TimeoutExpired:
        return False, (f"探测超时（网络/代理异常，未能确认主播是否在播）："
                       f"{timeout}s 内没有响应")
    except Exception as exc:  # noqa: BLE001
        return False, f"探测失败（网络/代理异常）：{type(exc).__name__}"

    out = r.stdout or ""
    if r.returncode == 0 and "codec_type=audio" in out:
        return True, "可拉流（含音轨）"
    if r.returncode == 0:
        return False, "能连但没有音轨"

    tail = _last_error_line(r.stderr)
    low = tail.lower()
    if any(h in low for h in _EXPIRED_HINTS):
        detail = f"：{tail}" if tail else ""
        return False, f"地址已失效（HTTP 4xx，多半是主播已下播或签名过期）{detail}"
    if any(h in low for h in _NETWORK_HINTS):
        detail = f"：{tail}" if tail else ""
        return False, f"探测失败（网络/代理异常，不是主播下播）{detail}"
    detail = f"：{tail}" if tail else ""
    return False, f"探测失败（ffprobe rc={r.returncode}）{detail}"


def resolve_playable_stream(item: ContentItem, api: str = DEFAULT_API,
                            timeout: int = 20) -> tuple:
    """取一个**当下真的能拉**的直播地址，返回 (url, 来源说明, 探测说明)。

    在 resolve_stream_url 之上加一层可用性校验并回退：
    日志里同一个主播会留下多次开播的地址，最新的那条未必是活的
    （实测 14:20 那条到 14:36 已失效，而 detail 里 14:07 那条还活着）。
    所以按候选逐个探，第一个能拉的胜出。
    """
    urls = list_stream_candidates(item, api=api, timeout=timeout)
    if not urls:
        raise FetchError(
            "取不到直播地址。\n\n"
            "请在微信里打开该直播间停留 3-5 秒，再点一次「刷新列表」。"
        )

    notes = []
    for url, src in urls[:6]:
        ok, why = probe_stream(url, timeout=min(12, timeout))
        notes.append(src + "：" + why)
        if ok:
            return url, src, why
    raise FetchError(
        "找到的地址都拉不动（多半是主播已下播，或需要重新打开直播间）。\n"
        + "；".join(notes[:4])
    )


def list_stream_candidates(item: ContentItem, api: str = DEFAULT_API,
                           timeout: int = 20) -> list:
    """按可信度列出该直播的所有候选地址 [(url, 来源), ...]。

    顺序：detail（工具自己认的那条，最准）→ 日志里同 live_id 的 → 日志里最新的。
    调用方负责逐个探测可用性。
    """
    seen = set()
    out = []

    def _add(url: str, src: str) -> None:
        key = (url or "").split("?", 1)[0]
        if url and key not in seen:
            seen.add(key)
            out.append((url, src))

    try:
        data = _api_get(api, "/api/content/detail?id=" +
                        urllib.parse.quote(item.api_id), timeout=timeout)
        payload = data.get("data") or {}
        if data.get("code") in (0, None):
            for res in payload.get("resources") or []:
                if not isinstance(res, dict):
                    continue
                cand = (str(res.get("stream_url") or "").strip()
                        or str(res.get("url") or "").strip())
                if cand and _looks_like_live(cand):
                    _add(cand, "工具详情")
    except Exception:  # noqa: BLE001
        pass

    try:
        logs = _api_get(api, "/api/logs?page_size=800", timeout=timeout)
        entries = (logs.get("data") or {}).get("entries") or []
        mine = []
        rest = []
        for entry in entries:
            raw = entry.get("raw") if isinstance(entry, dict) else None
            if not isinstance(raw, str):
                continue
            if ("liveCdnUrl" not in raw and "wxlivecdn" not in raw
                    and "wxqcloud" not in raw):
                continue
            try:
                obj = json.loads(raw)
            except (ValueError, TypeError):
                continue
            batch = []
            _collect_live_urls(obj, batch)
            mine.extend(batch)
        # 只收**同一个 live_id** 的地址。绝不能"日志里最新那条"兜底：
        # 实测两个不同主播同时在播，全局最新那条属于别人，
        # 拿它去录会把 B 主播的内容记成 A 的标题（串台）。
        for cand in mine:
            if item.live_id and item.live_id in cand:
                _add(cand, "工具日志")
    except Exception:  # noqa: BLE001
        pass

    preset = (getattr(item, "url", "") or "").strip()
    if preset and _looks_like_live(preset):
        _add(preset, "日志发现")

    return out


def url_media_key(url: str) -> str:
    """提取媒体条目的去重键（优先 encfilekey，退化为去 query 的 URL）。"""
    if not url:
        return ""
    try:
        qs = urllib.parse.parse_qs(urllib.parse.urlparse(url).query)
        enc = (qs.get("encfilekey") or [""])[0].strip()
        if enc:
            return "enc:" + enc
    except Exception:  # noqa: BLE001
        pass
    return url.split("?", 1)[0]


def discover_live_streams(api: str = DEFAULT_API,
                          timeout: int = 20) -> list:
    """从工具日志发现**正在直播**的场次，返回已带地址的 ContentItem 列表。

    为什么需要这条路径（2026-11-23 实测）：
        /api/content/list 只列 content 表，而 content 表里**只有点过下载/
        打开过的直播**。用户只是打开直播间看，没点那个下载按钮，
        这条直播就根本不在列表里 —— 于是"刷新列表看不到正在播的直播"。

        但地址其实已经拿到了：微信前端在进入直播间那一刻就把带签名的
        liveCdnUrl 打进了工具日志。实测在完全没入库的情况下，
        从日志取出的地址用 ffprobe 能直接拉到 aac+hevc。

    2026-11-26 补充：
        同时兼容两类日志结构：
        - set_live_feed / onJoinLive：feed.live.liveSdkInfo.liveCdnUrl
        - onFetchFeedProfile：feed.liveInfo.liveSdkInfo.liveCdnUrl（或 streamUrl）

    返回的 ContentItem.__class__ 与列表项相同，但 content_id 用的是
    直播场次 id —— 界面上用 "livediscover:" 前缀做 key，避免与
    /api/content/list 的条目撞车。
    """
    data = _api_get(api, "/api/logs?page_size=60&keyword=liveCdnUrl",
                    timeout=timeout)
    entries = (data.get("data") or {}).get("entries") or []

    out = []
    seen = set()
    for entry in entries:
        raw = entry.get("raw") if isinstance(entry, dict) else None
        if not isinstance(raw, str):
            continue
        try:
            obj = json.loads(raw)
        except (ValueError, TypeError):
            continue

        feed = obj.get("feed") or {}
        live = feed.get("live") or feed.get("liveInfo") or {}
        sdk = live.get("liveSdkInfo") or {}
        cdn = str(sdk.get("liveCdnUrl") or live.get("streamUrl") or "").strip()
        if not cdn or not _looks_like_live(cdn):
            continue

        key = cdn.split("?", 1)[0]
        if key in seen:
            continue
        seen.add(key)

        # live_id 必须从 CDN 地址里取，不能用 liveInfo.liveId：
        # 后者是 JS 数字，超过 2^53 会丢精度（实测 ...794797 变成
        # ...795000，差 203），拿它去匹配永远对不上。
        live_id = _live_id_from_url(cdn)
        if not live_id:
            continue

        od = feed.get("objectDesc") or {}
        title = str(live.get("liveDescription")
                    or od.get("description") or "").strip()
        if not title:
            title = "直播_" + live_id[-6:]
        author = ""
        prof = feed.get("profile")
        if isinstance(prof, dict):
            author = str(prof.get("nickname") or "").strip()
        if not author:
            contact = feed.get("contact")
            author = str(
                feed.get("nickname")
                or (contact.get("nickname") if isinstance(contact, dict) else "")
                or ""
            ).strip()

        out.append(ContentItem(
            content_id=live_id,
            title=title,
            url=cdn,
            author=author,
            is_live=True,
            live_id=live_id,
            captured_at=int(time.time() * 1000),
        ))
    return out


def discover_feed_videos(api: str = DEFAULT_API,
                         timeout: int = 20) -> list[ContentItem]:
    """从工具日志（set_feed）发现未入库的视频 / 直播回放。

    2026-11-26 实测：在微信里打开视频号视频或直播回放、但没点工具内置下载按钮时，
    /api/content/list 里没有记录；但 wxcd 前端已经把含 url + urlToken + decodeKey
    的完整 feed 打进了 set_feed 日志。把它们捞出来即可直接解析与下载，不必点下载按钮。
    """
    data = _api_get(api, "/api/logs?page_size=60&keyword=set_feed",
                    timeout=timeout)
    entries = (data.get("data") or {}).get("entries") or []

    out: list[ContentItem] = []
    seen_media: set[str] = set()
    for entry in entries:
        raw = entry.get("raw") if isinstance(entry, dict) else None
        if not isinstance(raw, str):
            continue
        try:
            obj = json.loads(raw)
        except (ValueError, TypeError):
            continue

        feed = obj.get("feed") or {}
        if not isinstance(feed, dict) or feed.get("liveInfo"):
            continue

        od = feed.get("objectDesc") or {}
        media_list = od.get("media") or []
        if not isinstance(media_list, list) or not media_list:
            continue
        m = media_list[0]
        if not isinstance(m, dict):
            continue

        full = str(m.get("fullUrl") or "").strip()
        if not full:
            base_url = str(m.get("url") or "").strip()
            tok = str(m.get("urlToken") or "").strip()
            if base_url and tok and tok not in base_url:
                full = base_url + tok
            else:
                full = base_url

        if not full.startswith(("http://", "https://")):
            continue
        if ("stodownload" not in full and not m.get("videoPlayLen")
                and not m.get("decodeKey")):
            continue

        mkey = url_media_key(full)
        if not mkey or mkey in seen_media:
            continue
        seen_media.add(mkey)

        cid = str(feed.get("id") or feed.get("objectId") or "").strip()
        if not cid:
            cid = mkey.replace("enc:", "")[:24]

        title = str(od.get("description") or "").strip() or f"视频_{cid[-6:]}"
        contact = feed.get("contact")
        author = str(
            feed.get("nickname")
            or (contact.get("nickname") if isinstance(contact, dict) else "")
            or ""
        ).strip()

        try:
            pub = int(feed.get("createtime") or 0)
        except (TypeError, ValueError):
            pub = 0
        try:
            dkey = int(str(m.get("decodeKey") or "0").strip() or 0)
        except (TypeError, ValueError):
            dkey = 0
        try:
            dur = float(m.get("videoPlayLen") or 0)
        except (TypeError, ValueError):
            dur = 0.0

        out.append(ContentItem(
            content_id=cid,
            title=title,
            url=full,
            cover_url=str(m.get("coverUrl") or "").strip(),
            author=author,
            publish_time=pub,
            captured_at=int(time.time() * 1000),
            duration_seconds=dur,
            is_live=False,
            decrypt_key=dkey,
        ))
    return out


def list_page(api: str = DEFAULT_API, keyword: str = "",
              limit: int | None = None, offset: int | None = None,
              page: int | None = None, page_size: int | None = None,
              platform: str = "wxchannels") -> ContentPage:
    """\u8BFB\u4E00\u9875\u5185\u5BB9\u5217\u8868\uFF0C\u5E26 total\u3002

    \u53C2\u6570\u5BF9\u5E94\u5DE5\u5177\u81EA\u5DF1\u7684\u67E5\u8BE2\u53C2\u6570\uFF08internal/api/handler_content.go \u7684
    handle_content_list_with_type \u91CC those form \u7ED1\u5B9A\uFF09\uFF1A
        keyword / limit / offset / page / page_size / platform_id

    \u6CE8\u610F keyword \u662F**\u670D\u52A1\u7AEF**\u8FC7\u6EE4\uFF08SQL LIKE\uFF09\uFF0C\u4E0D\u662F\u524D\u7AEF\u8FC7\u6EE4\uFF1B
    \u4E2D\u6587\u5FC5\u987B URL \u7F16\u7801\uFF0C\u5426\u5219\u670D\u52A1\u7AEF\u6536\u5230\u4E71\u7801\u4F1A\u8FD4\u56DE 0 \u6761\uFF08\u5B9E\u6D4B\uFF09\u3002
    """
    qs: list[str] = []
    if platform:
        qs.append("platform_id=" + urllib.parse.quote(platform))
    kw = (keyword or "").strip()
    if kw:
        qs.append("keyword=" + urllib.parse.quote(kw))
    if limit is not None and limit > 0:
        qs.append(f"limit={int(limit)}")
    if offset is not None and offset >= 0:
        qs.append(f"offset={int(offset)}")
    if page is not None and page > 0:
        qs.append(f"page={int(page)}")
    if page_size is not None and page_size > 0:
        qs.append(f"page_size={int(page_size)}")

    path = "/api/content/list" + (("?" + "&".join(qs)) if qs else "")
    data = _api_get(api, path)
    if data.get("code") not in (0, None):
        raise FetchError(f"\u63A5\u53E3\u8FD4\u56DE\u9519\u8BEF\uFF1A{data.get('msg')}")

    payload = data.get("data") or {}
    items: list[ContentItem] = []
    for raw in payload.get("list") or []:
        if isinstance(raw, dict):
            item = _parse_item(raw)
            if item:
                items.append(item)

    try:
        total = int(payload.get("total") or 0)
    except (TypeError, ValueError):
        total = 0
    if not total:
        total = len(items)

    return ContentPage(items=items, total=total,
                       page=int(payload.get("page") or 1),
                       page_size=int(payload.get("page_size") or len(items)))


def list_contents(api: str = DEFAULT_API, keyword: str = "",
                  limit: int | None = None,
                  offset: int | None = None) -> list[ContentItem]:
    """\u5217\u51FA\u5DE5\u5177\u5DF2\u6355\u83B7\u7684\u5185\u5BB9\uFF08\u65B0\u2192\u65E7\uFF09\u3002\u53EA\u5173\u5FC3\u6761\u76EE\u65F6\u7528\u8FD9\u4E2A\u3002"""
    return list_page(api, keyword=keyword, limit=limit, offset=offset).items


def find_content(keyword: str, api: str = DEFAULT_API) -> ContentItem:
    """\u6309\u6807\u9898\u5173\u952E\u8BCD\u6216 id \u627E\u5185\u5BB9\u3002\u547D\u4E2D\u591A\u6761\u65F6\u53D6\u7B2C\u4E00\u6761\u3002"""
    kw = keyword.strip()
    if not kw:
        raise FetchError("\u5173\u952E\u8BCD\u4E0D\u80FD\u4E3A\u7A7A")
    page = list_page(api, keyword=kw)
    items = page.items
    for it in items:
        if it.content_id == kw or it.api_id == kw:
            return it
    hits = [it for it in items if kw.lower() in it.title.lower()]
    if not hits:
        titles = "\u3001".join(f"\u300A{it.title}\u300B" for it in items[:5]) or "\uFF08\u7A7A\uFF09"
        raise FetchError(f"\u6CA1\u627E\u5230\u5305\u542B\u300C{kw}\u300D\u7684\u5185\u5BB9\u3002\u5DF2\u6355\u83B7\uFF1A{titles}")
    return hits[0]


def probe_item(item: ContentItem, timeout: int = 20) -> ProbeInfo:
    """\u4E0B\u8F7D\u524D\u89E3\u6790\uFF1A\u5730\u5740\u662F\u5426\u8FC7\u671F\u3001\u8981\u4E0D\u8981\u89E3\u5BC6\u3001\u6587\u4EF6\u591A\u5927\u3002

    \u8FD9\u4E00\u6B65\u662F"\u53EF\u9009"\u7684 \u2014\u2014 \u4E0D\u89E3\u6790\u76F4\u63A5\u4E0B\u4E5F\u80FD\u6210\uFF0C\u4F46\u4EE3\u4EF7\u89C1 ProbeInfo \u6CE8\u91CA\u3002
    """
    from .sphdecrypt import head_info

    try:
        info = head_info(item.url, timeout=timeout)
    except Exception as exc:  # noqa: BLE001
        return ProbeInfo(ok=False, reason=f"\u8FDE\u4E0D\u4E0A\uFF1A{type(exc).__name__}: {exc}")

    try:
        status = int(info.get("status") or 0)
    except (TypeError, ValueError):
        status = 0

    if status in (401, 403):
        return ProbeInfo(
            ok=False, status=status,
            reason=f"HTTP {status}\uFF1A\u5730\u5740\u53EF\u80FD\u5DF2\u8FC7\u671F\uFF0C\u8BF7\u5728\u5FAE\u4FE1\u91CC\u91CD\u65B0\u6253\u5F00\u8BE5\u89C6\u9891\u518D\u5237\u65B0")
    if status == 404:
        return ProbeInfo(ok=False, status=status,
                         reason="HTTP 404\uFF1A\u8D44\u6E90\u4E0D\u5B58\u5728\uFF08\u53EF\u80FD\u5DF2\u4E0B\u67B6\uFF09")
    if status >= 400:
        return ProbeInfo(ok=False, status=status, reason=f"HTTP {status}")

    enc_flag = 0
    enc_len = 0
    size = -1
    try:
        enc_flag = int(info.get("enc_flag") or 0)
        enc_len = int(info.get("enc_len") or 0)
        size = int(info.get("content_length") or -1)
    except (TypeError, ValueError):
        pass

    return ProbeInfo(ok=True, status=status, encrypted=bool(enc_flag),
                     enc_len=enc_len, size_bytes=size)


def get_decrypt_key(item: ContentItem, api: str = DEFAULT_API) -> int:
    """取解密密钥。优先用日志直接带回的 decrypt_key，否则查 /api/content/detail。"""
    preset = getattr(item, "decrypt_key", 0)
    if preset:
        try:
            return int(preset)
        except (TypeError, ValueError):
            pass
    data = _api_get(api, "/api/content/detail?id=" +
                    urllib.parse.quote(item.api_id))
    if data.get("code") not in (0, None):
        raise FetchError(f"\u53D6\u8BE6\u60C5\u5931\u8D25\uFF1A{data.get('msg')}")
    content = (data.get("data") or {}).get("content") or {}
    raw = content.get("metadata") or ""
    if not raw:
        raise FetchError("\u8BE6\u60C5\u91CC\u6CA1\u6709 metadata\uFF0C\u62FF\u4E0D\u5230\u89E3\u5BC6\u5BC6\u94A5")
    try:
        meta = json.loads(raw) if isinstance(raw, str) else raw
        return int(str(meta.get("key") or "0"))
    except (ValueError, TypeError) as exc:
        raise FetchError(f"\u89E3\u5BC6\u5BC6\u94A5\u89E3\u6790\u5931\u8D25\uFF1A{raw!r}") from exc


def find_wxcd_exe() -> Path | None:
    """定位本地抓流工具可执行文件（tools/wxcd/wx_video_download.exe）。"""
    cand = Path(__file__).resolve().parent.parent / "tools" / "wxcd" / "wx_video_download.exe"
    return cand if cand.is_file() else None


def restart_wxcd(api: str = DEFAULT_API, timeout: float = 12.0) -> tuple[bool, str]:
    """重启本地抓流工具（不带 server 参数直接运行），并确认代理接管 2023。"""
    from . import systemproxy

    exe = find_wxcd_exe()
    if exe is None:
        return False, "找不到 tools/wxcd/wx_video_download.exe"

    if sys.platform == "win32":
        winproc.run(
            ["taskkill", "/F", "/IM", exe.name],
            capture_output=True, text=True, timeout=8,
        )
        time.sleep(0.6)

    flags = 0
    if sys.platform == "win32":
        flags = (getattr(subprocess, "CREATE_NO_WINDOW", 0x08000000)
                 | getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0x00000200))
    winproc.popen(
        [str(exe)],
        cwd=str(exe.parent),
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        creationflags=flags,
        close_fds=True,
    )

    deadline = time.time() + max(3.0, timeout)
    api_ready = False
    while time.time() < deadline:
        try:
            st = _api_get(api, "/api/status", timeout=2)
            info = st.get("data") or {}
            if (info.get("api") or {}).get("listening") and (info.get("proxy") or {}).get("listening"):
                api_ready = True
                break
        except Exception:  # noqa: BLE001
            pass
        time.sleep(0.4)

    if not api_ready:
        return False, "抓流工具启动超时，2022/2023 端口未就绪"

    # 工具拉起拦截器后约 1s 会写注册表系统代理；若未写则由我们补写
    for _ in range(5):
        if systemproxy.is_tool_takeover():
            return True, f"抓流工具已重启，系统代理已接管为 {systemproxy.TOOL_PROXY}"
        time.sleep(0.4)

    ok, msg = systemproxy.enable_tool_proxy()
    if ok:
        return True, f"抓流工具已重启，{msg}"
    return False, f"抓流工具已重启，但代理接管失败：{msg}"


def _find_ffmpeg() -> str:
    exe = shutil.which("ffmpeg")
    if not exe:
        raise FetchError("\u627E\u4E0D\u5230 ffmpeg\uFF0C\u8BF7\u5148\u5B89\u88C5")
    return exe


def safe_filename(name: str) -> str:
    """\u53BB\u6389 Windows \u6587\u4EF6\u540D\u975E\u6CD5\u5B57\u7B26\u3002"""
    bad = '<>:"/\\|?*'
    out = "".join("_" if c in bad else c for c in name)
    out = "".join(" " if ord(c) < 32 else c for c in out)
    out = out.strip().strip(".")
    # Windows \u4FDD\u7559\u540D\uFF08CON/PRN/AUX/NUL/COM1..9/LPT1..9\uFF09\uFF0C\u5E26\u540E\u7F00\u4E5F\u4E0D\u884C
    stem = out.split(".")[0].upper()
    reserved = {"CON", "PRN", "AUX", "NUL"} | {
        f"{p}{i}" for p in ("COM", "LPT") for i in range(1, 10)}
    if stem in reserved:
        out = "_" + out
    return out[:120] or "untitled"


def unique_path(path: Path) -> Path:
    """\u540C\u540D\u6587\u4EF6\u4E0D\u8986\u76D6\uFF0C\u8FFD\u52A0 (2)(3)\u2026\u3002

    \u5DE5\u5177\u81EA\u5DF1\u7684\u4E0B\u8F7D\u4E5F\u8E29\u8FC7\u8FD9\u4E2A\u5751\uFF08\u88AB\u91CD\u540D\u89C6\u9891\u8986\u76D6\uFF09\uFF0C\u6211\u4EEC\u540C\u6837\u8981\u907F\u8BA9\uFF1A
    \u540C\u4E00\u4E2A\u76F4\u64AD\u95F4\u5F00\u4E24\u573A\uFF0C\u6807\u9898\u53EF\u80FD\u4E00\u6A21\u4E00\u6837\u3002
    """
    path = Path(path)
    if not path.exists():
        return path
    stem, suffix = path.stem, path.suffix
    for i in range(2, 1000):
        cand = path.with_name(f"{stem}({i}){suffix}")
        if not cand.exists():
            return cand
    raise FetchError(f"\u540C\u540D\u6587\u4EF6\u8FC7\u591A\uFF1A{path.name}")


def download_decrypted(item: ContentItem, key: int,
                       dest: Path | None = None,
                       on_progress=None) -> Path:
    """\u4E0B\u8F7D\u5E76\u89E3\u5BC6\u6574\u6BB5\u5A92\u4F53\uFF0C\u8FD4\u56DE\u843D\u5730\u8DEF\u5F84\u3002

    \u5FC5\u987B\u5148\u5B8C\u6574\u843D\u76D8\u518D\u7ED9 ffmpeg\uFF1A\u89C6\u9891\u53F7\u7684 MP4 \u662F\u5206\u7247\u7ED3\u6784\uFF0C
    moov \u5728\u6587\u4EF6\u5934\u4E4B\u540E\uFF0C\u7528\u7BA1\u9053\u5582 ffmpeg \u65F6\u65E0\u6CD5 seek\uFF0C
    \u4F1A\u62A5 "moov atom not found"\u3002
    """
    flag, enc_len = probe_encryption(item.url)
    if flag and not key:
        raise FetchError("\u6587\u4EF6\u5DF2\u52A0\u5BC6\u4F46\u672A\u62FF\u5230\u5BC6\u94A5\uFF0C\u65E0\u6CD5\u89E3\u5BC6")

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
        raise FetchError("\u4E0B\u8F7D\u5230 0 \u5B57\u8282\uFF0C\u5730\u5740\u53EF\u80FD\u5DF2\u8FC7\u671F")
    os.replace(part, dest)
    return dest


def extract_audio(video: Path, wav: Path | None = None,
                  sample_rate: int = 16000, on_progress=None) -> Path:
    """\u4ECE\u89C6\u9891\u4E2D\u62BD\u51FA 16k \u5355\u58F0\u9053 WAV\uFF08\u8F6C\u5199\u5F15\u64CE\u7684\u8F93\u5165\u683C\u5F0F\uFF09\u3002"""
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
    # 必须隐藏窗口：GUI 从 pythonw 启动时没有控制台，
    # 每起一个 console 程序系统都会新开一个黑框闪一下。
    r = winproc.run(cmd, capture_output=True, text=True,
                    encoding="utf-8", errors="replace")
    if r.returncode != 0 or not wav.exists() or wav.stat().st_size == 0:
        tail = (r.stderr or "").strip()[-400:]
        raise FetchError(f"\u62BD\u97F3\u9891\u5931\u8D25\uFF08rc={r.returncode}\uFF09\uFF1A{tail}")
    return wav


def probe_media(path: Path) -> tuple[float, bool]:
    """\u8FD4\u56DE (\u65F6\u957F\u79D2, \u662F\u5426\u6709\u97F3\u8F68)\u3002"""
    exe = shutil.which("ffprobe")
    if not exe:
        return 0.0, False
    cmd = [exe, "-v", "error", "-show_entries", "format=duration",
           "-show_entries", "stream=codec_type",
           "-of", "default=noprint_wrappers=1", str(path)]
    r = winproc.run(cmd, capture_output=True, text=True,
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


def nas_supports_decrypt(base_url: str, timeout: float = 4.0, token: str = "") -> bool:
    """检查目标 NAS 服务端是否原生支持 decrypt_key（ProbeReq / StreamStartReq）。"""
    base = (base_url or "").strip().rstrip("/")
    if not base:
        return False
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    headers = {"X-WXLIVE-Token": token.strip()} if (token or "").strip() else {}
    req = urllib.request.Request(base + "/openapi.json", headers=headers)
    try:
        with opener.open(req, timeout=timeout) as r:
            spec = json.loads(r.read().decode("utf-8"))
        schemas = (spec.get("components") or {}).get("schemas") or {}
        probe_props = (schemas.get("ProbeReq") or {}).get("properties") or {}
        stream_props = (schemas.get("StreamStartReq") or {}).get("properties") or {}
        return "decrypt_key" in probe_props and "decrypt_key" in stream_props
    except Exception:  # noqa: BLE001
        return False


def upload_to_nas(base_url: str, file_path: Path, name: str = "",
                  auto_transcribe: bool = True, on_progress=None,
                  timeout: int = 300, token: str = "") -> dict:
    """把本地音频/媒体文件流式上传到 NAS `/api/jobs/upload`，返回任务 JSON。

    使用 http.client 直连局域网，绕过系统代理且避免把上百 MB 文件整个读进内存。
    """
    import http.client
    import uuid

    p = Path(file_path)
    if not p.is_file():
        raise FetchError(f"待上传文件不存在：{p}")
    size = p.stat().st_size
    if size <= 0:
        raise FetchError("待上传文件为空（0 字节）")

    parsed = urllib.parse.urlsplit((base_url or "").strip().rstrip("/"))
    scheme = parsed.scheme.lower()
    if scheme not in ("http", "https") or not parsed.hostname:
        raise FetchError(f"无效的 NAS 地址：{base_url}")

    boundary = f"----wxlive-{uuid.uuid4().hex}"
    fields = [
        ("name", name or p.stem),
        ("auto_transcribe", "true" if auto_transcribe else "false"),
    ]
    parts_head = bytearray()
    for k, v in fields:
        parts_head.extend(
            f"--{boundary}\r\n"
            f'Content-Disposition: form-data; name="{k}"\r\n\r\n'
            f"{v}\r\n".encode("utf-8")
        )
    safe_name = f"upload{p.suffix.lower() or '.wav'}"
    parts_head.extend(
        f"--{boundary}\r\n"
        f'Content-Disposition: form-data; name="file"; filename="{safe_name}"\r\n'
        f"Content-Type: application/octet-stream\r\n\r\n".encode("utf-8")
    )
    tail = f"\r\n--{boundary}--\r\n".encode("utf-8")
    total_len = len(parts_head) + size + len(tail)

    prefix = parsed.path.rstrip("/")
    req_path = f"{prefix}/api/jobs/upload"
    conn_cls = http.client.HTTPSConnection if scheme == "https" else http.client.HTTPConnection
    port = parsed.port or (443 if scheme == "https" else 80)
    conn = conn_cls(parsed.hostname, port, timeout=timeout)
    try:
        conn.putrequest("POST", req_path)
        conn.putheader("Content-Type", f"multipart/form-data; boundary={boundary}")
        conn.putheader("Content-Length", str(total_len))
        tok = (token or "").strip()
        if tok:
            conn.putheader("X-WXLIVE-Token", tok)
        conn.endheaders()
        conn.send(bytes(parts_head))

        sent = 0
        with p.open("rb") as f:
            while True:
                buf = f.read(1 << 20)
                if not buf:
                    break
                conn.send(buf)
                sent += len(buf)
                if on_progress:
                    on_progress(sent, size)
        conn.send(tail)

        resp = conn.getresponse()
        body = resp.read().decode("utf-8", "replace")
        if resp.status >= 400:
            raise FetchError(f"NAS 上传被拒 HTTP {resp.status}：{body[:300]}")
        return json.loads(body)
    except FetchError:
        raise
    except Exception as exc:  # noqa: BLE001
        raise FetchError(f"上传到 NAS 失败：{type(exc).__name__}: {exc}") from exc
    finally:
        try:
            conn.close()
        except Exception:  # noqa: BLE001
            pass
