"""ASR 输出的质量检查：识别"模型没在转写、而是在解释自己"的幻觉输出。

## 为什么需要这一层

2026-11-16 的两份真实转写里出现了这种东西，被当成正文存进了 json / md / jsonl：

    抱歉，我无法访问或接收您提到的音频文件 `[attachment audio/wav]`，
    当前对话中未包含实际音频内容……

    无法完成转写，当前会话中仅包含 `[attachment audio/wav]` 的文字占位……

排查结论（都有实测依据）：

1. **音频本身是好的**——抽那 5 个位置读原始 WAV，RMS 0.093~0.117，
   峰值 0.96，比正常对照区（RMS 0.086）还响。不是录音中断，不是静音。
2. **100% 落在 110 秒切片边界上**——起点在 300s 块内的偏移全是 0/110/220，
   也就是切片块的起点或尾部残余块。切片把音频硬切断，模型拿到边界块后拒绝转写。
3. **代码完全不知道**——HTTP 返回 200 就算成功，meta 里写着 `primary_fail: 0`、
   `degraded: false`。幻觉句一路进了纠错和摘要。
4. **时有时无**——同一份 WAV 跑两次：15:29 的产物 0 条，17:05 的产物 5 条
   （句数也差 9.6%）。是模型对边界块的不稳定行为，不是确定性故障。

第 4 点很重要：**既然是瞬时的，重试就是有效手段**。实测同一个幻觉块原样连跑
三次全部正常，错开 ±5s 也能正常出内容。所以这里只负责"判定"，
"怎么救"交给 `gemini_asr` 的重试与二分逻辑。

## 判定原则：宁可漏判，不可误杀

误杀会丢掉主播真实说过的话，比漏判更糟（漏判只是保留了一条已知类型的问题行）。
所以规则写得很窄——只匹配"模型在描述自己收到了什么"这类不可能出现在
升学直播口播里的话。
"""

from __future__ import annotations

import re

# 明确的幻觉特征。刻意收窄：
# - 只认"（无法/不能/未能/没有）+ 访问/接收/读取/获取/收到 + 音频/文件"这类组合
# - 只认 attachment/input_audio 这种占位符字面量
# - 只认教科书式的 AI 免责话术
# 主播说"我无法理解这个"、"没有这个文件"都不会命中。
_HALLUCINATION = re.compile(
    r"attachment\s*audio"          # 模型看到的占位符原文
    r"|input_audio"
    r"|\[audio\]"
    r"|(?:无法|不能|未能|没有办法|没有|无法正常|抱歉|不好意思)"
    r".{0,12}(?:访问|接收|读取|获取|收到|提取|打开).{0,12}(?:音频|文件|录音)"
    r"|未(?:收到|能收到|检测到).{0,8}音频"
    r"|(?:无法|不能|未能).{0,8}(?:完成|进行|继续).{0,8}转写"
    r"|(?:音频|文件).{0,8}(?:占位|缺失|为空|不存在)"
    r"|\b(?:as an AI|I (?:can't|cannot|am unable|do not have access))\b",
    re.IGNORECASE,
)

# 正常中文口播语速约 4-5 字/秒，实测 110 秒块产出 580-670 字。
# 幻觉块只有 86-96 字（密度 0.8 字/秒）。取 1.5 字/秒作可疑线，
# 离两端都有很大余量。
_MIN_CHARS_PER_SEC = 1.5


def is_hallucination(text: str) -> bool:
    """这段输出是否是"模型在解释自己没收到音频"。

    只做**字面特征**判定，不掺入长度推断——长度可疑但内容真实的情况
    （比如整块是背景音乐）不应该被当成幻觉丢掉。
    """
    if not text:
        return False
    return bool(_HALLUCINATION.search(text))


def looks_truncated(text: str, duration: float) -> bool:
    """输出短得和音频时长不匹配。

    这**只用作重试触发器**，绝不用作丢弃依据：
    一块音频真的没说话时，正常输出本来就该很短。重试一次确认即可，
    确认后仍短就照实保留（那可能就是真实情况）。
    """
    if duration <= 0 or not text:
        return False
    # 太短的块没有统计意义，30 秒以上才判断
    if duration < 30:
        return False
    return len(text) / duration < _MIN_CHARS_PER_SEC


def is_bad_output(text: str, duration: float) -> bool:
    """需要重试的判定：明确幻觉，或长度明显不匹配。"""
    return is_hallucination(text) or looks_truncated(text, duration)


# ---------- 分句失败（"整块塌成一句"） ----------
#
# 这是比幻觉更普遍的问题：模型把整块音频塞进一行，内容是真的，
# 但句级时间戳全部失效（一句横跨 110 秒在物理上不可能）。
#
# 实测 2026-11-16 的 7 份产物，每份都有 1~10 处：
#   1561 句那份 5 处 / 1956 句那份 3 处 / 1759 句那份 10 处
# 其中"1759 句那份"开头两块连续塌陷（[0-110]、[110-220]）。
#
# 为什么要管：这类行在 md 里会变成一段几百字的巨型段落，
# 段落聚合的 max_seconds 兜底也会被它突破，而且它污染"段级时间可信"这个前提。

# 一句中文口播不可能持续这么久（含停顿，实测最长自然句约 25 秒）
MAX_SENTENCE_SECONDS = 45.0


def looks_unsplit(lines: list[str], duration: float) -> bool:
    """模型把整块塞成一行（或极少几行），导致分句失效。

    判据用"行数 + 句长"而不是单纯字数：
    内容可能很长但确实是漏了换行——这正是要重试的信号。
    """
    if duration < 30 or not lines:
        return False
    # 行数太少（30 秒以上的音频正常应有至少 2-3 句）
    if len(lines) <= 1 and duration >= 45:
        return True
    # 平均每行太长：整块糊成 2-3 行且每行几百字
    if len(lines) <= 3 and duration >= 60:
        avg = sum(len(x) for x in lines) / len(lines)
        if avg > 150:
            return True
    return False


def split_long_line(text: str, limit: int = 80) -> list[str]:
    """把塌陷成长的单行按标点硬拆，作为**最后兜底**。

    只在模型重试后仍然不分句时使用。优先在句末标点切，
    找不到就退到逗号，再找不到就在 limit 处硬切——硬切至少能让
    段落聚合和时间戳不至于完全失效。
    """
    text = (text or "").strip()
    if not text:
        return []
    if len(text) <= limit:
        return [text]

    out: list[str] = []
    buf = ""
    for ch in text:
        buf += ch
        if len(buf) >= limit // 2 and ch in "。！？；":
            out.append(buf.strip())
            buf = ""
        elif len(buf) >= limit and ch in "，、,":
            out.append(buf.strip())
            buf = ""
        elif len(buf) >= limit * 2:
            # 极端情况（一句话几百字没有标点），必须硬切
            out.append(buf.strip())
            buf = ""
    if buf.strip():
        out.append(buf.strip())
    return [x for x in out if x]


def compute_completeness(
    *,
    drain_incomplete: bool = False,
    dropped_chunks: int = 0,
    asr_queue_dropped: int = 0,
    asr_spilled: int = 0,
    asr_failed_chunks: int = 0,
    has_content_loss: bool = False,
) -> dict:
    """统一判定本次产物是否完整。

    之前 pipeline / 两个离线脚本 / NAS 各有一套判定：
    pipeline 只看收尾线程有没有超时，脚本/NAS 只看末句时间与音频时长差。
    两边都会漏掉“片段被丢、占位符进正文”这类内容损失。

    这里把已知的丢失来源汇总成一处：
      - drain_incomplete：收尾等待超时
      - dropped_chunks：采集端丢块（录音本身可能缺）
      - asr_queue_dropped：转写队列满导致片段未入队（主循环）
      - asr_spilled：片段落盘到 spilled 但未转写
      - asr_failed_chunks：转写调用异常导致片段丢失
      - has_content_loss：引擎内部已落占位符（如 Gemini 整块失败）

    返回 `{"complete": bool, "reasons": [...]}`，reasons 可直接写进产物 meta，
    用户/界面不必再猜“为什么这份稿子不可信”。
    """
    reasons: list[str] = []
    if drain_incomplete:
        reasons.append("drain_timeout")
    if dropped_chunks:
        reasons.append(f"capture_dropped_chunks={int(dropped_chunks)}")
    if asr_queue_dropped:
        reasons.append(f"asr_queue_dropped={int(asr_queue_dropped)}")
    if asr_spilled:
        reasons.append(f"asr_spilled={int(asr_spilled)}")
    if asr_failed_chunks:
        reasons.append(f"asr_failed_chunks={int(asr_failed_chunks)}")
    if has_content_loss:
        reasons.append("has_content_loss")
    return {"complete": not reasons, "reasons": reasons}


def placeholder_text(start: float, end: float) -> str:
    """彻底救不回来时的占位文案。

    落占位比静默丢弃好：用户看 md 时能立刻知道这里有个缺口，
    而不是以为主播那段没说话。方括号 + 显式字样，不会和口播内容混淆。
    """

    def one(sec: float) -> str:
        sec = max(0, int(sec))
        h, rem = divmod(sec, 3600)
        m, s = divmod(rem, 60)
        return f"{h:02d}:{m:02d}:{s:02d}"

    return f"[转写失败 {one(start)}-{one(end)}，{int(max(0, end - start))} 秒]"
