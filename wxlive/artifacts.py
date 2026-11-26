"""产物质量自检：把「看起来正常但内容已坏」的情况变成可量化的信号。

## 为什么需要这个模块

项目里反复出现同一类问题：**产物文件齐全、行数守恒、读起来通顺，但内容有问题**。
2026-11-26 复盘 7 份真实产物时，当场抓到两类以前验收完全查不出来的缺陷：

1. **繁简残留** —— 某一场的终稿里有 574 句是繁体。原有验收（行数守恒 /
   失败批 0 / 三份产物齐全）全部通过，因为它们不看字形。
2. **幻觉残留** —— Gemini 的「我无法访问音频」句子进了终稿，还被 LLM 纠错
   顺手改通顺了，读起来像正常正文。

这两类都不是「模型波动」，而是**确定性缺陷**，判据稳定、应当清零。

## 设计原则

- **纯本地计算**：只读已经产出的 segments，不重跑 ASR、不调 LLM，毫秒级。
- **只报告，不改写**：它负责让问题可见，不负责猜，以免把好数据改坏。
- **判据落在能人工复核的事实上**：每个计数都能点开具体句子看。
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

from .lexicon import to_simplified

# 只在繁体字形里才出现的标记字。用于快速判断「这段文本可能还是繁体」。
# 注意：这里放的是**判定用**的抽样字，不要求穷尽 —— 真检测靠 to_simplified 回环比对。
_TRAD_MARKERS = set(
    "話們級來這個會現後說韓體麼學國時間東進階師聯繫價藝術動員讀寫買賣"
    "點線樣區問間還進過發現當從經濟讓給帶車長門見覺聽講億萬歲歷經"
)

# 「模型在解释自己没收到音频」的句式。与 quality._HALLUCINATION 同源，
# 这里单独保留一份：那个正则是给「整块丢弃」用的，误判代价高、写得保守；
# 这个是给「事后体检」用的，宁可多报也不能漏报。
_LEAK_PAT = re.compile(
    r"\[attachment|audio/wav|input_audio|\[audio\]"
    # 「(不)能/可以 读/访问/获取 … 音频/录音/文件」的各种自然句式
    r"|(?:无法|不能|没能|不可以|没法).{0,6}(?:读取|读|访问|获取|打开|接收|收到)"
    r".{0,6}(?:音频|录音|文件)"
    r"|(?:无法|不能|未能).{0,8}(?:完成|进行|继续).{0,8}转写"
    r"|未(?:检测到|收到|能收到|获取到).{0,8}(?:音频|录音|文件)"
    r"|请(?:重新上传|先上传|上传)"
    r"|(?:我)?(?:没有|暂无).{0,6}(?:可用的|可读取的).{0,6}(?:音频|录音|文件)"
    r"|as an AI|I (?:can't|cannot|am unable|do not have access)",
    re.IGNORECASE,
)


@dataclass
class QualityReport:
    """一次产物体检的结果。全部为计数，0 表示通过。"""

    segments: int = 0
    # 终稿里仍是繁体、且 to_simplified() 本可以修掉的句子数
    trad_residual: int = 0
    # 终稿里含「我无法访问音频」类文本的句子数
    hallucination_residual: int = 0
    # 空文本句子数（转写出来但内容为空，通常是切分 bug）
    empty_text: int = 0
    samples: dict[str, list[str]] = field(default_factory=dict)

    @property
    def ok(self) -> bool:
        return not (self.trad_residual or self.hallucination_residual)

    def summary(self) -> str:
        if self.ok:
            return f"产物自检通过（{self.segments} 句，无繁简/幻觉残留）"
        parts = []
        if self.trad_residual:
            parts.append(f"繁简残留 {self.trad_residual} 句")
        if self.hallucination_residual:
            parts.append(f"幻觉残留 {self.hallucination_residual} 句")
        return "产物自检异常：" + "；".join(parts)

    def to_meta(self) -> dict:
        """写进 meta 的形态。刻意保持扁平、数值化，便于以后跨场次比对。"""
        d = {
            "segments": self.segments,
            "trad_residual": self.trad_residual,
            "hallucination_residual": self.hallucination_residual,
            "ok": self.ok,
        }
        if self.empty_text:
            d["empty_text"] = self.empty_text
        return d


def is_trad_residual(text: str) -> bool:
    """这句话是「活该被转成简体、但现在还是繁体」的吗？

    判据必须用「回环比对」而不是「有没有繁体字」：

    - `to_simplified(text) == text` → 已经是简体，或残留在转换表覆盖不到的字上。
      **这也算残留**（该转没转成），只是原因不同（表缺字 vs 没调用）。
    - `to_simplified(text) != text` → 转换会产生变化，说明这句仍是繁体。
      **这才是主判据**：终稿不该是需要转换的文本。

    注意这里刻意把两种情况都算作残留 —— 终稿里出现繁体，无论原因是
    「没调用转换」还是「兜底表缺字」，都是缺陷，应当清零。
    """
    if not text:
        return False
    if not any(c in _TRAD_MARKERS for c in text):
        return False
    # 只要能转出不同的结果，就说明原文仍是繁体形态
    return to_simplified(text) != text or any(
        c in _TRAD_MARKERS for c in to_simplified(text))


def is_leaked(text: str) -> bool:
    """这句是不是「模型在解释自己没收到音频」的泄漏文本。"""
    return bool(text and _LEAK_PAT.search(text))


def check_segments(segments: list[dict], max_samples: int = 5) -> QualityReport:
    """体检一段转写结果。只读，不修改入参。

    segments 的元素形如 {"text": ..., "start": ..., "end": ...}。
    """
    rep = QualityReport(segments=len(segments))
    trad_ex: list[str] = []
    leak_ex: list[str] = []

    for seg in segments or []:
        text = (seg.get("text") or "").strip()
        if not text:
            rep.empty_text += 1
            continue
        if is_trad_residual(text):
            rep.trad_residual += 1
            if len(trad_ex) < max_samples:
                trad_ex.append(text)
        if is_leaked(text):
            rep.hallucination_residual += 1
            if len(leak_ex) < max_samples:
                leak_ex.append(text)

    if trad_ex:
        rep.samples["trad_residual"] = trad_ex
    if leak_ex:
        rep.samples["hallucination_residual"] = leak_ex
    return rep
