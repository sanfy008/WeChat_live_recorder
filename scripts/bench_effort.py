"""思考强度档位 A/B 基准测试（可复用）。

存在的理由：2026-11-26 做档位复测时，脚本写在临时目录 .probe/ 里，用完就删了。
而这类判断（"某个档位到底值不值"）会反复出现，所以固化成脚本。

用法:
    python scripts/bench_effort.py fix    --src output/transcripts/xxx/xxx.json
    python scripts/bench_effort.py digest --src output/transcripts/xxx/xxx.json
    python scripts/bench_effort.py full   --src output/transcripts/xxx/xxx.json
    python scripts/bench_effort.py fix    --src xxx.json --efforts low,high --reps 2

三个必须保留的设计（都是踩过坑才加的）:
  1. 每档至少 2 轮 —— 否则无法与 A/A 噪声对比，会得出错误结论。
  2. 必须报 A/A 噪声基线 —— 同档位重复跑的差异，是判断"跨档位差异是否可辨"的标尺。
  3. 要点判据用事实型指标（章节完整 / 内容点命中 / 数字保真），不用文本相似度。
     要点自身重复只有 0.19-0.25，文本相似度的噪声会淹没信号。
"""

from __future__ import annotations

import argparse
import difflib
import itertools
import json
import os
import re
import statistics
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from wxlive import postprocess as pp  # noqa: E402

DEFAULT_ENDPOINT = "http://127.0.0.1:3425/v1/chat/completions"

# 要点章节完整度的判据
SECTIONS = ["一句话概括", "主要话题", "提到的学校", "可执行建议", "需要存疑的点"]


def load_segments(src: str) -> list[dict]:
    d = json.loads(Path(src).read_text(encoding="utf-8"))
    segs = d.get("segments") or []
    if not segs:
        raise SystemExit(f"{src} 里没有 segments")
    return segs


def raw_lines(segs: list[dict]) -> list[str]:
    """取 ASR 原始文本。优先 raw_text（被纠错前的原文），否则用 text。"""
    return [(s.get("raw_text") or s["text"]) for s in segs]


def sim(a: str, b: str) -> float:
    return difflib.SequenceMatcher(None, a, b).ratio()


def changed_count(src: list[str], out: list[str]) -> int:
    return sum(1 for a, b in zip(src, out) if a.strip() != b.strip())


def factual_numbers(text: str) -> list[str]:
    """抽事实型数字，剔除行首列表序号。

    这一步是必须的：不剔除的话 "9." "12." 这类编号会被当成事实数字，
    得出"某档位编造了数字"的假结论（2026-11-26 实际栽过）。
    """
    out: list[str] = []
    for line in text.split("\n"):
        stripped = re.sub(r"^\s*(?:[-*+]\s*)?\d+[.)]\s*", "", line)
        stripped = re.sub(r"^\s*#{1,6}\s*\d+[.)]?\s*", "", stripped)
        out.extend(re.findall(r"\d+", stripped))
    return out


def report_aa_and_ab(runs: dict, textfn, label: str) -> None:
    """打印 A/A 噪声基线与跨档位差异。这是整个基准测试的核心输出。

    关键：必须在**同一条语段内**比较。把 s0/s1/s2 三个不同语段混在一起算相似度
    只会得到 0.04 这种毫无意义的数字 —— 那是不同内容在比，不是同一内容的重复。
    （2026-11-26 第一版脚本就犯了这个错，把 A/A 基线算成 0.03，结论全废。）
    """
    efforts = sorted({k[1] for k in runs})
    groups = sorted({k[0] for k in runs})  # 语段/分组

    print()
    print("=" * 78)
    print(f"{label} · A/A 噪声基线（同一分组内、同档位重复跑）")
    print("=" * 78)
    # aa[eff] = 该档位在所有分组内的重复相似度（合并）
    aa: dict[str, list[float]] = {}
    for grp in groups:
        for eff in efforts:
            g = [runs[k] for k in runs if k[0] == grp and k[1] == eff]
            if len(g) < 2:
                continue
            s = [sim(textfn(x), textfn(y)) for x, y in itertools.combinations(g, 2)]
            aa.setdefault(eff, []).extend(s)
            print(f"  {grp:6s} {eff:6s} n={len(g)} 相似度 "
                  f"{[round(x, 4) for x in s]} min={min(s):.4f}")
    print()
    for eff in efforts:
        if eff not in aa:
            print(f"  {eff:6s} 每组都只跑了 1 轮，无法算 A/A（至少 2 轮）")
            continue
        s = aa[eff]
        print(f"  {eff:6s} 合并基线 n={len(s)} 均值={statistics.mean(s):.4f} "
              f"min={min(s):.4f} max={max(s):.4f}")

    print()
    print("=" * 78)
    print(f"{label} · 跨档位差异（必须与上面的 A/A 对比才有意义）")
    print("=" * 78)
    base = "low" if "low" in aa else (efforts[0] if efforts and efforts[0] in aa else None)
    if base is None:
        print("  没有可作基准的档位（需基准档至少 2 轮）")
        return
    base_mean = statistics.mean(aa[base])
    for eff in efforts:
        if eff == base:
            continue
        # 逐组比较，再合并 —— 绝不跨组配对
        s: list[float] = []
        for grp in groups:
            A = [runs[k] for k in runs if k[0] == grp and k[1] == eff]
            B = [runs[k] for k in runs if k[0] == grp and k[1] == base]
            s.extend(sim(textfn(x), textfn(y)) for x in A for y in B)
        if not s:
            continue
        m = statistics.mean(s)
        if m < base_mean - 0.01:
            verdict = "差异大于噪声 ✅ 值得进一步看"
        else:
            verdict = "差异在噪声内 ❌ 该档位无优势"
        print(f"  {eff:6s} vs {base}: n={len(s)} 均值={m:.4f} "
              f"| {base} 自身基线={base_mean:.4f}  → {verdict}")


def cmd_fix(segs, efforts, reps, args) -> None:
    lines = raw_lines(segs)
    spans = [(200, 280), (600, 680), (1000, 1080)]
    spans = [s for s in spans if s[1] <= len(lines)] or [(0, min(80, len(lines)))]
    runs: dict = {}
    times: list[tuple[str, float]] = []
    print(f"输入 {len(lines)} 行，{len(spans)} 个语段，每档 {reps} 轮 → "
          f"共 {len(spans) * len(efforts) * reps} 次调用")
    for si, (a, b) in enumerate(spans):
        src = lines[a:b]
        for eff in efforts:
            for rep in range(reps):
                st: dict = {}
                out = pp.fix_errors(
                    src, model=args.model, endpoint=args.endpoint,
                    api_key=args.api_key, batch_size=args.batch_size, workers=1,
                    retry_delays=(2.0,), reasoning_effort=eff, out_stats=st)
                runs[(f"s{si}", eff, rep)] = out
                secs = st.get("elapsed_seconds", 0.0)
                times.append((eff, secs))
                ok = "OK" if len(out) == len(src) else f"BAD(应{len(src)})"
                print(f"  s{si} {eff:6s} rep{rep} lines={len(out)}/{len(src)} {ok} "
                      f"failed={len(st['failed_batches'])} {secs:6.1f}s "
                      f"改动={changed_count(src, out)}", flush=True)

    print()
    print("=" * 78)
    print("耗时（中位，秒）")
    print("=" * 78)
    med: dict[str, float] = {}
    for eff in efforts:
        v = [t for e, t in times if e == eff]
        if v:
            med[eff] = statistics.median(v)
            print(f"  {eff:6s} n={len(v)} 中位={med[eff]:.1f}s "
                  f"{[round(x, 1) for x in v]}")
    if "low" in med:
        for eff, v in med.items():
            if eff != "low":
                print(f"  → {eff} 相对 low: {v / med['low']:.2f}x")

    print()
    print("=" * 78)
    print("改动强度稳定性（纠错质量的真正判据）")
    print("=" * 78)
    print("  为什么需要这一节：相似度只能说明「两档输出像不像」，")
    print("  说明不了「哪档更靠谱」。实测 `none` 的相似度与 low 差异在噪声内，")
    print("  但它的改动行数在 2~48 之间乱跳 —— 有时几乎不纠错，有时过量改写。")
    print("  稳定性只能看同一档位内改动行数的极差。")
    print()
    changed_by: dict[str, dict[str, list[int]]] = {}
    for si, (a, b) in enumerate(spans):
        src = lines[a:b]
        for eff in efforts:
            v = [changed_count(src, runs[(f"s{si}", eff, rep)])
                 for rep in range(reps) if (f"s{si}", eff, rep) in runs]
            if v:
                changed_by.setdefault(eff, {})[f"s{si}"] = v
    for eff in efforts:
        if eff not in changed_by:
            continue
        per = changed_by[eff]
        allv = [x for v in per.values() for x in v]
        spreads = [max(v) - min(v) for v in per.values() if len(v) > 1]
        desc = " ".join(f"{g}={v}" for g, v in per.items())
        if spreads:
            print(f"  {eff:6s} {desc}  组内极差={spreads} "
                  f"最大极差={max(spreads)}")
        else:
            print(f"  {eff:6s} {desc}  (每组只 1 轮，无法看稳定性)")

    report_aa_and_ab(runs, lambda x: "\n".join(x), "纠错")


def cmd_digest(segs, efforts, reps, args) -> None:
    spans = {"d200": (200, 420), "d600": (600, 820)}
    spans = {k: v for k, v in spans.items() if v[1] <= len(segs)} \
        or {"d0": (0, min(200, len(segs)))}
    runs: dict = {}
    for tag, (a, b) in spans.items():
        sub = segs[a:b]
        for eff in efforts:
            for rep in range(reps):
                st: dict = {}
                out = pp.build_digest(
                    sub, model=args.model, endpoint=args.endpoint,
                    api_key=args.api_key, title=args.title, group_seconds=60.0,
                    reasoning_effort=eff, out_stats=st)
                runs[(tag, eff, rep)] = out
                print(f"  {tag} {eff:6s} rep{rep} ok={st.get('ok')} "
                      f"{st.get('seconds')}s len={len(out)} "
                      f"trunc={st.get('truncated')}", flush=True)
    report_aa_and_ab(runs, lambda x: x, "要点")
    print()
    print("  ⚠️ 要点自身重复相似度通常只有 0.2-0.4，上面的跨档位对比")
    print("     很可能全部落在噪声内。这是正常现象 —— 判断要点质量必须用")
    print("     事实型判据（见 full 模式的内容点命中 / 数字保真），")
    print("     文本相似度在这里无效。")


def cmd_full(segs, efforts, reps, args) -> None:
    runs: dict = {}
    for eff in efforts:
        for rep in range(reps):
            st: dict = {}
            out = pp.build_digest(
                segs, model=args.model, endpoint=args.endpoint,
                api_key=args.api_key, title=args.title, group_seconds=60.0,
                reasoning_effort=eff, out_stats=st)
            runs[("all", eff, rep)] = out
            print(f"  full {eff:6s} rep{rep} ok={st.get('ok')} "
                  f"{st.get('seconds')}s len={len(out)} "
                  f"chunks_failed={len(st.get('failed_chunks') or [])} "
                  f"trunc={st.get('truncated')}", flush=True)

    print()
    print("=" * 78)
    print("结构完整度与事实型数字保真")
    print("=" * 78)
    srcnums = set(re.findall(r"\d+", "".join(s["text"] for s in segs)))
    for (tag, eff, rep), dg in sorted(runs.items()):
        sec = sum(1 for s in SECTIONS if s in dg)
        facts = factual_numbers(dg)
        bad = [n for n in facts if n not in srcnums]
        rate = (len(facts) - len(bad)) / len(facts) if facts else 0.0
        print(f"  {eff:6s} rep{rep} 章节={sec}/5 字数={len(dg):5d} "
              f"事实数字={len(facts):4d} 源里没有={len(bad):2d} "
              f"保真={rate:.2%} {bad[:8]}")

    report_aa_and_ab(runs, lambda x: x, "整场要点")


def main() -> None:
    p = argparse.ArgumentParser(description="思考强度档位 A/B 基准")
    p.add_argument("mode", choices=["fix", "digest", "full"])
    p.add_argument("--src", required=True, help="转写 json 路径")
    p.add_argument("--efforts", default="low,high,none",
                   help="逗号分隔，默认 low,high,none")
    p.add_argument("--reps", type=int, default=2,
                   help="每档轮数；<2 无法算 A/A 基线")
    p.add_argument("--model", default="workbuddy/deepseek-v4.1-flash")
    p.add_argument("--endpoint",
                   default=os.environ.get("WXLIVE_LLM_ENDPOINT") or DEFAULT_ENDPOINT)
    p.add_argument("--api-key",
                   default=os.environ.get("WXLIVE_LLM_API_KEY") or "magpie-alma")
    p.add_argument("--batch-size", type=int, default=40)
    p.add_argument("--title", default="")
    args = p.parse_args()

    if args.reps < 2:
        print("⚠️  reps<2 时无法计算 A/A 噪声基线 —— 跨档位结论不可信。\n")

    segs = load_segments(args.src)
    efforts = [e.strip() for e in args.efforts.split(",") if e.strip()]
    {"fix": cmd_fix, "digest": cmd_digest, "full": cmd_full}[args.mode](
        segs, efforts, args.reps, args)


if __name__ == "__main__":
    main()
