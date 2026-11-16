"""把逐句转录聚合成可读的段落（用于 .md 输出）。

## 为什么需要聚合

Gemini 不返回真实时间戳，我们按字数比例估算，误差可达 ±10~30 秒。
所以"秒级时间戳"是伪精度 —— 看着细，其实对不上真实位置。

同时实测：81 分钟的转录里，时间戳占文本量 **35.2%**
（正文 23850 字，时间戳开销 12945 字）。给 LLM 分析时三分之一 token
花在不准确的秒数上。

## 聚合策略：语义优先，长度兜底

直播间有很强的结构信号——主播的转折话术：
    "有家长问…" / "下一个问题" / "来我们看一下" / "那接下来"

在这些地方断段，比按固定 30 秒切更贴合内容：问答关系完整，可读性更好。
只有在长时间没有转折信号时，才用长度兜底（默认 60 秒）强制断开，
避免出现几百字的巨型段落。

## 重要：不改变任何 ASR 输出

本模块只作用于 .md 渲染。json / jsonl 保持逐句 + 原始时间戳，
将来要精确回查或接说话人分离时用得上。
"""

from __future__ import annotations

import re

# 主播的转折话术。命中即认为"新话题开始"，在此断段。
# 注意：这些是直播间高频说法，实测覆盖良好；宁可少断，不要乱断
_TURN_MARKERS = (
    "有家长问",
    "有家长说",
    "下一个问题",
    "下一个",
    "来我们看",
    "那我们看",
    "那我们说",
    "那接下来",
    "接下来",
    "好，我们",
    "我们先看",
    "再来看",
    "然后我们",
    "最后一个",
    "最后一位",
    "回答一下",
    "我再说",
    "补充一下",
    "总结一下",
)

# 编译成单个正则（只是判断是否命中，用 in 也行，但正则便于将来加容错）
_TURN_RE = re.compile("|".join(re.escape(m) for m in _TURN_MARKERS))


def _starts_new_turn(text: str) -> bool:
    """这句话是否标志着一个新话题的开始。

    只看句首 12 个字 —— 转折话术总在开头出现，
    句中出现（如"我刚才说了有家长问"）不算断点。
    """
    head = text[:12]
    return bool(_TURN_RE.search(head))


def _fmt_span(start: float, end: float) -> str:
    """段落时间范围，mm:ss（超过 1 小时用 h:mm:ss）。

    刻意不用秒级精度 —— 反正时间本来是估算的，
    写 [00:02:34] 这种只会让人误以为精确。
    """
    def one(sec: float) -> str:
        sec = max(0, int(sec))
        h, rem = divmod(sec, 3600)
        m, s = divmod(rem, 60)
        if h:
            return f"{h}:{m:02d}:{s:02d}"
        return f"{m:02d}:{s:02d}"

    return f"{one(start)}-{one(end)}"


def group_segments(
    segments: list[dict],
    *,
    max_seconds: float = 60.0,
    max_chars: int = 400,
    min_seconds: float = 8.0,
) -> list[dict]:
    """把逐句分段聚合成段落。

    Args:
        segments: [{start, end, text}, ...]
        max_seconds: 长度兜底 —— 超过这个时长强制断段，
            避免长时间无转折信号时出现巨型段落
        max_chars: 字数兜底，防止长句堆砌
        min_seconds: 段落太短（<这个值）且没有转折信号时，先不断，
            避免碎成一句一段

    Returns:
        [{start, end, text, count}, ...]
        count 是该段包含的原句数，便于回溯
    """
    if not segments:
        return []

    groups: list[dict] = []
    cur: list[dict] = []

    def flush() -> None:
        if not cur:
            return
        groups.append({
            "start": cur[0]["start"],
            "end": cur[-1]["end"],
            "text": "".join(s["text"] for s in cur),
            "count": len(cur),
        })
        cur.clear()

    for seg in segments:
        # 空白/纯空格句要整句丢弃，不能只判断 falsy —— "   " 是 truthy，
        # 会把空白拼进正文（实测出现过 "有内容   " 尾部带空格）
        text = str(seg.get("text") or "").strip()
        if not text:
            continue

        if cur:
            span = float(seg.get("end", 0)) - float(cur[0]["start"])
            chars = sum(len(s["text"]) for s in cur)
            started_turn = _starts_new_turn(text)
            too_long = span >= max_seconds or chars >= max_chars
            # 转折信号优先级最高：命中就断（前提是段已够长，避免碎段）
            if started_turn and span >= min_seconds:
                flush()
            elif too_long:
                flush()

        # 追加清理后的副本，避免把首尾空白带进正文
        cur.append({**seg, "text": text})

    flush()
    return groups


def render_paragraphs(
    segments: list[dict],
    *,
    max_seconds: float = 60.0,
    max_chars: int = 400,
    min_seconds: float = 8.0,
) -> str:
    """渲染成 Markdown 段落文本。

    每段一行：`**[mm:ss-mm:ss]** 正文`
    段与段之间空行，方便人读和 LLM 解析。
    """
    groups = group_segments(
        segments,
        max_seconds=max_seconds,
        max_chars=max_chars,
        min_seconds=min_seconds,
    )
    lines = [
        f"**[{_fmt_span(g['start'], g['end'])}]** {g['text']}"
        for g in groups
    ]
    return "\n\n".join(lines)


def stats(segments: list[dict], groups: list[dict]) -> dict:
    """聚合效果统计，用于日志与文档。"""
    n_seg = len(segments)
    n_grp = len(groups)
    seg_ts = n_seg * 15          # "**[00:00:00]** " 约 15 字符
    grp_ts = n_grp * 14          # "**[00:00-00:00]** " 约 14 字符
    body = sum(len(str(s.get("text") or "")) for s in segments)
    return {
        "segments": n_seg,
        "groups": n_grp,
        "reduction_pct": round((1 - n_grp / n_seg) * 100, 1) if n_seg else 0.0,
        "ts_chars_before": seg_ts,
        "ts_chars_after": grp_ts,
        "body_chars": body,
        "ts_ratio_before_pct": round(seg_ts / (body + seg_ts) * 100, 1) if body else 0.0,
        "ts_ratio_after_pct": round(grp_ts / (body + grp_ts) * 100, 1) if body else 0.0,
    }
