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
import re
import time
import urllib.error
import urllib.request
from typing import Callable

from .config import DEFAULT_REASONING_EFFORT, DEFAULT_TEXT_MODEL, validate_reasoning_effort

DEFAULT_ENDPOINT = "http://127.0.0.1:3425/v1/chat/completions"
# 单一真相在 config.py（TextConfig 的默认值也取同一个常量）。
DEFAULT_MODEL = DEFAULT_TEXT_MODEL
DEFAULT_FIX_WORKERS = 12

# 纠错批次的初始 token 预算与上限。
# 网关把 reasoning_tokens 计入 max_tokens，high/max 档下 8000 很容易被
# 思考吃掉导致 finish_reason="length"；实测重试同样预算仍会截断，
# 所以在重试时逐次放大预算，直到上限。
FIX_MAX_TOKENS = 8000
FIX_MAX_TOKENS_CAP = 32000

# 模型偶尔无视“不要序号”的指令，返回 `1. xxx` / `2、xxx` / `3) xxx`。
# 行数校验能过，但序号会进正文，必须剥离。
# `(?!\d)` 防止误伤小数开头的内容（如“1.5万人报名”不能被改成“5万人报名”）。
_NUMBER_PREFIX = re.compile(r"^\s*\d+\s*[.、)．](?!\d)\s*")


def _strip_number_prefix(line: str) -> str:
    return _NUMBER_PREFIX.sub("", line, count=1).strip()

# 纠错分批的失败重试间隔（秒）。网关会瞬时抖动：实测串行复跑里出现
# 23/40 行、1/40 行、HTTP 502/403，单独重试全部成功且行数正确。
# 默认重试 2 次（共 3 次尝试），总等待 5s，避免把整场耗时拖长。
FIX_RETRY_DELAYS = (1.0, 4.0)

# 要点终稿的 token 预算与续写上限。网关把 reasoning_tokens 计入 max_tokens，
# 实测 8000 会在 3000 字左右触顶返回 finish_reason="length"。
DIGEST_MAX_TOKENS = 16000
DIGEST_MAX_CONTINUATIONS = 3


CONTINUE_PROMPT = (
    "上一条回复因长度上限被截断。请从中断处紧接着继续写，"
    "不要重复已输出的内容，不要加任何说明或新标题。"
)


def _call(model: str, prompt: str, endpoint: str, api_key: str,
          max_tokens: int = 8000, timeout: int = 240,
          reasoning_effort: str | None = DEFAULT_REASONING_EFFORT,
          messages: list[dict] | None = None,
          out_meta: dict | None = None) -> str:
    """单次网关调用，默认返回 content 文本（保持旧签名兼容）。

    messages 非 None 时代替 prompt 作为对话历史（多轮续写用）；
    out_meta 非 None 时写入 finish_reason 与 usage。

    实测该网关（workbuddy/deepseek-v4.1-flash）把 reasoning_tokens 计入
    completion_tokens/max_tokens，触顶时返回 finish_reason="length"、
    content 被硬截断。调用方必须检查 finish_reason，不能只看有无内容。
    """
    payload = {
        "model": model,
        "messages": messages if messages is not None else [{"role": "user", "content": prompt}],
        "max_tokens": max_tokens,
    }
    # 思考档位只在 VALID_REASONING_EFFORTS 里选。实测 group/deepseek 传 medium
    # 虽返回 200 但行为不稳（reasoning_tokens 2751/5559，同一批 33.7s/63.6s），
    # max 直接 403；质量对比 40 行样本 low 改对 2 行、high 1 行，故取 low。
    if reasoning_effort:
        payload["reasoning_effort"] = reasoning_effort

    req = urllib.request.Request(
        endpoint, data=json.dumps(payload).encode(),
        headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=timeout) as r:  # noqa: S310
        d = json.loads(r.read().decode())
    choice = d["choices"][0]
    if out_meta is not None:
        out_meta["finish_reason"] = choice.get("finish_reason")
        out_meta["usage"] = d.get("usage")
    return (choice["message"].get("content") or "").strip()


def _merge_continuation(prev: str, piece: str) -> str:
    """拼接续写片段，去掉与上一段结尾重复的前缀（模型偶尔会回吐几字）。"""
    if not prev or not piece:
        return prev + piece
    for k in range(min(len(prev), len(piece), 500), 0, -1):
        if prev.endswith(piece[:k]):
            return prev + piece[k:]
    return prev + piece


def _complete_multi(messages: list[dict], *, model: str, endpoint: str,
                    api_key: str, max_tokens: int, timeout: int,
                    reasoning_effort: str,
                    max_continuations: int = 3) -> tuple[str, dict]:
    """调用网关；finish_reason=="length" 时自动多轮续写并拼接。

    返回 (text, meta)。meta 字段：finish_reason（最后一轮）、
    continuations（续写次数）、truncated（续写用尽后仍被截断）、usage。
    """
    convo = list(messages)
    text = ""
    cont = 0
    meta: dict = {"finish_reason": None, "continuations": 0,
                  "truncated": False, "usage": None}
    while True:
        call_meta: dict = {}
        piece = _call(model, convo[0]["content"], endpoint, api_key,
                      max_tokens=max_tokens, timeout=timeout,
                      reasoning_effort=reasoning_effort,
                      messages=convo, out_meta=call_meta)
        text = _merge_continuation(text, piece)
        meta["finish_reason"] = call_meta.get("finish_reason")
        if call_meta.get("usage") is not None:
            meta["usage"] = call_meta["usage"]
        if meta["finish_reason"] != "length":
            break
        if cont >= max_continuations:
            meta["truncated"] = True
            break
        cont += 1
        meta["continuations"] = cont
        convo = convo + [
            {"role": "assistant", "content": text},
            {"role": "user", "content": CONTINUE_PROMPT},
        ]
    return text, meta


def fix_errors(lines: list[str], model: str = DEFAULT_MODEL,
               endpoint: str = DEFAULT_ENDPOINT, api_key: str = "magpie-alma",
               vocabulary: str = "", batch_size: int = 40,
               progress: Callable[[int, int], None] | None = None,
               workers: int = DEFAULT_FIX_WORKERS,
               retry_delays: tuple[float, ...] | None = None,
               reasoning_effort: str = DEFAULT_REASONING_EFFORT,
               out_stats: dict | None = None) -> list[str]:
    """按领域词表纠正同音字。重试用尽的分批保留原文。

    workers > 1 时用线程池并发请求网关；结果按批下标拼回，顺序与输入一致。
    实测同一份 12 批：并发 3 路 83.7s，并发 12 路 35.4s，网关可横向扩展；
    27 批全量串行约 10 分钟，6 路并发约 102 秒。

    retry_delays 是失败重试的等待序列（默认 1s/4s，共 3 次尝试）。
    HTTP 错误与行数不匹配都会重试；做成可注入是为了测试不必真 sleep。

    out_stats 可选传入一个 dict：会把本次调用的参数与实际统计写进去
    （batches / retry_count / failed_batches / workers / model /
    reasoning_effort / elapsed_seconds），供产物 meta 追溯。
    返回值仍为 list[str]，保持对既有调用方兼容。
    """
    started = time.monotonic()
    reasoning_effort = validate_reasoning_effort(reasoning_effort)
    stats: dict = {
        "model": model,
        "reasoning_effort": reasoning_effort,
        "workers": workers,
        "batches": 0,
        "retry_count": 0,
        "failed_batches": [],
        "elapsed_seconds": 0.0,
    }

    if not lines:
        if out_stats is not None:
            out_stats.update(stats)
        return lines

    delays = tuple(retry_delays) if retry_delays is not None else FIX_RETRY_DELAYS
    total_batches = (len(lines) + batch_size - 1) // batch_size
    batches = [lines[i : i + batch_size] for i in range(0, len(lines), batch_size)]
    stats["batches"] = total_batches

    def process(bi: int, batch: list[str]) -> tuple[list[str], int, str]:
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
        last_err = ""
        retries = 0
        max_tokens = FIX_MAX_TOKENS
        for attempt in range(len(delays) + 1):
            if attempt:
                retries += 1
                delay = delays[attempt - 1]
                if delay:
                    time.sleep(delay)
            try:
                call_meta: dict = {}
                res = _call(model, prompt, endpoint, api_key,
                            max_tokens=max_tokens,
                            reasoning_effort=reasoning_effort,
                            out_meta=call_meta)
                # 输出被 max_tokens 截断：这批结果不完整，不可信任。
                # 截断常见于 reasoning_tokens 吃满预算，重试时放大预算，
                # 而不是用同样预算再问一遍（那样大概率再次截断）。
                if call_meta.get("finish_reason") == "length":
                    if max_tokens < FIX_MAX_TOKENS_CAP:
                        max_tokens = min(FIX_MAX_TOKENS_CAP, max_tokens * 2)
                        last_err = (f"输出被截断（finish_reason=length，"
                                    f"下一轮预算 {max_tokens}）")
                    else:
                        last_err = "输出被截断（finish_reason=length，已达预算上限）"
                    continue
                fixed = [
                    _strip_number_prefix(x)
                    for x in res.split("\n")
                    if x.strip()
                ]
                fixed = [x for x in fixed if x]
                # 行数不一致时不要用这批结果，避免错位
                if len(fixed) == len(batch):
                    if attempt:
                        print(f"[post] 纠错批次 {bi+1} 重试第 {attempt} 次恢复")
                    return fixed, retries, ""
                last_err = f"行数不匹配（{len(fixed)}≠{len(batch)}）"
            except Exception as e:  # noqa: BLE001
                last_err = str(e)[:120]

        print(f"[post] 纠错批次 {bi+1} 重试 {len(delays)} 次后仍失败"
              f"（{last_err}），保留原文")
        return batch, retries, last_err or "未知失败"

    if workers <= 1:
        out: list[str] = []
        for bi, batch in enumerate(batches):
            fixed, retries, err = process(bi, batch)
            out.extend(fixed)
            stats["retry_count"] += retries
            if err:
                stats["failed_batches"].append(bi + 1)
            if progress:
                progress(bi + 1, total_batches)
        stats["elapsed_seconds"] = round(time.monotonic() - started, 2)
        if out_stats is not None:
            out_stats.update(stats)
        return out

    results: list[list[str] | None] = [None] * total_batches
    done = 0
    with concurrent.futures.ThreadPoolExecutor(max_workers=workers) as executor:
        futures = {executor.submit(process, bi, b): bi
                   for bi, b in enumerate(batches)}
        for fut in concurrent.futures.as_completed(futures):
            bi = futures[fut]
            try:
                fixed, retries, err = fut.result()
                results[bi] = fixed
                stats["retry_count"] += retries
                if err:
                    stats["failed_batches"].append(bi + 1)
            except Exception as e:  # noqa: BLE001
                # process 内部已兜底，这里只防意外；该批仍保留原文
                print(f"[post] 纠错批次 {bi+1} 异常，保留原文: {str(e)[:120]}")
                results[bi] = batches[bi]
                stats["failed_batches"].append(bi + 1)
            done += 1
            # 并发时按完成数递增，不保证批次顺序
            if progress:
                progress(done, total_batches)

    final: list[str] = []
    for bi, batch in enumerate(results):
        if batch is None:  # 理论上不会发生；兜底保留原文，绝不让行数变少
            print(f"[post] 纠错批次 {bi+1} 无结果，保留原文")
            batch = batches[bi]
            stats["failed_batches"].append(bi + 1)
        final.extend(batch)
    stats["failed_batches"] = sorted(set(stats["failed_batches"]))
    stats["elapsed_seconds"] = round(time.monotonic() - started, 2)
    if out_stats is not None:
        out_stats.update(stats)
    return final


def build_digest(segments: list[dict], model: str = DEFAULT_MODEL,
                 endpoint: str = DEFAULT_ENDPOINT, api_key: str = "magpie-alma",
                 title: str = "",
                 group_seconds: float = 60.0,
                 reasoning_effort: str = DEFAULT_REASONING_EFFORT,
                 out_stats: dict | None = None) -> str:
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

    started = time.monotonic()
    reasoning_effort = validate_reasoning_effort(reasoning_effort)
    stats: dict = {
        "model": model,
        "reasoning_effort": reasoning_effort,
        "seconds": 0.0,
        "ok": False,
    }

    def finish(text: str, ok: bool) -> str:
        stats["seconds"] = round(time.monotonic() - started, 2)
        stats["ok"] = ok
        if out_stats is not None:
            out_stats.update(stats)
        return text

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
        chunk_errors: list[str] = []
        chunk_continuations = 0
        for i, c in enumerate(chunks):
            p = (f"下面是直播的部分记录（第{i+1}/{len(chunks)}部分），请提炼要点，"
                 f"保留具体学校名、分数、建议，不要泛泛而谈：\n\n{c}")
            try:
                part, pmeta = _complete_multi(
                    [{"role": "user", "content": p}], model=model,
                    endpoint=endpoint, api_key=api_key, max_tokens=4000,
                    timeout=240, reasoning_effort=reasoning_effort,
                    max_continuations=2)
                chunk_continuations += pmeta["continuations"]
                if pmeta["truncated"]:
                    chunk_errors.append(
                        f"第{i+1}部分摘要续写 {pmeta['continuations']} 次后仍被截断")
                partials.append(part)
            except Exception as e:  # noqa: BLE001
                chunk_errors.append(f"第{i+1}部分摘要失败: {str(e)[:60]}")
                partials.append(f"(第{i+1}部分摘要失败: {str(e)[:60]})")
        stats["chunk_continuations"] = chunk_continuations
        stats["failed_chunks"] = chunk_errors
        text = "\n\n".join(f"### 片段{i+1}\n{p}" for i, p in enumerate(partials))
        text = text[:60000]
    else:
        chunk_errors = []

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
    stats.setdefault("finish_reason", None)
    stats.setdefault("continuations", 0)
    stats.setdefault("truncated", False)
    try:
        digest, dmeta = _complete_multi(
            [{"role": "user", "content": prompt}], model=model,
            endpoint=endpoint, api_key=api_key, max_tokens=DIGEST_MAX_TOKENS,
            timeout=600, reasoning_effort=reasoning_effort,
            max_continuations=DIGEST_MAX_CONTINUATIONS)
    except Exception as e:  # noqa: BLE001
        stats["error"] = str(e)[:150]
        return finish(f"(要点整理失败: {str(e)[:150]})", False)

    stats["finish_reason"] = dmeta["finish_reason"]
    stats["continuations"] = dmeta["continuations"]
    stats["truncated"] = dmeta["truncated"]

    errors = list(chunk_errors)
    if dmeta["truncated"]:
        errors.append(f"要点整理续写 {dmeta['continuations']} 次后仍被截断"
                      f"（finish_reason=length），内容不完整")
    if errors:
        stats["error"] = "；".join(errors)[:300]
        return finish(digest, False)
    return finish(digest, True)


def _clean_suggested_title(raw: str, max_len: int = 48) -> str:
    """清洗 LLM 返回的标题文本，去掉引号、前缀与多余符号。"""
    for line in (raw or "").splitlines():
        t = line.strip()
        if not t:
            continue
        t = t.lstrip("#*-•·> ").strip()
        for prefix in ("直播标题：", "直播标题:", "场次名称：", "场次名称:", "标题：", "标题:"):
            if t.startswith(prefix):
                t = t[len(prefix):].strip()
        t = t.strip(" \t\"'“”‘’《》「」『』【】`")
        if not t:
            continue
        return t[:max_len].strip()
    return ""


def suggest_title(
    source: str | list[dict],
    model: str = DEFAULT_MODEL,
    endpoint: str = DEFAULT_ENDPOINT,
    api_key: str = "magpie-alma",
    reasoning_effort: str = DEFAULT_REASONING_EFFORT,
) -> str:
    """根据 AI 要点文本或转写片段提炼一个简短准确的场次标题。失败时返回空串。"""
    reasoning_effort = validate_reasoning_effort(reasoning_effort)
    if isinstance(source, str):
        snippet = source.strip()[:3500]
    else:
        parts: list[str] = []
        total = 0
        for s in source or []:
            txt = str(s.get("text") or "").strip() if isinstance(s, dict) else str(s).strip()
            if not txt:
                continue
            parts.append(txt)
            total += len(txt) + 1
            if total >= 3500:
                break
        snippet = "\n".join(parts)[:3500]

    if len(snippet) < 20:
        return ""

    prompt = (
        "请根据下面这场微信视频号直播的内容，提炼一个简短、信息明确的任务标题（8~24个字）。\n"
        "规则：\n"
        "1. 如果内容中能看出主播名、老师名或机构名，优先写成「主播或机构 - 核心主题」\n"
        "2. 如果看不出主播名，直接概括「核心主题」\n"
        "3. 只输出标题这一行文字，不要加引号、书名号、序号、Markdown 符号或任何解释。\n\n"
        f"{snippet}"
    )
    try:
        # 网关把 reasoning_tokens 计入 max_tokens，给 1024 避免思考耗尽输出空间
        raw = _call(
            model=model,
            prompt=prompt,
            endpoint=endpoint,
            api_key=api_key,
            max_tokens=1024,
            timeout=60,
            reasoning_effort=reasoning_effort,
        )
    except Exception:  # noqa: BLE001
        return ""
    return _clean_suggested_title(raw)


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
