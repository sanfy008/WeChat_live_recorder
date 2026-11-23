"""把本地抓流工具捕获到的视频号直播/回放，一键推给 NAS 上的 wxlive 服务。

为什么需要这个：方案 A 的分工是「本地拿地址 → NAS 长时间拉流转写」。
如果每次都要人手工把 URL 复制出来、再粘到浏览器表单里，很容易既慢又贴错。
这个脚本把三步串起来：

    1. 从抓流工具 API 读它已经捕获到的内容（或直接给一条 URL/ffmpeg 命令）
    2. 调 NAS 的 /api/probe 先确认能拉、有音轨
    3. 调 NAS 的 /api/jobs/stream 起录制任务（停录后自动转写）

用法：

    # 列出抓流工具里已捕获的内容（先看有什么）
    python scripts/push_to_nas.py --list

    # 按标题关键词，把最新一条推给 NAS 开录
    python scripts/push_to_nas.py --match 升学

    # 直接给视频号抓流工具给的流地址（整条 ffmpeg 命令也行）
    python scripts/push_to_nas.py --url "https://pull-m1.wxlivecdn.com/...flv?txTime=..."

    # 指定 NAS 地址 / 场次名 / 只录不转写
    python scripts/push_to_nas.py --match 升学 --nas http://192.168.68.49:8910 \\
        --name 20261123_升学直播 --no-transcribe

设计约束：
- 只做"发起"，不做守护。录制与转写在 NAS 上跑，本地随时可关。
- 抓流工具没启动时给出可执行的提示，而不是抛一堆 traceback。
- NAS 端 4xx 的原话（含"没识别出流地址"这类说明）直接透给用户。
"""

from __future__ import annotations

import argparse
import json
import sys
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

DEFAULT_NAS = "http://192.168.68.49:8910"
DEFAULT_TOOL_API = "http://127.0.0.1:2022"


def _post_json(url: str, payload: dict, timeout: int = 60) -> dict:
    req = urllib.request.Request(
        url,
        data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
        headers={"Content-Type": "application/json"},
    )
    # NAS 在局域网内，不能走系统代理（本机固定 10808 出口）
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    with opener.open(req, timeout=timeout) as r:
        return json.loads(r.read().decode("utf-8"))


def _get_json(url: str, timeout: int = 20) -> dict:
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    with opener.open(url, timeout=timeout) as r:
        return json.loads(r.read().decode("utf-8"))


def _strip_quotes(text: str) -> str:
    return (text or "").strip().strip("\"'")


def list_captured(tool_api: str, keyword: str = "") -> list[dict]:
    """读抓流工具已捕获的内容列表（新→旧）。"""
    qs = ["platform_id=wxchannels", "page_size=50"]
    if keyword:
        qs.append("keyword=" + urllib.parse.quote(keyword))
    data = _get_json(tool_api.rstrip("/") + "/api/content/list?" + "&".join(qs))
    if data.get("code") not in (0, None):
        raise RuntimeError(f"抓流工具返回错误：{data.get('msg')}")
    return ((data.get("data") or {}).get("list") or [])


def _tool_hint() -> str:
    return (
        "连不上抓流工具。\n"
        "请先在 tools/wxcd 下用根命令启动（不要加 server 子命令）：\n"
        "    cd tools/wxcd && ./wx_video_download.exe\n"
        "然后在微信里打开目标直播/回放，等页面出现下载按钮后再试。"
    )


def main() -> int:
    ap = argparse.ArgumentParser(
        description="把视频号直播/回放推给 NAS 上的 wxlive 服务（拉流 + 转写）"
    )
    src = ap.add_mutually_exclusive_group()
    src.add_argument("--list", action="store_true", help="只列出抓流工具里已捕获的内容")
    src.add_argument("--match", default="", help="按标题关键词匹配已捕获内容（取最新一条）")
    src.add_argument("--url", default="", help="直接给流地址或整条 ffmpeg -i 命令")
    src.add_argument("--from-local", action="store_true",
                     help="从本机运行中的 ffmpeg 自动取直播地址（最省事）")
    src.add_argument("--list-live", action="store_true",
                     help="只列出本机正在进行中的直播流")
    ap.add_argument("--nas", default=DEFAULT_NAS, help=f"NAS 服务地址（默认 {DEFAULT_NAS}）")
    ap.add_argument("--tool-api", default=DEFAULT_TOOL_API,
                    help=f"本地抓流工具 API（默认 {DEFAULT_TOOL_API}）")
    ap.add_argument("--name", default="", help="NAS 上的场次名称（留空自动取标题/时间）")
    ap.add_argument("--no-transcribe", action="store_true", help="只录制，不自动转写")
    ap.add_argument("--probe-only", action="store_true", help="只探测能否拉流，不开始录制")
    args = ap.parse_args()

    nas = args.nas.rstrip("/")
    try:
        health = _get_json(nas + "/api/health")
    except Exception as exc:  # noqa: BLE001
        print(f"连不上 NAS 服务：{nas}\n{exc}", file=sys.stderr)
        return 2
    if not health.get("ok"):
        print(f"NAS 服务异常：{health}", file=sys.stderr)
        return 2
    print(f"NAS 就绪：ffmpeg={health.get('ffmpeg_available')} "
          f"引擎已加载={health.get('engine_loaded')} "
          f"剩余磁盘={health.get('disk_free_gb')} GB")

    # ---- 只列本机正在进行的直播 ----
    if args.list_live:
        from wxlive import liveurl

        streams = liveurl.find_live_streams(tool_api=args.tool_api)
        if not streams:
            print("本机没有正在拉取的视频号直播流。")
            print("请先在微信里打开直播页，并点一次抓流工具的下载按钮"
                  "（让它起 ffmpeg），然后再试。")
            return 1
        print(f"本机进行中的直播 {len(streams)} 路：")
        for i, s in enumerate(streams, 1):
            print(f"  {i}. pid={s.pid} id={s.stream_id}  {s.title_hint}")
            print(f"     签名={'有' if s.has_signature else '无'} "
                  f"len={len(s.url)}")
        return 0

    # ---- 从本机 ffmpeg 自动取址 ----
    if args.from_local:
        from wxlive import liveurl

        streams = liveurl.find_live_streams(tool_api=args.tool_api)
        if not streams:
            print("本机没有正在拉取的视频号直播流。", file=sys.stderr)
            print("请先在微信里打开直播页，点一次抓流工具的下载按钮"
                  "（让它起 ffmpeg 进程），再运行本命令。", file=sys.stderr)
            return 1
        pick = streams[0]
        if len(streams) > 1:
            print(f"检测到 {len(streams)} 路直播，取第一路：{pick.title_hint}")
        print(f"取到直播流：{pick.title_hint}（pid={pick.pid}）")
        args.url = pick.url
        if not args.name:
            args.name = pick.title_hint

    # ---- 只列清单 ----
    if args.list:
        try:
            items = list_captured(args.tool_api)
        except Exception as exc:  # noqa: BLE001
            print(_tool_hint(), file=sys.stderr)
            print(f"原始错误：{exc}", file=sys.stderr)
            return 2
        if not items:
            print("抓流工具里还没有捕获到内容。先在微信里打开目标直播/回放。")
            return 1
        print(f"\n已捕获 {len(items)} 条（新→旧）：")
        for i, it in enumerate(items, 1):
            cid = it.get("external_id") or "?"
            title = (it.get("title") or "").strip() or "(无标题)"
            print(f"  {i}. {title}")
            print(f"     id={cid}")
        return 0

    # ---- 解析出目标 URL ----
    url = ""
    title = ""
    if args.url:
        url = _strip_quotes(args.url)
        if not args.name:
            from wxlive import liveurl

            eid = liveurl.extract_external_id(url)
            if eid:
                author, live_t = liveurl.resolve_wxcd_metadata(eid, tool_api=args.tool_api)
                title = liveurl.format_live_title(author, live_t)
    elif args.match:
        from wxlive import liveurl

        try:
            items = list_captured(args.tool_api, keyword=args.match)
        except Exception as exc:  # noqa: BLE001
            print(_tool_hint(), file=sys.stderr)
            print(f"原始错误：{exc}", file=sys.stderr)
            return 2
        hits = [it for it in items
                if args.match.lower() in (it.get("title") or "").lower()]
        if not hits:
            print(f"没找到标题包含「{args.match}」的内容。", file=sys.stderr)
            if items:
                print("当前可见标题：" + "、".join(
                    (it.get("title") or "")[:20] for it in items[:8]), file=sys.stderr)
            return 1
        pick = hits[0]
        url = _strip_quotes(pick.get("url") or "")
        author = liveurl._extract_owner_nickname(pick)
        title = liveurl.format_live_title(author, pick.get("title") or "")
        if not url:
            print("这条内容没有可用地址（可能只在抓流工具里登记过、没抓到真实 URL）。",
                  file=sys.stderr)
            return 1
        print(f"目标：《{title}》")
    else:
        print("请给出 --match 关键词、--url，或先用 --list 看看有什么。", file=sys.stderr)
        return 1

    # ---- 探测 ----
    try:
        probe = _post_json(nas + "/api/probe", {"url": url})
    except urllib.error.HTTPError as exc:
        print(f"NAS 探测失败（HTTP {exc.code}）：{exc.read().decode('utf-8', 'replace')}",
              file=sys.stderr)
        return 1
    except Exception as exc:  # noqa: BLE001
        print(f"NAS 探测失败：{exc}", file=sys.stderr)
        return 1

    if not probe.get("ok"):
        print(f"探测失败：{probe.get('error')}", file=sys.stderr)
        print("常见原因：地址已过期（回微信重新打开该直播/回放），或该内容没有音轨。",
              file=sys.stderr)
        return 1
    print(f"探测通过：格式={probe.get('format') or '未知'} 含音轨={probe.get('has_audio')}")
    if args.probe_only:
        return 0

    # ---- 起任务 ----
    payload = {
        "url": url,
        "name": args.name or title,
        "auto_transcribe": (not args.no_transcribe),
    }
    try:
        job = _post_json(nas + "/api/jobs/stream", payload)
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", "replace")
        print(f"NAS 拒绝开录（HTTP {exc.code}）：{detail}", file=sys.stderr)
        return 1
    except Exception as exc:  # noqa: BLE001
        print(f"NAS 开录失败：{exc}", file=sys.stderr)
        return 1

    print(f"\n✅ 已开始在 NAS 录制：{job.get('title')}")
    print(f"   任务 ID：{job.get('id')}")
    print(f"   自动转写：{job.get('auto_transcribe')}")
    print(f"   进度与逐字稿：{nas}/")
    print("\n现在本地可以关掉播放器了（录制在 NAS 上进行）。")
    return 0


if __name__ == "__main__":
    sys.exit(main())
