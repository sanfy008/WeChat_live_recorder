"""会话输出：边录边写，保证任何时刻崩溃都不丢已转写的内容。

三份产物：
1. `<session>.wav`        原始音频（不贬值资产）
2. `<session>.jsonl`      逐句流式追加，崩溃安全
3. `<session>.md`         人读版逐字稿，每句完成即重写
4. `<session>.json`       结束时生成，含完整 segments 结构，供 LLM 消费
"""

from __future__ import annotations

import json
import time
from datetime import datetime, timedelta
from pathlib import Path

import numpy as np
import soundfile as sf


def _ts(seconds: float) -> str:
    """秒 → HH:MM:SS"""
    seconds = max(0, int(seconds))
    h, rem = divmod(seconds, 3600)
    m, s = divmod(rem, 60)
    return f"{h:02d}:{m:02d}:{s:02d}"


class Session:
    def __init__(
        self,
        base_dir: Path,
        name: str = "",
        sample_rate: int = 16000,
        record_audio: bool = True,
    ) -> None:
        """record_audio=False 用于"转写已有音频"场景：不新建 WAV 文件，
        避免产生一个 44 字节的空壳音频误导人。"""
        self.sample_rate = sample_rate
        self.started_at = datetime.now()
        stamp = self.started_at.strftime("%Y%m%d_%H%M%S")
        safe = "".join(c for c in (name or "wechat_live") if c.isalnum() or c in "-_") or "wechat_live"
        self.id = f"{stamp}_{safe}"

        base_dir.mkdir(parents=True, exist_ok=True)
        self.audio_path = base_dir / f"{self.id}.wav"
        self.jsonl_path = base_dir / f"{self.id}.jsonl"
        self.md_path = base_dir / f"{self.id}.md"
        self.json_path = base_dir / f"{self.id}.json"

        self._wav: sf.SoundFile | None = None
        if record_audio and not self.audio_path.exists():
            self._wav = sf.SoundFile(
                str(self.audio_path), mode="w",
                samplerate=sample_rate, channels=1, subtype="PCM_16",
            )
        if not record_audio:
            self.audio_path = None  # type: ignore[assignment]

        # 转写模式下记录源音频名，用于在逐字稿里标注出处
        self.source_audio_name = ""

        self.segments: list[dict] = []
        self._jsonl = self.jsonl_path.open("a", encoding="utf-8")
        self._last_md_write = 0.0
        self._header_written = False

    def _audio_name(self) -> str:
        """记录的音频：录制模式是本会话 WAV，转写模式是源文件名。"""
        if self.audio_path:
            return self.audio_path.name
        return self.source_audio_name or "（外部音频）"

    # ---------- 写入 ----------

    def write_audio(self, chunk: np.ndarray) -> None:
        if self._wav is not None and len(chunk):
            self._wav.write(np.clip(chunk, -1.0, 1.0))

    def add_segments(self, segs: list[dict], offset: float = 0.0,
                     expand_codes: bool = False) -> None:
        """segs 里的时间是相对片段起点的秒数，offset 是片段在会话中的起始秒。"""
        # 首次写入前才生成文件头：此时 source_audio_name 已由调用方设置好
        if not self._header_written:
            self._write_md_header()

        if expand_codes:
            from . import lexicon

        for s in segs:
            text = s["text"]
            if expand_codes and text:
                text = lexicon.expand_codes(text, keep_original=True)
            rec = {
                "start": round(offset + s["start"], 2),
                "end": round(offset + s["end"], 2),
                "text": text,
                "raw_text": s["text"] if text != s["text"] else None,
                # 句级时间戳的可信度：云端 Gemini 不返回时间，是按字数摊的估算值
                # （实测中位偏差 19.3s）；本地 whisper 有真实对齐。
                # 显式标出来，避免下游把它当精确值用。
                "ts_source": s.get("ts_source", "exact"),
                "speaker": None,  # 预留：后续做说话人分离时填 S1/S2
            }
            self.segments.append(rec)
            self._jsonl.write(json.dumps(rec, ensure_ascii=False) + "\n")
        self._jsonl.flush()
        self._append_md(segs, offset)

    def _write_md_header(self) -> None:
        self._header_written = True
        self.md_path.write_text(
            f"# 微信直播逐字稿\n\n"
            f"- 会话 ID：`{self.id}`\n"
            f"- 开始时间：{self.started_at.strftime('%Y-%m-%d %H:%M:%S')}\n"
            f"- 音频文件：`{self._audio_name()}`\n\n"
            f"---\n\n",
            encoding="utf-8",
        )

    def _append_md(self, segs: list[dict], offset: float) -> None:
        if not segs:
            return
        now = time.time()
        # 每 2 秒最多重写一次，避免高频 IO
        if now - self._last_md_write < 2.0:
            return
        self._last_md_write = now
        # 实时追加仍用逐句（尚无完整上下文，无法可靠断段）；
        # 收尾时 close() 会按段落整体重写
        lines = [f"**[{_ts(offset + s['start'])}]** {s['text']}\n" for s in segs]
        with self.md_path.open("a", encoding="utf-8") as f:
            f.write("\n".join(lines) + "\n")

    # ---------- 收尾 ----------

    def close(self, extra_meta: dict | None = None) -> dict:
        if self._wav is not None:
            self._wav.close()
            self._wav = None
        self._jsonl.close()

        ended_at = datetime.now()
        doc = {
            "source": "wechat_channels",
            "session_id": self.id,
            "started_at": self.started_at.isoformat(timespec="seconds"),
            "ended_at": ended_at.isoformat(timespec="seconds"),
            "duration_seconds": _audio_duration(ended_at - self.started_at, self.segments),
            "audio_file": self._audio_name(),
            "segments": self.segments,
            "full_text": "".join(s["text"] for s in self.segments),
            "meta": {
                "sample_rate": self.sample_rate,
                "segment_count": len(self.segments),
                **(extra_meta or {}),
            },
        }
        self.json_path.write_text(json.dumps(doc, ensure_ascii=False, indent=2), encoding="utf-8")

        # 完整重写 md（补齐可能因节流漏掉的句子）
        # 说明：md 是"给人看 / 给 LLM 分析"的产物，用段落聚合渲染。
        # json / jsonl 保持逐句 + 原始时间戳，不受影响 —— 精确回查和
        # 将来的说话人分离都靠它们。
        from . import render as _render

        groups = _render.group_segments(self.segments)
        body = _render.render_paragraphs(self.segments)
        self.md_path.write_text(
            f"# 微信直播逐字稿\n\n"
            f"- 会话 ID：`{self.id}`\n"
            f"- 开始时间：{self.started_at.strftime('%Y-%m-%d %H:%M:%S')}\n"
            f"- 时长：{_ts(doc['duration_seconds'])}\n"
            f"- 句数：{len(self.segments)}\n"
            f"- 段落：{len(groups)}\n"
            f"- 音频文件：`{self._audio_name()}`\n\n"
            f"> 本文件为段落聚合版（按直播转折话术断段），便于阅读与分析。\n"
            f"> 需要精确到句和时间戳，请查同名 `.json`。\n\n"
            f"---\n\n{body}\n",
            encoding="utf-8",
        )
        return doc


def _audio_duration(wall: "timedelta", segments: list[dict]) -> float:
    """音频时长（秒），而非墙钟耗时。

    墙钟差包含转写/纠错所花的时间，用它当"时长"会严重偏小
    （实测 66.5 分钟音频被记成 33.7 分钟）。优先取末段 end；
    无分段时退回墙钟差。
    """
    if segments:
        last_end = 0.0
        for seg in segments:
            try:
                last_end = max(last_end, float(seg.get("end") or 0.0))
            except (TypeError, ValueError):
                continue
        if last_end > 0:
            return round(last_end, 1)
    return round(wall.total_seconds(), 1)

