"""通过 magpie 网关调用 Gemini 做音频转写。

实测依据（见 docs/ASR方案对比实测.md）：
- 中文准确率显著高于本地 whisper（同音字、校名、术语都更准）
- RTF 0.07-0.14，比 whisper small(0.41) 和 medium(1.75) 都快
- 但有三个硬约束，必须处理：

1) **单次最长 120 秒**。超过会失败或触发安全过滤。
   → 内部自动切片，按 110 秒切（留安全余量），逐片调用再拼回时间戳。

2) **内容安全过滤**。同一条 180s 音频实测被 `content_filter` 拒绝。
   → 调用失败必须能被上层捕获并回退本地引擎，不能中断整场录制。

3) **可能输出繁体**。gemini-3.8-flash 实测整段繁体。
   → 内置繁转简（装了 opencc 就用，没装则用内置常用字映射兜底）。
"""

from __future__ import annotations

import base64
import json
import time
import urllib.error
import urllib.request

import numpy as np

from . import lexicon

DEFAULT_ENDPOINT = "http://127.0.0.1:3425/v1/chat/completions"
# 实测 120s 稳定通过，留 10s 余量防止边界抖动
MAX_CHUNK_SECONDS = 110
SAMPLE_RATE = 16000

PROMPT = (
    "请逐字转写这段中文直播音频。要求：\n"
    "1. 只输出转写文本，不要任何解释或说明\n"
    "2. 必须使用简体中文，绝对不要输出繁体字\n"
    "3. 按自然句分段，每句一行\n"
    "4. 数字用阿拉伯数字，专业名词保留原说法\n"
    "5. 不要翻译、不要总结、不要补充原文没有的内容"
)


def build_prompt(lexicon_hint: str = "") -> str:
    """拼装最终 prompt。领域词表能显著降低专有名词错误率。"""
    if not lexicon_hint:
        return PROMPT
    return f"{PROMPT}\n\n【领域参考】\n{lexicon_hint}"

# 繁→简 常用字兜底表（覆盖实测出现的高频字）
_TC2SC = str.maketrans(
    "個們這來說時會後裡麼為與對開關實現點線樣區問間還進過發現當從學經濟這樣沒電話"
    "讓給帶東車長門見覺聽講讀寫買賣價錢銀藝術動員員國圖書畫筆記億萬歲歷經歷",
    "个们这来说时会后里么为与对开关实现点线样区问间还进过发现当从学经济这样没电话"
    "让给带东车长门见觉听讲读写买卖价钱银艺术动员员国图书画笔记亿万岁历经历",
)


def to_simplified(text: str) -> str:
    """繁转简。优先用 opencc，缺失时用内置映射兜底。"""
    if not text:
        return text
    try:
        from opencc import OpenCC

        return OpenCC("t2s").convert(text)
    except Exception:
        return text.translate(_TC2SC)


class GeminiASR:
    """与 local Transcriber 保持相同接口：transcribe(audio, sr) -> list[dict]"""

    def __init__(
        self,
        model: str = "google-vertex/gemini-4-hydrogen",
        endpoint: str = DEFAULT_ENDPOINT,
        api_key: str = "magpie-alma",
        reasoning_effort: str = "low",
        timeout: int = 300,
        lexicon_hint: str = "",
        correct_after: bool = True,
    ) -> None:
        self.model = model
        self.endpoint = endpoint
        self.api_key = api_key
        self.reasoning_effort = reasoning_effort
        self.timeout = timeout
        self._total_audio = 0.0
        self._total_infer = 0.0
        # 领域词表提示：直接拼进 prompt，能显著降低专有名词错误
        self.prompt = build_prompt(lexicon_hint)
        # 转写后是否按实测错字表做纠正
        self.correct_after = correct_after
        print(
            f"[gemini-asr] 使用 {model}（单次上限 {MAX_CHUNK_SECONDS}s"
            f"{'，已注入领域词表' if lexicon_hint else ''}"
            f"{'，启用错字纠正' if correct_after else ''}）"
        )

    # ---------- 单次 HTTP 调用 ----------

    def _call_once(self, pcm: np.ndarray) -> str:
        import io

        import soundfile as sf

        buf = io.BytesIO()
        sf.write(buf, np.clip(pcm, -1.0, 1.0), SAMPLE_RATE, format="WAV", subtype="PCM_16")
        b64 = base64.b64encode(buf.getvalue()).decode()

        payload = {
            "model": self.model,
            "messages": [
                {
                    "role": "user",
                    "content": [
                        {"type": "text", "text": self.prompt},
                        {"type": "input_audio", "input_audio": {"data": b64, "format": "wav"}},
                    ],
                }
            ],
            "max_tokens": 16000,
        }
        if self.reasoning_effort:
            # 关键：不压低思考强度时，模型会把 token 全烧在 reasoning 上，输出为空
            payload["reasoning_effort"] = self.reasoning_effort

        req = urllib.request.Request(
            self.endpoint,
            data=json.dumps(payload).encode(),
            headers={
                "Authorization": f"Bearer {self.api_key}",
                "Content-Type": "application/json",
            },
        )
        with urllib.request.urlopen(req, timeout=self.timeout) as r:  # noqa: S310
            data = json.loads(r.read().decode())
        return (data["choices"][0]["message"].get("content") or "").strip()

    # ---------- 对外接口 ----------

    def transcribe(self, audio: np.ndarray, sample_rate: int = SAMPLE_RATE) -> list[dict]:
        """转写音频，返回 [{start, end, text, words}]，时间是相对片段起点的秒。"""
        # 空输入必须早退：否则会拿空音频去问模型，
        # 模型会顺着 prompt 凭空编出一大段内容（实测生成过 206 句假句子）。
        if len(audio) == 0:
            return []
        # 极短片段（<0.3s）没有转写价值，且容易让模型瞎猜
        if len(audio) / sample_rate < 0.3:
            return []

        if sample_rate != SAMPLE_RATE:
            import soxr

            audio = soxr.resample(np.asarray(audio, dtype=np.float32), sample_rate, SAMPLE_RATE, quality="HQ")
        audio = np.ascontiguousarray(audio, dtype=np.float32)

        t0 = time.time()
        total_sec = len(audio) / SAMPLE_RATE
        chunk_samples = MAX_CHUNK_SECONDS * SAMPLE_RATE
        out: list[dict] = []

        for offset in range(0, len(audio), chunk_samples):
            piece = audio[offset : offset + chunk_samples]
            start = offset / SAMPLE_RATE
            dur = len(piece) / SAMPLE_RATE
            try:
                text = self._call_once(piece)
            except urllib.error.HTTPError as e:
                body = ""
                try:
                    body = e.read().decode()[:200]
                except Exception:
                    pass
                raise RuntimeError(f"HTTP {e.code}: {body}") from e

            if not text:
                print(f"[gemini-asr] {start:.0f}s 片段返回空文本，跳过")
                continue

            # 繁转简 → 按实测错字表纠正（省实/金广附/指标到校等专有名词）
            text = to_simplified(text)
            if self.correct_after:
                text = lexicon.correct_text(text)
            lines = [x.strip() for x in text.split("\n") if x.strip()]
            if not lines:
                continue

            # Gemini 不返回逐句时间戳，按字数比例估算分配，
            # 保证时间轴单调且覆盖整段（LLM 分析时够用）
            lens = [max(1, len(x)) for x in lines]
            tot = sum(lens)
            cur = start
            for line, ln in zip(lines, lens):
                span = dur * (ln / tot)
                out.append(
                    {
                        "start": round(cur, 2),
                        "end": round(cur + span, 2),
                        "text": line,
                        "words": [],
                    }
                )
                cur += span

        elapsed = time.time() - t0
        self._total_audio += total_sec
        self._total_infer += elapsed
        rtf = elapsed / total_sec if total_sec else 0
        print(f"[gemini-asr] {total_sec:.1f}s 音频 → {len(out)} 句，耗时 {elapsed:.1f}s (RTF={rtf:.2f})")
        return out

    @property
    def stats(self) -> dict:
        return {
            "engine": f"gemini:{self.model}",
            "audio_seconds": round(self._total_audio, 1),
            "infer_seconds": round(self._total_infer, 1),
            "rtf": round(self._total_infer / self._total_audio, 2) if self._total_audio else 0,
        }
