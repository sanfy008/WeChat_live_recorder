"""盯住 NAS 上的一个任务，直到它进入终态，并把关键指标打成事件流。

## 为什么需要这个脚本

「直播在 NAS 上录着，等它录完再看结果」这件事，人肉盯不可行：
一场真实直播可能 1-3 小时，而真正要看的是**终态那几个字段**
（`transcript_complete` / `meta.quality` / `llm_status` / `truncated`）。

手工反复 curl 有三个坑：
1. 容易在 `audio_bytes` 两次采样恰好相同的瞬间误判「卡住了」——
   实测 6 秒采样就会撞上（ffmpeg 写盘是 256KB 一批，间隔不定）。
   所以卡死判据必须是**连续多分钟**无增长，不是两次采样相同。
2. 转写阶段（queued/converting/transcribing/postprocessing）耗时远长于录制，
   需要看 `rtf` 与 `progress_pct` 判断是"在跑"还是"卡住"。
3. 终态字段多且分散，事后再去翻容易漏。

## 用法

    python scripts/watch_nas_job.py --job 20261128_073432_00dbef
    python scripts/watch_nas_job.py --job <id> --base http://192.168.68.49:8910

只读：只 GET `/api/jobs`，不 POST、不建任务、不改任何状态。
"""

from __future__ import annotations

import argparse
import json
import sys
import time
import urllib.request

DEFAULT_BASE = "http://192.168.68.49:8910"
TERMINAL = {"completed", "failed", "error", "cancelled"}


def _opener():
    # 显式空 ProxyHandler：局域网地址不能被系统代理（10808）接管，
    # 否则会报 000/超时，看着像 NAS 挂了（MEMORY 记过的坑）。
    return urllib.request.build_opener(urllib.request.ProxyHandler({}))


def fetch_job(op, base: str, job_id: str) -> dict | None:
    r = op.open(f"{base.rstrip('/')}/api/jobs", timeout=15)
    data = json.loads(r.read().decode("utf-8"))
    jobs = data if isinstance(data, list) else (data.get("jobs") or [])
    for j in jobs:
        if j.get("id") == job_id:
            return j
    return None


def log(msg: str) -> None:
    print(f"[{time.strftime('%H:%M:%S')}] {msg}", flush=True)


def summarize(j: dict) -> None:
    sec = (j.get("audio_seconds") or 0) / 60.0
    log(f"  时长      : {sec:.1f} 分钟")
    log(f"  句子/纠错 : {j.get('sentence_count')} / {j.get('changed_count')}")
    log(f"  rtf       : {j.get('rtf')}")
    log(f"  llm_status: {j.get('llm_status')!r}")
    log(f"  truncated : {j.get('truncated')}")
    log(f"  停止原因  : {j.get('stop_reason')!r} {j.get('stop_reason_text')!r}")
    log(f"  error     : {j.get('error')!r}")
    log(f"  md_path   : {j.get('md_path')}")
    log(f"  json_path : {j.get('json_path')}")


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="盯住 NAS 任务直到终态")
    ap.add_argument("--job", required=True, help="任务 ID")
    ap.add_argument("--base", default=DEFAULT_BASE, help="NAS 地址")
    ap.add_argument("--interval", type=int, default=60, help="录制阶段轮询秒数")
    ap.add_argument("--stall-min", type=int, default=6,
                    help="连续多少分钟无增长才判定卡住（默认 6）")
    ap.add_argument("--max-hours", type=float, default=4.0, help="最长盯多久")
    args = ap.parse_args(argv)

    op = _opener()
    last_status = None
    last_bytes = None
    stall = 0
    deadline = time.time() + args.max_hours * 3600

    while time.time() < deadline:
        try:
            j = fetch_job(op, args.base, args.job)
        except Exception as exc:  # noqa: BLE001
            log(f"查询失败：{type(exc).__name__}: {exc}（继续重试）")
            time.sleep(30)
            continue

        if j is None:
            log("任务已不在列表（可能被清理）")
            return 2

        st = j.get("status")
        b = j.get("audio_bytes") or 0
        sec = (j.get("audio_seconds") or 0) / 60.0

        if st != last_status:
            log(f"状态 {last_status} -> {st} | 已录 {sec:.1f} 分钟 / {b / 1048576:.1f} MB")
            last_status = st

        if st == "recording":
            if last_bytes is not None and b == last_bytes:
                stall += 1
                if stall >= args.stall_min:
                    log(f"⚠ 疑似卡住：连续 {stall} 分钟无增长（{b} 字节）")
            else:
                stall = 0
            last_bytes = b
            time.sleep(args.interval)
            continue

        if st in TERMINAL:
            log(f"=== 终态 {st} ===")
            summarize(j)
            return 0 if st == "completed" else 1

        # queued / converting / transcribing / postprocessing
        log(f"  …{st} pct={j.get('progress_pct')}% "
            f"rtf={j.get('rtf')} llm={j.get('llm_status')!r}")
        time.sleep(30)

    log("超过最长观察时间，退出")
    return 3


if __name__ == "__main__":
    sys.exit(main())
