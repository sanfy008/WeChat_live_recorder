"""转录产物定期复盘：从已有 JSON 产物里挖「词表该怎么更新」。

存在的理由：lexicon/xsc.json 里的 _v1纠错记录 / _待确认 是人工复盘积累的，
说明这件事一直在手工做。但手工复盘有三个结构性缺陷（2026-11-26 实测）：

  1. **只看得到 LLM 改对了的**（raw_text vs text 的差异），看不到 LLM 漏改的；
  2. **看不出错误在往哪个方向集中**（繁简、同音、还是幻觉）；
  3. **一次性做完就忘了**，没有基线，无法判断这周比上周好还是坏了。

本脚本只读已有产物，不重跑 ASR、不调 LLM，秒级完成，适合挂成定期任务。

用法:
    python scripts/lexicon_retro.py                      # 扫最近 7 天产物
    python scripts/lexicon_retro.py --days 30
    python scripts/lexicon_retro.py --all                # 全部产物
    python scripts/lexicon_retro.py --json out.json      # 机器可读输出

输出的六项体检（每项都有实测基线，见 docs/词表定期复盘可行性-2026-11-26.md）：
  1. 繁简残留     —— 终稿里 to_simplified() 能修掉却没修的句子
  2. ASR 幻觉残留 —— Gemini 的"无法访问音频"类句子进了终稿
  3. 纠错覆盖率   —— 含词表词的纠错句占比（实测 18%，说明词表不是主要贡献者）
  4. 高频替换候选 —— 单点改动的 top N，供人工筛进 corrections
  5. 数字化规范   —— "准2 / 6年级" 这类该统一成中文数字的位置
  6. 空文本/异常  —— raw_text 为空的句子（历史产物没有该字段，不能当信号）

**本脚本只报告，不自动改词表。** 原因见 README 段：
  correct_text() 是全局子串替换，"他→它" 这类单字替换在语料里
  他出现 1386 次、绝大多数是正确的。自动入库会大面积误伤。
  候选必须人工确认「是否只在该语境成立」才能进 corrections。
"""

from __future__ import annotations

import argparse
import difflib
import glob
import json
import os
import re
import sys
import time
from collections import Counter
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from wxlive.lexicon import to_simplified  # noqa: E402

TRAD_MARK = set("話們級來這個會現後說韓體麼學國時間東進階師聯繫價藝術動員讀寫買賣"
                "點線樣區問間還進過發現當從經濟讓給帶車長門見覺聽講億萬歲歷經")

HALLU_PAT = re.compile(
    r"\[attachment|audio/wav|无法访问|无法完成转写|未检测到音频|请重新上传|"
    r"我没有可用的音频|As an AI|I cannot", re.I)

# "准2线 / 6年级 / 3单元" 这类该统一成中文数字的位置（B 级判据：仅提示）
NUM_UNIT_PAT = re.compile(r"(?<![A-Za-z0-9])([0-9])(?=\s*(?:年级|单元|月|班|线|中|小|号))")


def load_corpus(days: int | None, use_all: bool) -> list[tuple[str, dict]]:
    root = Path(__file__).resolve().parent.parent / "output" / "transcripts"
    pats = [str(root / "*" / "*.json")]
    # 兼容真实项目目录（产物可能不在仓库内）
    pats.append("D:/File/01_Vibe_Coding/14-WeChat_live/output/transcripts/*/*.json")
    files: list[str] = []
    for pat in pats:
        files.extend(glob.glob(pat))
    files = sorted(set(files))
    if not use_all and days:
        cut = time.time() - days * 86400
        files = [f for f in files if os.path.getmtime(f) >= cut]
    out = []
    for f in files:
        try:
            out.append((f, json.loads(Path(f).read_text(encoding="utf-8"))))
        except Exception:
            continue
    return out


def seg_texts(doc: dict):
    for i, s in enumerate(doc.get("segments") or []):
        yield i, s


def report(docs: list[tuple[str, dict]], top_n: int) -> dict:
    stats = {
        "products": len(docs),
        "segments": 0,
        "corrected": 0,
        "trad_residual": 0,
        "hallucination": 0,
        "num_candidates": 0,
        "lexicon_hit_corrections": 0,
        "empty_raw": 0,
    }
    trad_items: list[tuple[str, str, str]] = []      # (file, raw, text)
    hallu_items: list[tuple[str, str]] = []
    num_items: list[tuple[str, str]] = []
    one_point: Counter = Counter()

    # 词表命中判据用的词（只有 >=2 字才有意义）
    try:
        lex = json.loads(
            (Path(__file__).resolve().parent.parent / "lexicon" / "xsc.json")
            .read_text(encoding="utf-8"))
        vocab = [v for v in lex.get("schools", []) + lex.get("terms", []) if len(v) >= 2]
        vocab += [k for k in (lex.get("codes") or {}) if len(k) >= 2 and not k.startswith("_")]
    except Exception:
        vocab = []

    def has_vocab(t: str) -> bool:
        return any(v in t for v in vocab)

    for f, doc in docs:
        base = os.path.basename(f)
        for i, s in seg_texts(doc):
            stats["segments"] += 1
            raw = s.get("raw_text")
            txt = s.get("text") or ""
            if raw is None:
                stats["empty_raw"] += 1

            # 1. 繁简残留：终稿里还有繁体，且 to_simplified 能修掉
            if any(c in TRAD_MARK for c in txt):
                if not any(c in TRAD_MARK for c in to_simplified(txt)):
                    stats["trad_residual"] += 1
                    if len(trad_items) < 6:
                        trad_items.append((base, raw or "", txt))

            # 2. 幻觉残留
            if HALLU_PAT.search(txt):
                stats["hallucination"] += 1
                if len(hallu_items) < 6:
                    hallu_items.append((base, txt))

            # 3. 数字化规范候选
            if NUM_UNIT_PAT.search(txt):
                stats["num_candidates"] += 1
                if len(num_items) < 5:
                    num_items.append((base, txt))

            # 4. 纠错统计
            if raw and raw.strip() != txt.strip():
                stats["corrected"] += 1
                if has_vocab(txt):
                    stats["lexicon_hit_corrections"] += 1
                sm = difflib.SequenceMatcher(None, raw.strip(), txt.strip())
                for tag, i1, i2, j1, j2 in sm.get_opcodes():
                    if tag == "equal":
                        continue
                    a, b = raw.strip()[i1:i2], txt.strip()[j1:j2]
                    if abs(len(a) - len(b)) <= 1 and 0 < len(a) <= 6 and 0 < len(b) <= 6:
                        one_point[(a, b)] += 1

    total = max(1, stats["segments"])
    corr = max(1, stats["corrected"])
    stats["corrected_rate"] = stats["corrected"] / total
    stats["lexicon_coverage"] = stats["lexicon_hit_corrections"] / corr

    # ---- 打印 ----
    print("=" * 76)
    print("转录词表定期复盘（只读，成本 ≈ 0）")
    print("=" * 76)
    print(f"产物 {stats['products']} 份 / 句子 {stats['segments']} / "
          f"被改过的句子 {stats['corrected']} ({stats['corrected_rate']:.1%})")
    print()

    print("-" * 76)
    print("[1] 繁简残留")
    print("-" * 76)
    if stats["trad_residual"]:
        print(f"  ❌ {stats['trad_residual']} 句终稿仍是繁体，且 to_simplified() 本可修掉")
        print("     这是确定性缺陷（不是模型波动），说明该场走了没调 to_simplified 的路径。")
        for f, r, t in trad_items:
            print(f"     {f[:34]:34s} {t[:60]}")
    else:
        print("  ✅ 无残留")
    print()

    print("-" * 76)
    print("[2] ASR 幻觉残留")
    print("-" * 76)
    if stats["hallucination"]:
        print(f"  ❌ {stats['hallucination']} 句「我无法访问音频」类文本进了终稿")
        print("     这类句子会被 LLM 纠错顺手改通顺，读起来像正常正文 → 必须按模式扫。")
        for f, t in hallu_items:
            print(f"     {f[:34]:34s} {t[:60]}")
    else:
        print("  ✅ 无残留")
    print()

    print("-" * 76)
    print("[3] 词表贡献度")
    print("-" * 76)
    print(f"  含词表词的纠错句: {stats['lexicon_hit_corrections']} / {stats['corrected']} "
          f"= {stats['lexicon_coverage']:.0%}")
    print("  → 数字越低，说明纠错主要靠 LLM 常识判断，扩词表的边际收益越小。")
    print()

    print("-" * 76)
    print(f"[4] 高频单点替换候选 top {top_n}（⚠️ 仅供人工筛选，勿自动入库）")
    print("-" * 76)
    for (a, b), n in one_point.most_common(top_n):
        print(f"  {n:4d}x  {a!r} → {b!r}")
    print()
    print("  注意：correct_text() 是全局子串替换，单字替换风险极高。")
    print("        实测「他」在语料里出现 1386 次且绝大多数是正确的，")
    print("        把它自动加进 corrections 会大面积误伤。候选必须人工确认")
    print("        「是否只在该语境成立」后才可入库。")
    print()

    print("-" * 76)
    print("[5] 数字化规范候选（B 级，仅提示）")
    print("-" * 76)
    print(f"  {stats['num_candidates']} 句含「阿拉伯数字+中文量词」")
    for f, t in num_items:
        m = NUM_UNIT_PAT.search(t)
        print(f"     {f[:30]:30s} …{t[max(0,m.start()-14):m.end()+8]}…")
    print()

    print("-" * 76)
    print("[6] 数据可用性")
    print("-" * 76)
    print(f"  raw_text 为空的句子: {stats['empty_raw']} / {stats['segments']}")
    print("  历史产物没有 raw_text 字段，这些句子无法参与纠错复盘（不是错误信号）。")
    print()
    return stats


def main() -> None:
    p = argparse.ArgumentParser(description="转录词表定期复盘（只读）")
    p.add_argument("--days", type=int, default=7, help="窗口天数，默认 7")
    p.add_argument("--all", action="store_true", help="忽略窗口，扫全部产物")
    p.add_argument("--top", type=int, default=20, help="替换候选条数")
    p.add_argument("--json", help="把统计写到该路径")
    args = p.parse_args()

    docs = load_corpus(args.days, args.all)
    if not docs:
        print("没有找到产物。检查 output/transcripts 或 --days 窗口。")
        return
    stats = report(docs, args.top)
    if args.json:
        Path(args.json).write_text(
            json.dumps(stats, ensure_ascii=False, indent=2), encoding="utf-8")
        print(f"统计已写入 {args.json}")


if __name__ == "__main__":
    main()
