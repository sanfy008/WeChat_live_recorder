"""等录制停止 → 自动转写 → 纠错 → 要点（流式读盘，不吃满内存）。

与 auto_pipeline.py 的区别（为什么不直接用它）：
  1. 它按文件名前缀 WATCH_PREFIX 找文件，写死了 "回放130min"；这里接受显式路径。
  2. 它用 sf.read() 整段读入内存。48k 单声道 130 分钟 = 1.5GB float32，
     叠加 whisper + 并发转写容易把内存压到危险区。这里按 300s 块流式读。
  3. 它的 recorder_alive() 只认 record_only.py，GUI 录制会永远判"还活着"。

判定录制结束（两个独立信号，任一成立）：
  A. WAV 头被回填（riff size == 文件大小-8 且 data 长度>0）—— 只有正常 close() 才这样
  B. 文件大小连续 STABLE_SECONDS 秒不变 —— GUI 被强杀时走这条

用法：
  python tools/auto_transcribe_after_stop.py <wav路径> [--stable 90] [--max-hours 6]
"""
from __future__ import annotations

import argparse
import os
import struct
import sys
import time
import traceback
from pathlib import Path

import numpy as np

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

# Windows 控制台默认 GBK，日志里的 ✓ 会抛 UnicodeEncodeError 直接中断整条链路。
# 统一改成 utf-8 + replace，编码问题永不影响转写。
for _s in (sys.stdout, sys.stderr):
    try:
        _s.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass


# 头部扫描窗口上限。原先只读 4096 字节：带大 LIST/INFO 元数据的 WAV，
# data 块可能落在窗口外，函数会误判“没有 data 块 = 已收尾”，
# 导致录制中途就开始转写。
MAX_HEADER_SCAN = 1024 * 1024


def header_closed(p: Path) -> bool:
    try:
        size = p.stat().st_size
        scan = min(size, MAX_HEADER_SCAN)
        with open(p, "rb") as f:
            head = f.read(scan)
    except OSError:
        return False
    if len(head) < 44 or head[:4] != b"RIFF" or head[8:12] != b"WAVE":
        return False
    riff = struct.unpack("<I", head[4:8])[0]
    i = 12
    while i + 8 <= len(head):
        cid = head[i:i + 4]
        csz = struct.unpack("<I", head[i + 4:i + 8])[0]
        if cid == b"data":
            return csz > 0 and riff == size - 8
        i += 8 + csz + (csz & 1)
    # 只有把整个文件都扫完仍没看到 data 块，才能断言“已收工”。
    # 若只是窗口用完，必须返回 False，交给“大小稳定”信号判断。
    return scan >= size


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("wav")
    ap.add_argument("--stable", type=float, default=90, help="文件大小稳定多少秒算停止")
    ap.add_argument("--max-hours", type=float, default=6.0)
    ap.add_argument("--poll", type=float, default=15)
    ap.add_argument("--engine", choices=["auto", "gemini", "local"], default=None,
                    help="覆盖 user.json 中的 ASR 引擎选择")
    ap.add_argument("--local-backend", choices=["sensevoice", "whisper"], default=None,
                    help="覆盖本地 ASR 后端（默认 sensevoice）")
    args = ap.parse_args()

    src = Path(args.wav)
    LOG = ROOT / "output" / f"auto_transcribe_{src.stem}.log"

    def log(msg: str) -> None:
        line = f"[{time.strftime('%H:%M:%S')}] {msg}"
        # pythonw 下 sys.stdout 是 None，直接 print 会 AttributeError 打断整条链路。
        try:
            if sys.stdout is not None:
                print(line, flush=True)
        except Exception:
            pass
        LOG.parent.mkdir(parents=True, exist_ok=True)
        with LOG.open("a", encoding="utf-8") as f:
            f.write(line + "\n")

    log(f"守护开始，目标 {src}")

    # ---------- 1. 等待录制结束 ----------
    t0 = time.time()
    last_size, stable_since, last_report = -1, time.time(), 0.0
    while True:
        if not src.exists():
            time.sleep(args.poll)
            continue
        size = src.stat().st_size
        if size != last_size:
            last_size, stable_since = size, time.time()
        stable = time.time() - stable_since
        elapsed = time.time() - t0

        if time.time() - last_report > 600:
            log(f"录制中 {size/1048576:.0f}MB 稳定{stable:.0f}s 已等{elapsed/60:.0f}min")
            last_report = time.time()

        if header_closed(src):
            log(f"✓ 检测到 WAV 已正常收尾（信号A），大小 {size/1048576:.0f}MB")
            break
        if stable >= args.stable and size > 44:
            log(f"✓ 文件 {stable:.0f}s 无变化（信号B），判定录制结束 {size/1048576:.0f}MB")
            break
        if elapsed > args.max_hours * 3600:
            log("✗ 等待超时，退出")
            return 2
        time.sleep(args.poll)

    time.sleep(3)

    # ---------- 2. 转写 ----------
    try:
        return _transcribe(src, log, engine_override=args.engine,
                           local_backend_override=args.local_backend)
    except Exception:
        log("转写异常：\n" + traceback.format_exc())
        return 1


def format_asr_summary(cfg) -> str:
    """按实际生效的 engine 格式化 ASR 启动日志，避免 local 模式仍打印 Gemini 模型名。"""
    kind = (cfg.asr.engine or "local").strip().lower()
    backend = (getattr(cfg.asr, "local_backend", "sensevoice") or "sensevoice").strip().lower()
    if backend == "sensevoice":
        threads = getattr(cfg.asr, "local_threads", 2)
        local_desc = f"sensevoice-small-int8 (cpu/int8, {threads}t)"
    else:
        local_desc = f"faster-whisper/{cfg.asr.model_size} ({cfg.asr.device}/{cfg.asr.compute_type})"
    if kind == "local":
        return f"{local_desc} [engine=local]"
    gemini_desc = f"{cfg.asr.gemini_model} / workers={cfg.asr.gemini_workers}"
    if kind == "gemini":
        return f"{gemini_desc} [engine=gemini]"
    return f"{gemini_desc}（兜底 {local_desc}） [engine={kind}]"


def _transcribe(src: Path, log, engine_override: str | None = None,
                local_backend_override: str | None = None) -> int:
    import soundfile as sf

    from wxlive import lexicon, postprocess, userconfig
    from wxlive.config import Config, TRANSCRIPTS_DIR, ensure_dirs, validate_reasoning_effort
    from wxlive.pipeline import build_engine
    from wxlive.resample import StreamResampler
    from wxlive.session import Session

    ensure_dirs()
    info = sf.info(str(src))
    sr = int(info.samplerate)
    total = int(info.frames)
    dur = total / sr if sr else 0.0
    log(f"音频 {dur/60:.1f} 分钟 @ {sr}Hz（{total} 帧）")
    # 帧数为 0 基本只有两种可能：空文件，或录制被强杀后 WAV 头没回填。
    # 继续跑会生成空产物并打印“✓ 全部完成”，是本项目反复踩过的假成功。
    if sr <= 0 or total <= 0 or dur <= 0:
        log(f"✗ 音频帧数为 0（total={total}, sr={sr}）：文件可能未正常收尾，"
            f"拒绝生成空产物")
        return 3

    cfg = userconfig.build_config_from_user(Config())
    if engine_override:
        cfg.asr.engine = engine_override
    if local_backend_override:
        cfg.asr.local_backend = local_backend_override
    # 尊重 user.json 中的 gemini_workers（未配置时默认沿用 AsrConfig.gemini_workers）。
    try:
        effort = validate_reasoning_effort(cfg.text.reasoning_effort)
    except ValueError as e:
        log(f"配置思考强度非法（{e}），退回 low")
        effort = "low"
    text_model = cfg.text.model
    log(f"ASR 模型 {format_asr_summary(cfg)}；文本模型 {text_model} / effort={effort}")

    engine = build_engine(cfg)

    fingerprint = f"w{postprocess.DEFAULT_FIX_WORKERS}_{effort}"
    sess = Session(TRANSCRIPTS_DIR, name=src.stem, sample_rate=sr, record_audio=False,
                   audio_stem=src.stem, fingerprint=fingerprint, audio_path=src)
    sess.source_audio_name = src.name
    log(f"会话 {sess.id} → {sess.stem_dir}")

    rs = StreamResampler(in_rate=sr, out_rate=16000)
    block = sr * 300                       # 每块 5 分钟
    n = (total + block - 1) // block
    t0 = time.time()
    skipped = 0

    with sf.SoundFile(str(src)) as f:
        for bi in range(n):
            piece = f.read(block, dtype="float32", always_2d=False)
            if piece is None or len(piece) == 0:
                break
            if piece.ndim > 1:
                piece = piece.mean(axis=1)
            pcm = rs.process(piece)
            if len(pcm) == 0:
                continue
            res = engine.transcribe(pcm, 16000)
            if res:
                # offset 用块的真实起点（秒），不假设块长恰好 300s
                sess.add_segments(res, offset=(bi * block) / sr, expand_codes=True)
            el = time.time() - t0
            done = min(total, (bi + 1) * block) / sr
            speed = done / el if el else 0
            eta = (dur - done) / speed / 60 if speed else 0
            log(f"  块 {bi+1}/{n}  {done/60:.0f}/{dur/60:.0f}min  "
                f"{len(sess.segments)}句  用时 {el/60:.1f}min  剩余约 {eta:.0f}min")

    tail = rs.flush()
    if len(tail):
        res = engine.transcribe(tail, 16000)
        if res:
            sess.add_segments(res, offset=total / sr, expand_codes=True)

    stats = getattr(engine, "stats", {}) or {}
    log(f"转写完成 {len(sess.segments)} 句；"
        f"失败块 {stats.get('failed_chunk_count', 0)}，内容损失={stats.get('has_content_loss')}")

    # 完整性自检：末句时间 vs 音频时长（MEMORY 里的教训：文件存在≠内容完整）
    last_ts = sess.segments[-1]["end"] if sess.segments else 0.0
    gap = dur - float(last_ts)
    log(f"完整性：末句 {last_ts:.1f}s / 音频 {dur:.1f}s，差 {gap:.1f}s")

    # ---------- 3. 纠错 ----------
    fix_stats: dict = {}
    lines = [s["text"] for s in sess.segments]
    changed = 0
    if lines:
        log("LLM 纠错中…")
        try:
            lexicon.apply_loaded(lexicon.load(
                Path(cfg.asr.lexicon_path) if cfg.asr.lexicon_path else None))
            vocab = "、".join(lexicon.SCHOOLS[:60] + lexicon.TERMS[:40])
        except Exception:
            vocab = ""
        try:
            fixed = postprocess.fix_errors(
                lines, model=text_model,
                endpoint=cfg.asr.gemini_endpoint, api_key=cfg.asr.gemini_api_key,
                vocabulary=vocab, reasoning_effort=effort, out_stats=fix_stats)
            changed = sum(1 for a, b in zip(lines, fixed) if a and b and a != b)
            for seg, new in zip(sess.segments, fixed):
                if new and new != seg["text"]:
                    # 不覆盖已有的 raw_text（展开前的原始文本）；
                    # 原始 ASR 文本另有 asr_text 字段，纠错不得改动。
                    if not seg.get("raw_text"):
                        seg["raw_text"] = seg["text"]
                    seg["text"] = new
            log(f"纠错 {changed} 处（重试 {fix_stats.get('retry_count', 0)}，"
                f"失败批 {fix_stats.get('failed_batches', [])}）")
        except Exception as e:
            log(f"纠错失败（保留原文）: {e}")
            fix_stats["error"] = str(e)[:200]

    # ---------- 4. 要点 ----------
    digest, digest_stats = "", {}
    if sess.segments:
        log("生成要点中…")
        try:
            digest = postprocess.build_digest(
                sess.segments, model=text_model,
                endpoint=cfg.asr.gemini_endpoint, api_key=cfg.asr.gemini_api_key,
                title=src.stem, reasoning_effort=effort, out_stats=digest_stats)
        except Exception as e:
            log(f"要点生成失败: {e}")
            digest_stats = {"ok": False, "error": str(e)[:200]}

    text_meta = {"model": text_model, "reasoning_effort": effort,
                 "fix": fix_stats, "digest": digest_stats}
    from wxlive.quality import compute_completeness

    # tail gap ≥120s 等同于“收尾没跑完”；其余丢失来源由统一判定汇总。
    completeness = compute_completeness(
        drain_incomplete=gap >= 120,
        has_content_loss=bool(stats.get("has_content_loss")),
    )
    if not completeness["complete"]:
        log(f"⚠ 产物不完整：{'；'.join(completeness['reasons'])}")
    doc = sess.close(extra_meta={
        "engine": "auto_transcribe_after_stop",
        "asr": stats,
        "text": text_meta,
        "audio_seconds": round(dur, 1),
        "last_segment_end": round(float(last_ts), 1),
        "tail_gap_seconds": round(gap, 1),
        "transcript_complete": bool(sess.segments) and completeness["complete"],
        "completeness_reasons": completeness["reasons"],
    })
    if digest:
        md = sess.md_path
        md.write_text(md.read_text(encoding="utf-8") +
                      "\n\n---\n\n# AI 整理要点\n\n" + digest + "\n", encoding="utf-8")

    log(f"✓ 全部完成：{sess.md_path}")
    log(f"          {sess.json_path}")
    log(f"          共 {len(doc['segments'])} 句，纠错 {changed} 处，"
        f"总用时 {(time.time()-t0)/60:.1f} 分钟")
    return 0


if __name__ == "__main__":
    sys.exit(main())
