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
               progress: Callable[[int, int], None] | None = None) -> list[str]:
    """按领域词表纠正同音字。失败的分批保留原文。"""
    if not lines:
        return lines

    out: list[str] = []
    total_batches = (len(lines) + batch_size - 1) // batch_size

    for bi, start in enumerate(range(0, len(lines), batch_size)):
        batch = lines[start : start + batch_size]
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
                out.extend(fixed)
            else:
                print(f"[post] 纠错批次 {bi+1} 行数不匹配（{len(fixed)}≠{len(batch)}），保留原文")
                out.extend(batch)
        except Exception as e:  # noqa: BLE001
            print(f"[post] 纠错批次 {bi+1} 失败，保留原文: {str(e)[:120]}")
            out.extend(batch)

        if progress:
            progress(bi + 1, total_batches)

    return out


def build_digest(segments: list[dict], model: str = DEFAULT_MODEL,
                 endpoint: str = DEFAULT_ENDPOINT, api_key: str = "magpie-alma",
                 title: str = "") -> str:
    """生成结构化要点。长文本超限时自动按段摘要再归并。"""
    if not segments:
        return ""

    text = "\n".join(f"[{_ts(s['start'])}] {s['text']}" for s in segments)

    # 上下文保护：太长就先分块摘要
    if len(text) > 30000:
        chunks, cur = [], []
        size = 0
        for s in segments:
            line = f"[{_ts(s['start'])}] {s['text']}"
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
