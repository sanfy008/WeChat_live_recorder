"""转写后处理：用 LLM 做纠错、暗语展开、要点整理。

为什么需要单独一步：
  ASR 只负责"听清"，不负责"读懂"。实测即使 Gemini 也会把「白省」写成「白水」、
  把「16南」写成「16难」。用文本 LLM 拿着领域词表再校对一遍，能明显改善。

三个可独立开关的动作：
  1. fix    —— 同音字/专有名词纠错（最轻量，强烈建议开）
  2. codes  —— 暗语展开成正式校名，便于检索
  3. digest —— 生成结构化要点（话题、学校评价、关键结论）

设计原则：
  - 分批处理，单批不超过 N 行，避免超长上下文导致质量下降
  - 任何一步失败都保留原文，不能让后处理把数据搞丢
"""

from __future__ import annotations

import concurrent.futures
import json
import time
import urllib.error
import urllib.request
from typing import Callable

DEFAULT_ENDPOINT = "http://127.0.0.1:3425/v1/chat/completions"
DEFAULT_MODEL = "workbuddy/deepseek-v4.1-flash"


def _call(model: str, prompt: str, endpoint: str, api_key: str,
          max_tokens: int = 8000, timeout: int = 240,
          reasoning_effort: str | None = "minimal") -> str:
    payload = {
        "model": model,
        "messages": [{"role": "user", "content": prompt}],
        "max_tokens": max_tokens,
    }
    # 关键：不压低思考强度会把 token 全烧在 reasoning 上导致空输出
    if reasoning_effort:
        payload["reasoning_effort"] = reasoning_effort

    req = urllib.request.Request(
        endpoint, data=json.dumps(payload).encode(),
        headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=timeout) as r:  # noqa: S310
        d = json.loads(r.read().decode())
    return (d["choices"][0]["message"].get("content") or "").strip()


def fix_errors(lines: list[str], model: str = DEFAULT_MODEL,
               endpoint: str = DEFAULT_ENDPOINT, api_key: str = "magpie-alma",
               vocabulary: str = "", batch_size: int = 40,
               progress: Callable[[int, int], None] | None = None,
               workers: int = 6) -> list[str]:
    """按领域词表纠正同音字。失败的分批保留原文。

    workers > 1 时用线程池并发请求网关；结果按批下标拼回，顺序与输入一致。
    实测 27 批串行约 10 分钟，6 路并发约 107 秒。
    """
    if not lines:
        return lines

    total_batches = (len(lines) + batch_size - 1) // batch_size
    batches = [lines[i : i + batch_size] for i in range(0, len(lines), batch_size)]

    def process(bi: int, batch: list[str]) -> list[str]:
        numbered = "\n".join(f"{i+1}. {t}" for i, t in enumerate(batch))
        prompt = (
            "你是中文 ASR 后处理专家。下面是一段升学规划直播的语音识别结果，存在同音字错误。\n"
            f"{('【领域词表】' + chr(10) + vocabulary + chr(10)) if vocabulary else ''}"
            "要求：\n"
            "- 逐行修正同音字错误，行数必须与输入完全一致\n"
            "- 严禁增加、删除、合并任何一行\n"
            "- 只改明显听错的地方，不确定就保持原样\n"
            "- 输出格式：每行只输出修正后的文本，不要序号、不要解释\n\n"
            f"待修正：\n{numbered}"
        )
        try:
            res = _call(model, prompt, endpoint, api_key)
            fixed = [x.strip() for x in res.split("\n") if x.strip()]
            # 行数不一致时不要用这批结果，避免错位
            if len(fixed) == len(batch):
                return fixed
            print(f"[post] 纠错批次 {bi+1} 行数不匹配（{len(fixed)}≠{len(batch)}），保留原文")
            return batch
        except Exception as e:  # noqa: BLE001
            print(f"[post] 纠错批次 {bi+1} 失败，保留原文: {str(e)[:120]}")
            return batch

    if workers <= 1:
        out: list[str] = []
        for bi, batch in enumerate(batches):
            out.extend(process(bi, batch))
            if progress:
                progress(bi + 1, total_batches)
        return out

    results: list[list[str] | None] = [None] * total_batches
    done = 0
    with concurrent.futures.ThreadPoolExecutor(max_workers=workers) as executor:
        futures = {executor.submit(process, bi, b): bi
                   for bi, b in enumerate(batches)}
        for fut in concurrent.futures.as_completed(futures):
            bi = futures[fut]
            try:
                results[bi] = fut.result()
            except Exception as e:  # noqa: BLE001
                # process 内部已兜底，这里只防意外；该批仍保留原文
                print(f"[post] 纠错批次 {bi+1} 异常，保留原文: {str(e)[:120]}")
                results[bi] = batches[bi]
            done += 1
            # 并发时按完成数递增，不保证批次顺序
            if progress:
                progress(done, total_batches)

    final: list[str] = []
    for bi, batch in enumerate(results):
        if batch is None:  # 理论上不会发生；兜底保留原文，绝不让行数变少
            print(f"[post] 纠错批次 {bi+1} 无结果，保留原文")
            batch = batches[bi]
        final.extend(batch)
    return final


def build_digest(segments: list[dict], model: str = DEFAULT_MODEL,
                 endpoint: str = DEFAULT_ENDPOINT, api_key: str = "magpie-alma",
                 title: str = "",
                 group_seconds: float = 60.0) -> str:
    """生成结构化要点。长文本超限时自动按段摘要再归并。

    ## 为什么按段落喂而不是逐句喂

    实测同一份 81 分钟转录（863 句）：

    | 形态 | 输入字符 | 是否触发 30000 分块 |
    |---|---|---|
    | 逐句 + 句级时间戳 | 34,205 | **是**（27.8% 是时间戳） |
    | 段落聚合 + 段级时间 | 26,065 | 否 |

    降 23.8%，而且**不再触发分块**——分块意味着全片被切成几段分别摘要再归并，
    全局视野丢失，这是实实在在的质量损失，不只是省钱。

    另外句级时间戳本来是"按字数摊"的估算值（实测中位偏差 19.3s），
    拿 27.8% 的 token 去喂一个假的精度，本身也不划算。
    段落边界取的是聚合后的真实块起点，比句级估算可信。
    """
    if not segments:
        return ""

    from . import render as _render

    groups = _render.group_segments(segments, max_seconds=group_seconds)
    if not groups:
        return ""

    text = "\n".join(f"[{_span(g['start'], g['end'])}] {g['text']}" for g in groups)

    # 上下文保护：太长就先分块摘要
    if len(text) > 30000:
        chunks, cur = [], []
        size = 0
        for g in groups:
            line = f"[{_span(g['start'], g['end'])}] {g['text']}"
            if size + len(line) > 12000 and cur:
                chunks.append("\n".join(cur))
                cur, size = [], 0
            cur.append(line)
            size += len(line)
        if cur:
            chunks.append("\n".join(cur))

        partials = []
        for i, c in enumerate(chunks):
            p = (f"下面是直播的部分记录（第{i+1}/{len(chunks)}部分），请提炼要点，"
                 f"保留具体学校名、分数、建议，不要泛泛而谈：\n\n{c}")
            try:
                partials.append(_call(model, p, endpoint, api_key, max_tokens=4000))
            except Exception as e:  # noqa: BLE001
                partials.append(f"(第{i+1}部分摘要失败: {str(e)[:60]})")
        text = "\n\n".join(f"### 片段{i+1}\n{p}" for i, p in enumerate(partials))
        text = text[:60000]

    prompt = (
        f"下面是一场升学规划直播的完整记录{f'（{title}）' if title else ''}。\n"
        "请输出结构化整理，用 Markdown：\n\n"
        "## 一句话概括\n"
        "## 主要话题（每个话题列出讨论的具体内容）\n"
        "## 提到的学校与评价（逐条列：学校名 — 评价/分数/建议）\n"
        "## 可执行建议（家长该怎么做）\n"
        "## 需要存疑的点（说错或没讲清的地方）\n\n"
        "要求：保留具体数字和校名，不要编造，不要写客套话。\n\n"
        f"{text}"
    )
    try:
        return _call(model, prompt, endpoint, api_key, max_tokens=8000, timeout=300)
    except Exception as e:  # noqa: BLE001
        return f"(要点整理失败: {str(e)[:150]})"


def _ts(seconds: float) -> str:
    s = max(0, int(seconds))
    h, rem = divmod(s, 3600)
    m, sec = divmod(rem, 60)
    return f"{h:02d}:{m:02d}:{sec:02d}"


def _span(start: float, end: float) -> str:
    """段落时间范围，mm:ss（超过 1 小时用 h:mm:ss）。

    与 render._fmt_span 保持同样的口径：段级时间来自聚合后的真实块边界，
    比句级"按字数摊"的估算值可信，所以不带秒级伪精度。
    """

    def one(sec: float) -> str:
        sec = max(0, int(sec))
        h, rem = divmod(sec, 3600)
        m, s = divmod(rem, 60)
        return f"{h}:{m:02d}:{s:02d}" if h else f"{m:02d}:{s:02d}"

    return f"{one(start)}-{one(end)}"
