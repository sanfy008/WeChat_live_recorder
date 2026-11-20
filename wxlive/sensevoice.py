"""本地 SenseVoice-Small (INT8 ONNX) ASR 封装。

为什么作为本地默认引擎（2026-11-20 i7-6700 无独显真机实测）：
- 磁盘仅 ~228 MB，推理峰值内存 ~473 MB（vs faster-whisper/small ~1GB+、虚拟提交 2.6GB）
- 非自回归 CTC 架构，num_threads=2 下实测 RTF ~0.08（2h 录音约 9-10 分钟，快 faster-whisper ~6x）
- 原生输出简体中文 + 自带标点 + ITN 数字规范化 + 支持普通话/粤语/英文混说
- 超过 max_chunk_seconds 的长块（如离线 300s 块）自动复用纯 NumPy VAD Segmenter 分段解码，
  避免自注意力长序列开销并保留准确的句级时间戳
"""

from __future__ import annotations

import os
import re
import time
from pathlib import Path

import numpy as np

from . import lexicon
from .lexicon import to_simplified
from .transcriber import _collapse_repeats
from .vad import Segmenter

DEFAULT_SENSEVOICE_REPO = "csukuangfj/sherpa-onnx-sense-voice-zh-en-ja-ko-yue-2024-07-17"

_SENTENCE_END_CHARS = set("。！？!?；;\n")
_CLOSING_PUNCT = set("”’」』）】》\"')")
_SPECIAL_TAG_RE = re.compile(r"<\|[^|]+\|>")


def resolve_model_files(model_repo_or_dir: str = DEFAULT_SENSEVOICE_REPO) -> tuple[Path, Path]:
    """解析 SenseVoice INT8 模型文件 (model.int8.onnx, tokens.txt)。

    支持本地目录或 HuggingFace repo id；优先使用本地缓存避免联网阻塞，
    缺失时自动走 hf-mirror 下载（仅拉 INT8 权重和词表，不拉 937MB FP32）。
    """
    os.environ.setdefault("HF_ENDPOINT", "https://hf-mirror.com")

    local_dir = Path(model_repo_or_dir)
    if local_dir.is_dir():
        model_path = local_dir / "model.int8.onnx"
        if not model_path.exists():
            model_path = local_dir / "model.onnx"
        tokens_path = local_dir / "tokens.txt"
        if not model_path.exists() or not tokens_path.exists():
            raise FileNotFoundError(
                f"本地 SenseVoice 目录缺少模型或词表: {local_dir} "
                "(需要 model.int8.onnx 与 tokens.txt)"
            )
        return model_path, tokens_path

    from huggingface_hub import hf_hub_download

    def _get(filename: str) -> Path:
        try:
            return Path(hf_hub_download(model_repo_or_dir, filename, local_files_only=True))
        except Exception:
            return Path(hf_hub_download(model_repo_or_dir, filename))

    return _get("model.int8.onnx"), _get("tokens.txt")


def _split_sentences_proportional(
    text: str, seg_start: float, seg_end: float
) -> list[tuple[float, float, str]]:
    """无对齐 token 时间戳时的兜底按标点分句 + 按字数比例插值时间。"""
    parts: list[str] = []
    buf: list[str] = []
    n = len(text)
    i = 0
    while i < n:
        ch = text[i]
        buf.append(ch)
        if ch in _SENTENCE_END_CHARS:
            while i + 1 < n and text[i + 1] in _CLOSING_PUNCT:
                i += 1
                buf.append(text[i])
            s = "".join(buf).strip()
            if s:
                parts.append(s)
            buf = []
        i += 1
    if buf:
        s = "".join(buf).strip()
        if s:
            parts.append(s)

    if not parts:
        return []
    total_chars = sum(len(p) for p in parts)
    dur = max(0.0, seg_end - seg_start)
    cur = seg_start
    out: list[tuple[float, float, str]] = []
    for idx, p in enumerate(parts):
        if idx == len(parts) - 1 or total_chars <= 0:
            nxt = seg_end
        else:
            nxt = cur + dur * (len(p) / total_chars)
        out.append((cur, max(cur, nxt), p))
        cur = nxt
    return out


def _split_sentences(
    text: str,
    tokens: list[str] | None,
    timestamps: list[float] | None,
    seg_start: float,
    seg_end: float,
) -> list[tuple[float, float, str]]:
    """优先用 token 时间戳按句末标点切句，对齐失败时回退到字数比例插值。"""
    clean_text = _SPECIAL_TAG_RE.sub("", text or "").strip()
    if not clean_text:
        return []

    if (
        tokens
        and timestamps
        and len(tokens) == len(timestamps)
    ):
        joined = "".join(tokens).replace("\u2581", " ").strip()
        # SenseVoice 中文几乎是逐字 token（含标点）；忽略空白一致时直接用精确时间戳
        if re.sub(r"\s+", "", joined) == re.sub(r"\s+", "", clean_text):
            out: list[tuple[float, float, str]] = []
            buf_tok: list[str] = []
            sent_start: float | None = None
            last_ts: float = seg_start
            n = len(tokens)
            i = 0
            while i < n:
                tok = _SPECIAL_TAG_RE.sub("", tokens[i]).replace("\u2581", " ")
                ts = float(timestamps[i])
                if tok and not _SPECIAL_TAG_RE.match(tokens[i]):
                    if sent_start is None and tok.strip():
                        sent_start = seg_start + max(0.0, ts)
                    buf_tok.append(tok)
                    last_ts = seg_start + max(0.0, ts)
                if tok in _SENTENCE_END_CHARS:
                    while i + 1 < n and tokens[i + 1] in _CLOSING_PUNCT:
                        i += 1
                        buf_tok.append(tokens[i])
                        last_ts = seg_start + max(0.0, float(timestamps[i]))
                    s = "".join(buf_tok).strip()
                    if s and sent_start is not None:
                        end_t = min(seg_end, max(sent_start, last_ts + 0.08))
                        out.append((sent_start, end_t, s))
                    buf_tok = []
                    sent_start = None
                i += 1
            if buf_tok:
                s = "".join(buf_tok).strip()
                if s:
                    st = sent_start if sent_start is not None else last_ts
                    out.append((st, max(st, seg_end), s))
            if out:
                return out

    return _split_sentences_proportional(clean_text, seg_start, seg_end)


class SenseVoiceTranscriber:
    """与 Transcriber 保持同接口：transcribe(audio, sample_rate) -> list[dict] 和 stats。"""

    def __init__(
        self,
        model_repo_or_dir: str = DEFAULT_SENSEVOICE_REPO,
        num_threads: int = 2,
        language: str = "auto",
        use_itn: bool = True,
        correct_after: bool = True,
        max_chunk_seconds: float = 28.0,
    ) -> None:
        import sherpa_onnx

        self.model_repo_or_dir = model_repo_or_dir
        self.num_threads = max(1, int(num_threads))
        self.language = language or "auto"
        self.use_itn = bool(use_itn)
        self.correct_after = bool(correct_after)
        self.max_chunk_seconds = float(max_chunk_seconds)

        t0 = time.time()
        print(
            f"[asr] 加载模型 SenseVoice-Small (int8, threads={self.num_threads}, "
            f"lang={self.language})…"
        )
        model_path, tokens_path = resolve_model_files(model_repo_or_dir)
        self.recognizer = sherpa_onnx.OfflineRecognizer.from_sense_voice(
            model=str(model_path),
            tokens=str(tokens_path),
            num_threads=self.num_threads,
            use_itn=self.use_itn,
            language=self.language,
            debug=False,
        )
        print(f"[asr] 模型就绪，耗时 {time.time() - t0:.1f}s")

        self._total_audio = 0.0
        self._total_infer = 0.0

    def _decode_chunk(self, audio: np.ndarray, offset_sec: float) -> list[dict]:
        """解码单个 ≤max_chunk_seconds 的 16kHz 片段，切分成带时间戳的句子列表。"""
        if len(audio) == 0:
            return []
        stream = self.recognizer.create_stream()
        stream.accept_waveform(16000, audio)
        self.recognizer.decode_stream(stream)

        res = stream.result
        raw_text = (getattr(res, "text", "") or "").strip()
        if not raw_text:
            return []

        tokens = list(getattr(res, "tokens", None) or [])
        timestamps = list(getattr(res, "timestamps", None) or [])
        seg_end = offset_sec + len(audio) / 16000.0
        sentences = _split_sentences(raw_text, tokens, timestamps, offset_sec, seg_end)

        out: list[dict] = []
        for s_start, s_end, text in sentences:
            text = to_simplified(text)
            text = _collapse_repeats(text)
            if self.correct_after:
                text = lexicon.correct_text(text)
            if not text:
                continue
            out.append(
                {
                    "start": float(s_start),
                    "end": float(s_end),
                    "text": text,
                    "words": [],
                }
            )
        return out

    def transcribe(self, audio: np.ndarray, sample_rate: int = 16000) -> list[dict]:
        """转写一段音频，返回 [{start, end, text, words}]（时间是片段内相对秒）。"""
        if len(audio) == 0:
            return []

        if sample_rate != 16000:
            import soxr

            audio = soxr.resample(
                np.asarray(audio, dtype=np.float32), sample_rate, 16000, quality="HQ"
            )
            print(f"[asr] 采样率 {sample_rate}≠16000，已自动重采样")

        audio = np.ascontiguousarray(audio, dtype=np.float32)
        dur = len(audio) / 16000.0
        t0 = time.time()

        out: list[dict] = []
        if dur <= self.max_chunk_seconds + 2.0:
            out.extend(self._decode_chunk(audio, 0.0))
        else:
            # 离线长块（如 300s）走纯 NumPy 能量 VAD 切分，既保持低内存又获得段级时间戳
            seg = Segmenter(sample_rate=16000, max_segment_seconds=self.max_chunk_seconds)
            step = 1600  # 0.1s
            for i in range(0, len(audio), step):
                for s in seg.feed(audio[i : i + step]):
                    out.extend(self._decode_chunk(s.audio, s.start_ts))
            tail = seg.flush()
            if tail is not None:
                out.extend(self._decode_chunk(tail.audio, tail.start_ts))

        elapsed = time.time() - t0
        self._total_audio += dur
        self._total_infer += elapsed
        rtf = elapsed / dur if dur else 0.0
        print(f"[asr] {dur:.1f}s 音频 → {len(out)} 句，耗时 {elapsed:.1f}s (RTF={rtf:.2f})")
        return out

    @property
    def stats(self) -> dict:
        return {
            "backend": "sensevoice",
            "model": self.model_repo_or_dir,
            "num_threads": self.num_threads,
            "audio_seconds": round(self._total_audio, 1),
            "infer_seconds": round(self._total_infer, 1),
            "rtf": round(self._total_infer / self._total_audio, 2) if self._total_audio else 0.0,
        }


def check_available(model_repo_or_dir: str = DEFAULT_SENSEVOICE_REPO) -> bool:
    """快速自检：能否加载 SenseVoice 模型。用于环境诊断。"""
    try:
        SenseVoiceTranscriber(model_repo_or_dir=model_repo_or_dir)
        return True
    except Exception as e:  # noqa: BLE001
        print(f"[asr] SenseVoice 自检失败: {e}")
        return False
