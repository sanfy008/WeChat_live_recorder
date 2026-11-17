"""守护脚本：等录制结束 → 自动全量转写 → 后处理。

为什么需要它：
  130 分钟回放如果靠人盯着，不现实。这个脚本轮询录制状态，
  一旦录制进程退出且文件稳定，就自动开始转写。

安全设计：
  - 只认"录制进程不存在 且 文件大小 60 秒无变化"才判定录制结束
  - 转写全程写日志到 output/auto_pipeline.log，出问题可回查
  - 转写产物与手工操作完全一致（同一套 Session/engine）
"""
from __future__ import annotations

import sys
import time
import traceback
from pathlib import Path

import numpy as np
import soundfile as sf

from wxlive.config import (
    RECORDINGS_DIR,
    TRANSCRIPTS_DIR,
    Config,
    ensure_dirs,
    validate_reasoning_effort,
)
from wxlive.pipeline import build_engine
from wxlive.resample import StreamResampler
from wxlive.session import Session
from wxlive import lexicon, postprocess, userconfig, winproc

LOG = Path("output/auto_pipeline.log")
WATCH_PREFIX = "回放130min"


def log(msg: str) -> None:
    line = f"[{time.strftime('%H:%M:%S')}] {msg}"
    print(line, flush=True)
    LOG.parent.mkdir(parents=True, exist_ok=True)
    with LOG.open("a", encoding="utf-8") as f:
        f.write(line + "\n")


def recorder_alive() -> bool:
    """检查是否还有 record_only.py 在跑。"""
    try:
        r = winproc.run(
            ["powershell", "-NoProfile", "-Command",
             "Get-CimInstance Win32_Process -Filter \"Name='python.exe'\" | "
             "Where-Object { $_.CommandLine -like '*record_only.py*' } | "
             "Measure-Object | Select-Object -ExpandProperty Count"],
            capture_output=True, text=True, timeout=25)
        return int((r.stdout or "0").strip() or 0) > 0
    except Exception:
        # 查不出来就当作还在录，避免误判
        return True


def wait_for_recording() -> Path | None:
    """轮询直到录制结束，返回最终文件路径。"""
    log("开始守护：等待录制结束…")
    target = None
    last_size = 0
    stable_since = time.time()
    last_report = time.time()

    while True:
        files = sorted(RECORDINGS_DIR.glob(f"*{WATCH_PREFIX}*.wav"),
                       key=lambda p: p.stat().st_mtime, reverse=True)
        if not files:
            time.sleep(10)
            continue
        target = files[0]
        size = target.stat().st_size
        alive = recorder_alive()

        if size != last_size:
            last_size = size
            stable_since = time.time()

        if time.time() - last_report > 300:  # 每 5 分钟报一次
            log(f"录制中… {size/1024/1024:.0f} MB  进程存活={alive}")
            last_report = time.time()

        # 判定结束：进程不在 且 文件 60 秒无变化
        if not alive and time.time() - stable_since > 60:
            secs = max(0, (size - 44)) / (48000 * 2)
            log(f"✓ 录制结束：{target.name}  {size/1024/1024:.0f} MB  约 {secs/60:.1f} 分钟")
            return target

        time.sleep(15)


def transcribe(src: Path) -> Session:
    ensure_dirs()
    log(f"开始转写 {src.name}")

    audio, sr = sf.read(str(src), dtype="float32", always_2d=False)
    if audio.ndim > 1:
        audio = audio.mean(axis=1)
    dur = len(audio) / sr
    log(f"音频 {dur/60:.1f} 分钟 @ {sr}Hz")

    cfg = userconfig.build_config_from_user(Config())
    try:
        effort = validate_reasoning_effort(cfg.text.reasoning_effort)
    except ValueError as e:
        # 守护脚本无人值守：非法档不能静默接受，记录后退回默认，避免整条链路停摆。
        log(f"配置里的思考强度非法（{e}），改用 low")
        effort = "low"
    text_model = cfg.text.model
    lexicon.apply_loaded(lexicon.load())
    engine = build_engine(cfg)

    fingerprint = f"w{postprocess.DEFAULT_FIX_WORKERS}_{effort}"
    sess = Session(TRANSCRIPTS_DIR, name=src.stem, sample_rate=sr, record_audio=False,
                   audio_stem=src.stem, fingerprint=fingerprint, audio_path=src)
    sess.source_audio_name = src.name

    rs = StreamResampler(in_rate=sr, out_rate=16000)
    block = sr * 300          # 每 5 分钟一块
    total = len(audio)
    n = (total + block - 1) // block
    t0 = time.time()

    for bi, i in enumerate(range(0, total, block)):
        pcm = rs.process(audio[i:i + block])
        if len(pcm) == 0:
            continue
        res = engine.transcribe(pcm, 16000)
        if res:
            sess.add_segments(res, offset=i / sr, expand_codes=True)
        el = time.time() - t0
        done = min(total, i + block) / sr
        eta = (dur - done) / max(1e-6, done / el) / 60 if done else 0
        log(f"  块 {bi+1}/{n}  已出 {len(sess.segments)} 句  用时 {el/60:.1f}min  预计剩余 {eta:.0f}min")

    tail = rs.flush()
    if len(tail):
        res = engine.transcribe(tail, 16000)
        if res:
            sess.add_segments(res, offset=total / sr, expand_codes=True)

    log(f"转写完成：{len(sess.segments)} 句")

    # 后处理 1：LLM 纠错
    fix_stats: dict = {}
    lines = [s["text"] for s in sess.segments]
    if lines:
        log("LLM 纠错中…")
        vocab = "、".join(lexicon.SCHOOLS[:60] + lexicon.TERMS[:40])
        try:
            fixed = postprocess.fix_errors(
                lines, model=text_model,
                endpoint=cfg.asr.gemini_endpoint, api_key=cfg.asr.gemini_api_key,
                vocabulary=vocab, reasoning_effort=effort, out_stats=fix_stats)
            changed = sum(1 for a, b in zip(lines, fixed) if a != b)
            for seg, new in zip(sess.segments, fixed):
                if new and new != seg["text"]:
                    seg["raw_text"] = seg["text"]
                    seg["text"] = new
            log(f"纠错 {changed} 处（重试 {fix_stats.get('retry_count', 0)} 次，"
                f"失败批次 {fix_stats.get('failed_batches', [])}）")
        except Exception as e:  # noqa: BLE001
            log(f"纠错失败（保留原文）: {e}")
            fix_stats.setdefault("error", str(e)[:150])

    # 后处理 2：生成要点
    digest = ""
    digest_stats: dict = {}
    if sess.segments:
        log("生成要点中…")
        try:
            digest = postprocess.build_digest(
                sess.segments, model=text_model,
                endpoint=cfg.asr.gemini_endpoint, api_key=cfg.asr.gemini_api_key,
                title=src.stem, reasoning_effort=effort, out_stats=digest_stats)
        except Exception as e:  # noqa: BLE001
            log(f"要点生成失败: {e}")
            digest = f"(要点生成失败: {e})"
            digest_stats["ok"] = False
            digest_stats["error"] = str(e)[:150]

    text_meta = {
        "model": text_model,
        "reasoning_effort": effort,
        "fix": fix_stats,
        "digest": digest_stats,
    }
    doc = sess.close(extra_meta={"engine": "auto_pipeline",
                                 "asr": getattr(engine, "stats", {}),
                                 "text": text_meta})
    if digest:
        md = sess.md_path
        md.write_text(md.read_text(encoding="utf-8") + "\n\n---\n\n# AI 整理要点\n\n" + digest + "\n",
                      encoding="utf-8")
    log(f"✓ 全部完成：{sess.md_path.name}  共 {len(doc['segments'])} 句")
    return sess


def main() -> int:
    try:
        src = wait_for_recording()
        if src is None:
            log("未找到录制文件，退出")
            return 1
        sess = transcribe(src)
        log(f"产物：{sess.md_path}")
        log(f"      {sess.json_path}")
        log("老环境 .venv 现在可以清理了")
        return 0
    except Exception:
        log("守护脚本异常：")
        log(traceback.format_exc())
        return 1


if __name__ == "__main__":
    sys.exit(main())
