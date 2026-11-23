"""本机地址代理：让 NAS 反向"借"本机抓流工具已经拿到的直播地址。

## 为什么

方案 A 的分工是「本机拿地址 → NAS 长时间拉流转写」。但"拿地址"这一步
在微信里其实很别扭：

- 点抓流工具的主按钮 = 它自己起 ffmpeg 录（占本地磁盘、还经常断）
- 下拉菜单里的"复制命令"很难找，很多人根本没见过

而真实的流地址**已经在**本机 ffmpeg 的命令行里了。所以做一个极小的本机 HTTP
服务，NAS 需要地址时来问它一声 —— 用户只要在微信里点一下下载按钮，剩下全自动。

## 设计

- 只读：只扫描 ffmpeg 命令行，不启动/不终止任何进程
- 只监听回环地址（默认 `127.0.0.1`），不对外暴露
- 单端点：`GET /live` → `{"ok": true, "streams": [{url, stream_id, title_hint, ...}]}`
- NAS 侧可以通过 SSH 隧道 / 局域网地址访问（绑 `0.0.0.0` 时需自行保证网络安全）

## 用法

    python -m wxlive.localagent --port 8911
    curl http://127.0.0.1:8911/live
"""

from __future__ import annotations

import argparse
import json
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from . import liveurl


def _payload() -> dict:
    try:
        streams = liveurl.find_live_streams()
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "error": f"{type(exc).__name__}: {exc}", "streams": []}

    items = []
    for s in streams:
        items.append(
            {
                "url": s.url,
                "stream_id": s.stream_id,
                "external_id": s.external_id,
                "author": s.author,
                "title": s.title,
                "title_hint": s.title_hint,
                "pid": s.pid,
                "has_signature": s.has_signature,
                "stream_key": s.stream_key,
            }
        )
    return {
        "ok": True,
        "count": len(items),
        "streams": items,
        "checked_at": time.strftime("%Y-%m-%d %H:%M:%S"),
    }


class _Handler(BaseHTTPRequestHandler):
    server_version = "wxlive-localagent/1.0"

    def _send(self, code: int, payload: dict) -> None:
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        # 允许本机浏览器 / NAS 网页跨域调用
        self.send_header("Access-Control-Allow-Origin", "*")
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self) -> None:  # noqa: N802 - BaseHTTPRequestHandler 约定
        path = self.path.split("?", 1)[0].rstrip("/") or "/"
        if path in ("/live", "/api/live", "/"):
            self._send(200, _payload())
            return
        if path == "/health":
            self._send(200, {"ok": True})
            return
        self._send(404, {"ok": False, "error": "只支持 GET /live 与 /health"})

    def do_OPTIONS(self) -> None:  # noqa: N802
        self.send_response(204)
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "GET, OPTIONS")
        self.end_headers()

    def log_message(self, fmt: str, *args) -> None:  # noqa: A003
        # 默认每条请求都打日志，扫描时太吵；只在启动/异常时由外层打印
        pass


def serve(host: str = "127.0.0.1", port: int = 8911) -> None:
    httpd = ThreadingHTTPServer((host, port), _Handler)
    print(f"本机直播地址代理已启动：http://{host}:{port}/live")
    print("NAS 侧可用 scripts/push_to_nas.py --from-local 自动取址。Ctrl+C 退出。")
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        print("\n已停止")
    finally:
        httpd.server_close()


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(
        description="本机直播地址代理（只读扫描 ffmpeg 命令行，供 NAS 取址）"
    )
    ap.add_argument("--host", default="127.0.0.1",
                    help="监听地址（默认 127.0.0.1，仅本机可访问）")
    ap.add_argument("--port", type=int, default=8911, help="监听端口（默认 8911）")
    args = ap.parse_args()
    serve(args.host, args.port)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
