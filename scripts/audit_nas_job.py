"""对一个已完成的 NAS 任务做产物验收，按「结构层 / 内容层」分别给结论。

## 为什么单独做这个

MEMORY 里那条反复出现的教训：
**结构层绿灯不能推断内容层正确。**
旧验收（行数守恒 / 失败批 0 / 三份产物齐全）全在结构层，
而实际踩过的坑（574 句繁体、幻觉句被 LLM 顺手改通顺）全在内容层。

所以这个脚本分两段输出：

1. **结构层**（文件存在、句子数、时长、rtf、llm_status、truncated）
2. **内容层**（`meta.quality` 里的 `trad_residual` / `hallucination_residual`，
   以及「音频时长 vs 转写末句时间」的完整性差值）

## 用法

    python scripts/audit_nas_job.py --job 20261128_073432_00dbef
    python scripts/audit_nas_job.py --job <id> --json-out out.json

只读：GET 任务元数据与产物，不修改任何东西。
"""

from __future__ import annotations

import argparse
import json
import sys
import urllib.request

# Windows 的 stdout 默认是 GBK，打 ✅/❌ 会抛 UnicodeEncodeError。
# 包内入口（python -m wxlive）有 __init__ 的 _force_utf8 兜底，
# 但 scripts/ 下的独立脚本不经过包，必须自己改一次。
for _s in ("stdout", "stderr"):
    _stream = getattr(sys, _s, None)
    if _stream is not None:
        try:
            if (_stream.encoding or "").lower().replace("-", "") != "utf8":
                _stream.reconfigure(encoding="utf-8", errors="replace")
        except Exception:  # noqa: BLE001
            pass

DEFAULT_BASE = "http://192.168.68.49:8910"

# 完整性判据：音频时长与转写覆盖时长允许的差值（秒）。
# 依据：VAD 尾部静音、末句对齐误差；超过 30 秒基本可判为真丢内容。
TAIL_GAP_TOLERANCE = 30.0


def _opener():
    return urllib.request.build_opener(urllib.request.ProxyHandler({}))


def fetch_job(op, base: str, job_id: str) -> dict | None:
    r = op.open(f"{base.rstrip('/')}/api/jobs", timeout=20)
    data = json.loads(r.read().decode("utf-8"))
    jobs = data if isinstance(data, list) else (data.get("jobs") or [])
    for j in jobs:
        if j.get("id") == job_id:
            return j
    return None


def fetch_artifact_meta(op, base: str, job_id: str) -> dict:
    """取产物的 meta（内容层判据所在处）。

    ## 为什么走 download/json 而不是任务详情

    实测三个候选都不给 meta：
      · `GET /api/jobs/{id}`            → 无 `meta` 字段（只有状态与路径）
      · `GET /api/jobs/{id}/transcript` → 返回 {job, md_text, segments, duration}
      · `GET /api/jobs/{id}/json`       → 404（不存在）
    真正的 meta 只写在产物 json 文件里。免 sudo 的取法是
    `GET /api/jobs/{id}/download/json`（server.py:2105），直接把文件流回来。

    内容层判据（`quality.trad_residual` / `hallucination_residual` /
    `transcript_complete`）必须从这里读 —— 只看任务列表的
    `status=completed` 属于「结构层绿灯推断内容层」，是本项目踩过的老坑。
    """
    try:
        r = op.open(f"{base.rstrip('/')}/api/jobs/{job_id}/download/json", timeout=30)
        data = json.loads(r.read().decode("utf-8"))
    except Exception:  # noqa: BLE001
        return {}
    if not isinstance(data, dict):
        return {}
    m = data.get("meta")
    return m if isinstance(m, dict) else {}


def line(tag: str, ok: bool | None, text: str) -> str:
    mark = {True: "✅", False: "❌", None: "…"}[ok]
    return f"  {mark} [{tag}] {text}"


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="NAS 任务产物验收")
    ap.add_argument("--job", required=True)
    ap.add_argument("--base", default=DEFAULT_BASE)
    ap.add_argument("--json-out", default="", help="把任务元数据另存为 JSON")
    args = ap.parse_args(argv)

    op = _opener()
    j = fetch_job(op, args.base, args.job)
    if j is None:
        print(f"找不到任务 {args.job}")
        return 2

    if args.json_out:
        with open(args.json_out, "w", encoding="utf-8") as f:
            json.dump(j, f, ensure_ascii=False, indent=2)
        print(f"任务元数据已存：{args.json_out}")

    print(f"\n===== 任务 {j.get('id')} =====")
    print(f"标题     : {j.get('title')}")
    print(f"状态     : {j.get('status')}")
    print(f"停止原因 : {j.get('stop_reason')!r} {j.get('stop_reason_text')!r}")

    audio_min = (j.get("audio_seconds") or 0) / 60.0
    print(f"音频时长 : {audio_min:.1f} 分钟")
    print(f"文件大小 : {(j.get('audio_bytes') or 0) / 1048576:.1f} MB")

    # ---------------- 结构层 ----------------
    print("\n--- 结构层 ---")
    rows = []
    rows.append(line("status", j.get("status") == "completed",
                     f"终态 status={j.get('status')!r}（应为 completed）"))
    rows.append(line("md_path", bool(j.get("md_path")),
                     f"逐字稿 md={j.get('md_path') or '（空）'}"))
    rows.append(line("json_path", bool(j.get("json_path")),
                     f"结构化 json={j.get('json_path') or '（空）'}"))
    sc = j.get("sentence_count") or 0
    rows.append(line("sentences", sc > 0, f"句子数={sc}"))
    # truncated 不能一刀切看 True/False：
    # 代码里 COMPLETE_STOP_REASONS = {user_stop, silence_timeout, stream_ended}，
    # 其余（如 stream_ended_404）刻意判为截断 —— 因为没看到静音就 404，
    # 多半是签名地址先失效，录音可能不完整。这是**设计行为**，不是 bug。
    # 所以判据是「停止原因是否属于正常收工」，而不是 truncated 的字面值。
    NORMAL_STOPS = {"user_stop", "silence_timeout", "stream_ended"}
    stop = j.get("stop_reason") or ""
    normal_stop = stop in NORMAL_STOPS
    rows.append(line(
        "truncated", normal_stop,
        f"truncated={j.get('truncated')} / stop_reason={stop!r}"
        f"（{'正常收工' if normal_stop else '非常规收工 → 可能截断，需看 tail_gap'}）"))
    llm = j.get("llm_status") or ""
    rows.append(line("llm_status", llm != "partial",
                     f"llm_status={llm!r}（不应为 'partial'）"))
    err = j.get("error") or ""
    rows.append(line("error", not err, f"error={err!r}"))
    print("\n".join(rows))

    # ---------------- 内容层 ----------------
    print("\n--- 内容层 ---")
    meta = fetch_artifact_meta(op, args.base, args.job)
    quality = meta.get("quality") or {}
    comp = meta.get("transcript_complete")

    trad = quality.get("trad_residual")
    hall = quality.get("hallucination_residual")
    qok = quality.get("ok")

    crows = []
    crows.append(line("quality.ok", qok if qok is not None else None,
                      f"meta.quality.ok={qok}"))
    crows.append(line("trad_residual", (trad == 0) if trad is not None else None,
                      f"残留繁体字数={trad}（应为 0）"))
    crows.append(line("hallucination_residual", (hall == 0) if hall is not None else None,
                      f"幻觉残留={hall}（应为 0）"))
    crows.append(line("transcript_complete", comp if comp is not None else None,
                      f"transcript_complete={comp}"))

    # 完整性：优先用 meta 自带的 tail_gap_seconds（服务端算的），
    # 退化时再用 done_seconds 自己算。两者都不存在才算「无法判定」。
    meta_gap = meta.get("tail_gap_seconds")
    if isinstance(meta_gap, (int, float)):
        crows.append(line("tail_gap", meta_gap <= TAIL_GAP_TOLERANCE,
                          f"meta.tail_gap_seconds={meta_gap:.1f}s"
                          f"（阈值 {TAIL_GAP_TOLERANCE:.0f}s）"))
    else:
        covered = (j.get("done_seconds") or 0)
        if covered > 0 and audio_min > 0:
            gap = (j.get("audio_seconds") or 0) - covered
            crows.append(line("tail_gap", gap <= TAIL_GAP_TOLERANCE,
                              f"音频 {audio_min:.1f} 分 vs 转写覆盖 {covered/60:.1f} 分 "
                              f"→ 差 {gap:.0f}s（阈值 {TAIL_GAP_TOLERANCE:.0f}s）"))
        else:
            crows.append(line("tail_gap", None, "服务端未提供 tail_gap，无法计算"))

    if not quality:
        # 注意：老产物（quality 字段引入前的镜像产出）本来就没有这一段。
        # 不能当成「检查失败」，但**也不能当成通过** —— 必须明说。
        crows.append(line("quality", None,
                          "meta 里没有 quality 字段：可能是旧镜像产物，"
                          "或该版本尚未接入 check_segments"))
    print("\n".join(crows))

    print("\n--- 结论 ---")
    struct_ok = all([j.get("status") == "completed", bool(j.get("md_path")),
                     bool(j.get("json_path")), sc > 0,
                     normal_stop, llm != "partial", not err])
    content_known = bool(quality)
    content_ok = (qok is True and trad == 0 and hall == 0) if content_known else None
    print(f"  结构层: {'通过' if struct_ok else '未通过'}")
    print(f"  内容层: {'通过' if content_ok else ('未通过' if content_ok is False else '未取到数据')}")
    if not content_known:
        print("  ⚠ 结构层绿灯**不能**推断内容层正确 —— 需人工抽查 md 前 200 行确认无幻觉/繁体")

    return 0 if struct_ok else 1


if __name__ == "__main__":
    sys.exit(main())
