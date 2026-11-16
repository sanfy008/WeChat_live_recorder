"""ASR 转写封装。

用 faster-whisper（CTranslate2 后端）：
- CPU 上 int8 量化，比原版 whisper 快 3-4 倍
- 自带 Silero VAD 预过滤，能去掉长静音里的幻听
- 支持 word-level 时间戳，便于后续做溯源

模型选择建议（CPU 实测经验）：
- tiny   : 快但中文错字多，只适合做"有没有人说话"的粗判
- base   : 勉强可用
- small  : 中文可用的下限，CPU 约 0.5-1x 实时（推荐默认）
- medium : 中文明显更准，CPU 约 2-4x 实时（需等待）
- large-v3: 最准，CPU 太慢，建议配 GPU
"""

from __future__ import annotations

import os
import sys
import time

import numpy as np


def _collapse_repeats(text: str, min_phrase: int = 6, max_rounds: int = 8) -> str:
    """压制 whisper 在长音频上的重复循环。

    典型症状（实测）：
      "……第二款是基础款,如果单纯是299元,其实不太必要去冲,
       第二款是基础款,如果单纯是299元,其实不太必要去冲,……"
    whisper 在解码退化时会反复吐同一句。这里用简单的后缀-前缀拼接检测，
    发现循环就只保留一份。

    只处理"相邻重复"，不碰正常的排比句 —— 阈值取 min_phrase=6 个字以上，
    短句（"好的""对"）重复是真实口语，不能删。
    """
    if len(text) < min_phrase * 2:
        return text

    for _ in range(max_rounds):
        n = len(text)
        changed = False
        # 从长到短找重复单元，优先消掉最长的循环
        for unit in range(n // 2, min_phrase - 1, -1):
            # 检查末尾是否由同一单元重复构成
            tail = text[n - unit :]
            start = n - unit * 2
            if start >= 0 and text[start : n - unit] == tail:
                # 找到循环，压缩成一份
                text = text[: n - unit]
                changed = True
                break
        if not changed:
            break
    return text


class Transcriber:
    def __init__(
        self,
        model_size: str = "small",
        device: str = "cpu",
        compute_type: str = "int8",
        language: str = "zh",
        beam_size: int = 5,
        vad_filter: bool = True,
        initial_prompt: str = "",
    ) -> None:
        # 国内网络访问 HuggingFace 常被阻断，默认走镜像
        os.environ.setdefault("HF_ENDPOINT", "https://hf-mirror.com")

        from faster_whisper import WhisperModel

        self.language = language
        self.beam_size = beam_size
        self.vad_filter = vad_filter
        self.initial_prompt = initial_prompt

        t0 = time.time()
        print(f"[asr] 加载模型 faster-whisper/{model_size} ({device}/{compute_type})…")
        self.model = WhisperModel(model_size, device=device, compute_type=compute_type)
        print(f"[asr] 模型就绪，耗时 {time.time() - t0:.1f}s")

        self._total_audio = 0.0
        self._total_infer = 0.0

    def transcribe(self, audio: np.ndarray, sample_rate: int = 16000) -> list[dict]:
        """转写一段音频，返回 [{start, end, text, words}]（时间是片段内相对秒）。"""
        if len(audio) == 0:
            return []

        # 防御性检查：whisper 只接受 16kHz。
        # 传 48k 进来会被当成慢放 3 倍，输出几乎全是幻觉 —— 而且不报错，
        # 极难排查。所以在入口强制转换，宁可有损也不能喂错。
        if sample_rate != 16000:
            import soxr

            audio = soxr.resample(
                np.asarray(audio, dtype=np.float32), sample_rate, 16000, quality="HQ"
            )
            print(f"[asr] 采样率 {sample_rate}≠16000，已自动重采样")

        audio = np.ascontiguousarray(audio, dtype=np.float32)
        t0 = time.time()
        segments, _info = self.model.transcribe(
            audio,
            language=self.language,
            beam_size=self.beam_size,
            vad_filter=self.vad_filter,
            vad_parameters={"min_silence_duration_ms": 500} if self.vad_filter else None,
            initial_prompt=self.initial_prompt or None,
            condition_on_previous_text=False,  # 关键：避免上一段内容污染下一段
            word_timestamps=True,
        )

        out: list[dict] = []
        for s in segments:
            text = (s.text or "").strip()
            if not text:
                continue
            text = _collapse_repeats(text)
            if not text:
                continue
            out.append(
                {
                    "start": float(s.start),
                    "end": float(s.end),
                    "text": text,
                    "words": [
                        {"w": w.word, "s": float(w.start), "e": float(w.end)}
                        for w in (s.words or [])
                    ],
                }
            )

        dur = len(audio) / sample_rate
        elapsed = time.time() - t0
        self._total_audio += dur
        self._total_infer += elapsed
        rtf = elapsed / dur if dur else 0
        print(f"[asr] {dur:.1f}s 音频 → {len(out)} 句，耗时 {elapsed:.1f}s (RTF={rtf:.2f})")
        return out

    @property
    def stats(self) -> dict:
        return {
            "audio_seconds": round(self._total_audio, 1),
            "infer_seconds": round(self._total_infer, 1),
            "rtf": round(self._total_infer / self._total_audio, 2) if self._total_audio else 0,
        }


def check_available(model_size: str = "tiny") -> bool:
    """快速自检：能否加载模型。用于环境诊断。"""
    try:
        Transcriber(model_size=model_size)
        return True
    except Exception as e:  # noqa: BLE001
        print(f"[asr] 自检失败: {e}", file=sys.stderr)
        return False
