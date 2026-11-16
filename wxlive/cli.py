"""命令行入口。

用法：
    python -m wxlive devices                  # 列出可录制的回环设备
    python -m wxlive doctor                   # 环境自检
    python -m wxlive record --minutes 30      # 录 30 分钟并实时转写
    python -m wxlive record --until-ctrl-c    # 一直录到 Ctrl+C
    python -m wxlive transcribe a.wav         # 把已有音频转成逐字稿
"""

from __future__ import annotations

import argparse
import sys
import time
from pathlib import Path

from . import _force_utf8  # noqa: F401  (导入即生效)
from .config import (
    OUTPUT_DIR,
    RECORDINGS_DIR,
    TRANSCRIPTS_DIR,
    AsrConfig,
    AudioConfig,
    Config,
    VadConfig,
    ensure_dirs,
)


def cmd_gui(args: argparse.Namespace) -> int:
    from .gui import main as gui_main
    return gui_main()


def cmd_devices(args: argparse.Namespace) -> int:
    from .audio_capture import list_loopback_devices, pick_device

    print("可用的 WASAPI 回环（Loopback）设备：\n")
    devs = list_loopback_devices()
    if not devs:
        print("未找到任何回环设备。")
        return 1
    default = pick_device("")
    for d in devs:
        mark = " ← 默认" if d.index == default.index else ""
        print(f"  [{d.index:2d}] {d.name}  ({d.rate}Hz, {d.channels}ch){mark}")
    print("\n提示：用 --device <关键字> 指定，例如 --device Realtek")
    return 0


def cmd_doctor(args: argparse.Namespace) -> int:
    print("=" * 58)
    print("环境自检")
    print("=" * 58)

    ok = True

    # 1. 回环设备
    print("\n[1/4] 回环音频设备")
    try:
        from .audio_capture import pick_device

        dev = pick_device(args.device or "")
        print(f"       OK  {dev.name} @ {dev.rate}Hz {dev.channels}ch")
    except Exception as e:  # noqa: BLE001
        print(f"       FAIL {e}")
        ok = False

    # 2. 录音实测
    print("\n[2/4] 录音链路（采集 3 秒，检测是否为静音）")
    try:
        import numpy as np

        from .audio_capture import LoopbackRecorder

        rec = LoopbackRecorder(device_hint=args.device or "", chunk_seconds=0.1)
        rec.start()
        chunks = []
        t0 = time.time()
        while time.time() - t0 < 3:
            c = rec.read(timeout=0.3)
            if len(c):
                chunks.append(c)
        rec.stop()
        audio = np.concatenate(chunks) if chunks else np.zeros(0, np.float32)
        rms = float(np.sqrt(np.mean(audio**2))) if len(audio) else 0.0
        if rms > 1e-4:
            print(f"       OK  采到 {len(audio)/rec.device.rate:.1f}s 音频，RMS={rms:.5f}")
        else:
            print("       WARN 采到音频但全程静音 —— 录屏时请确认有声音在播放")
    except Exception as e:  # noqa: BLE001
        print(f"       FAIL {e}")
        ok = False

    # 3. 本地 ASR
    print(f"\n[3/5] 本地转写引擎（tiny 自检，配置 {args.model}）")
    try:
        from .transcriber import Transcriber

        t0 = time.time()
        Transcriber(model_size="tiny", device="cpu", compute_type="int8")
        print(f"       OK  模型可加载（{time.time() - t0:.1f}s）")
    except Exception as e:  # noqa: BLE001
        print(f"       FAIL {e}")
        ok = False

    # 4. 云端 Gemini 通道（可选，失败不致命）
    print(f"\n[4/5] 云端 Gemini 通道（{args.gemini_model}）")
    try:
        import base64
        import io
        import json
        import urllib.request

        import numpy as np
        import soundfile as sf

        from .config import AsrConfig as _A

        ac = _A()
        buf = io.BytesIO()
        sf.write(buf, np.zeros(16000, dtype=np.float32), 16000, format="WAV", subtype="PCM_16")
        payload = {
            "model": args.gemini_model,
            "messages": [{"role": "user", "content": [
                {"type": "text", "text": "回复 OK 两个字即可"},
                {"type": "input_audio", "input_audio": {
                    "data": base64.b64encode(buf.getvalue()).decode(), "format": "wav"}}]}],
            "max_tokens": 50,
            "reasoning_effort": "low",
        }
        req = urllib.request.Request(
            ac.gemini_endpoint, data=json.dumps(payload).encode(),
            headers={"Authorization": f"Bearer {ac.gemini_api_key}",
                     "Content-Type": "application/json"})
        t0 = time.time()
        with urllib.request.urlopen(req, timeout=90) as r:  # noqa: S310
            d = json.loads(r.read().decode())
        reply = (d["choices"][0]["message"].get("content") or "").strip()[:40]
        print(f"       OK  通道可用（{time.time()-t0:.1f}s）响应: {reply}")
    except Exception as e:  # noqa: BLE001
        print(f"       WARN 云端不可用，将自动降级本地: {str(e)[:100]}")

    # 4. 输出目录
    print("\n[5/5] 输出目录")
    try:
        ensure_dirs()
        print(f"       OK  {OUTPUT_DIR}")
    except Exception as e:  # noqa: BLE001
        print(f"       FAIL {e}")
        ok = False

    print("\n" + "=" * 58)
    print("结论：" + ("全部通过，可以开始录制" if ok else "存在问题，请按上面 FAIL 项排查"))
    return 0 if ok else 1


def cmd_stream(args: argparse.Namespace) -> int:
    """流地址直录（不转写），产出 16k 单声道 WAV。"""
    from .config import RECORDINGS_DIR, ensure_dirs
    from .stream_capture import StreamRecorder, is_url, probe_url

    if not is_url(args.url):
        print("不是有效的流地址: " + args.url)
        return 1

    print("探测 " + args.url[:80] + " ...")
    r = probe_url(args.url)
    if not r.get("ok"):
        print("探测失败: " + str(r.get("error")))
        return 1
    print("可拉流，格式 " + str(r.get("format", "?")) +
          "，含音轨: " + str(r.get("has_audio")))

    ensure_dirs()
    rec = StreamRecorder(args.url, RECORDINGS_DIR,
                         name=args.name or "stream", log_cb=print)
    rec.start()

    print("开始拉流录音，Ctrl+C 停止...")
    try:
        while rec.running:
            time.sleep(2)
            print("\r  " + rec.status(), end="", flush=True)
    except KeyboardInterrupt:
        print("")
        print("收到中断")
    finally:
        rec.stop()

    print("")
    print("已保存: " + str(rec.out_path))
    if rec.out_path:
        print("下一步转写:")
        print('  python -m wxlive transcribe "' + str(rec.out_path) + '"')
    return 0



def cmd_record(args: argparse.Namespace) -> int:
    from .pipeline import run_pipeline

    cfg = Config(
        audio=AudioConfig(device_hint=args.device or ""),
        vad=VadConfig(),
        asr=AsrConfig(
            engine=args.engine,
            gemini_model=args.gemini_model,
            use_lexicon=not args.no_lexicon,
            lexicon_path=args.lexicon_path,
            model_size=args.model,
            device=args.device_type,
            compute_type=args.compute_type,
            language=args.language,
            gemini_workers=getattr(args, "workers", 4),
        ),
    )
    cfg.session_name = args.name or ""

    max_seconds = 0.0
    if args.minutes:
        max_seconds = args.minutes * 60

    print(f"模型: {args.model} | 语言: {args.language} | "
          f"时长: {'Ctrl+C 结束' if not max_seconds else f'{args.minutes} 分钟'}")
    doc = run_pipeline(cfg, max_seconds=max_seconds)
    print(f"\n共 {len(doc['segments'])} 句，时长 {doc['duration_seconds']}s")
    return 0


def cmd_fetch(args: argparse.Namespace) -> int:
    """从视频号 API 取址 → 解密下载 → 抽音频（可选直接转写）。

    前提：本地抓流工具在跑，且已在微信里打开过目标视频（让工具捕获到）。
    """
    from .config import RECORDINGS_DIR, ensure_dirs
    from . import fetch as F

    try:
        if args.list:
            items = F.list_contents(args.api)
            if not items:
                print("工具里还没有捕获到任何内容。")
                print("请先在微信里打开目标视频，让抓流工具捕获到它。")
                return 1
            print(f"已捕获 {len(items)} 条：\n")
            for i, it in enumerate(items, 1):
                print(f"  {i}. 《{it.title}》")
                print(f"     id={it.content_id}")
            return 0

        if not args.target:
            print("请给出标题关键词或 id（或用 --list 查看）")
            return 1

        item = F.find_content(args.target, args.api)
        print(f"目标：《{item.title}》")
        print(f"id  ：{item.content_id}")

        key = F.get_decrypt_key(item, args.api)
        flag, enc_len = F.probe_encryption(item.url)
        print(f"加密：flag={flag} enc_len={enc_len}  key={'已取到' if key else '无'}")
        if flag and not key:
            print("文件已加密但没拿到密钥，中止")
            return 1

        ensure_dirs()
        stem = F.safe_filename(args.name or item.title)

        if args.no_video:
            print("下载中（不保留视频）…")
        else:
            print("下载中…")
        last = [0.0]

        def prog(done: int, secs: float) -> None:
            mb = done / 1048576
            now = time.time()
            if now - last[0] >= 2:
                last[0] = now
                rate = mb / max(1e-6, secs)
                print(f"  {mb:6.1f} MB  {rate:5.1f} MB/s", flush=True)

        if args.no_video:
            # 直接流式抽音频：先落临时文件避免 moov seek 问题不可行，
            # 因此仍先下完整文件，最后删掉。
            tmp = Path(RECORDINGS_DIR) / (stem + ".mp4")
            video = F.download_decrypted(item, key, tmp, on_progress=prog)
            wav = F.extract_audio(video, Path(RECORDINGS_DIR) / (stem + ".wav"))
            video.unlink(missing_ok=True)
            print(f"已删除临时视频，保留音频：{wav.name}")
        else:
            video = F.download_decrypted(
                item, key, Path(RECORDINGS_DIR) / (stem + ".mp4"), on_progress=prog)
            print(f"视频：{video}  ({video.stat().st_size/1048576:.1f} MB)")
            wav = F.extract_audio(video, Path(RECORDINGS_DIR) / (stem + ".wav"))
            print(f"音频：{wav}  ({wav.stat().st_size/1048576:.1f} MB)")

        dur, has_audio = F.probe_media(wav)
        print(f"时长：{dur/60:.1f} 分钟，含音轨：{has_audio}")

        if args.transcribe:
            print("\n开始转写…")
            ns = argparse.Namespace(
                input=str(wav), engine=args.engine,
                gemini_model=args.gemini_model, no_lexicon=args.no_lexicon,
                lexicon_path=args.lexicon_path, model=args.model,
                device_type=args.device_type, compute_type=args.compute_type,
                language=args.language, workers=getattr(args, "workers", 4),
            )
            return cmd_transcribe(ns)
        return 0
    except F.FetchError as exc:
        print(f"失败：{exc}")
        return 1


def cmd_transcribe(args: argparse.Namespace) -> int:
    """把已有音频文件转成逐字稿（离线补转，或换模型重转）。"""
    import numpy as np
    import soundfile as sf

    from .session import Session
    from .transcriber import Transcriber

    src = Path(args.input)
    if not src.exists():
        print(f"文件不存在: {src}")
        return 1

    print(f"读取 {src.name}…")
    audio, sr = sf.read(str(src), dtype="float32", always_2d=False)
    if audio.ndim > 1:
        audio = audio.mean(axis=1)
    print(f"时长 {len(audio)/sr:.1f}s，采样率 {sr}Hz")

    # 与 record 共用引擎选择逻辑，同样支持 cloud/local/auto 自动降级
    from .config import Config as _Cfg
    from .pipeline import build_engine

    tr = build_engine(_Cfg(asr=AsrConfig(
        engine=args.engine,
        gemini_model=args.gemini_model,
        use_lexicon=not args.no_lexicon,
        lexicon_path=args.lexicon_path,
        model_size=args.model,
        device=args.device_type,
        compute_type=args.compute_type,
        language=args.language,
    )))

    ensure_dirs()
    # record_audio=False：源音频已存在，不再新建 WAV（否则会留下一个 44 字节空壳）
    sess = Session(TRANSCRIPTS_DIR, name=src.stem, sample_rate=sr, record_audio=False)
    sess.source_audio_name = src.name

    # 必须先重采样到 16kHz：whisper 只接受 16k，
    # 直接喂 48k 会把它当慢放处理，输出全是幻觉（本项目踩过的坑）。
    from .resample import StreamResampler

    rs = StreamResampler(in_rate=sr, out_rate=16000)
    if sr != 16000:
        print(f"重采样 {sr}Hz → 16000Hz …")

    block = sr * 300
    total = len(audio)
    for i in range(0, total, block):
        piece = audio[i : i + block]
        print(f"\n处理 {i/sr:.0f}s - {min(total, i+block)/sr:.0f}s …")
        pcm16 = rs.process(piece)
        if len(pcm16) == 0:
            continue
        res = tr.transcribe(pcm16, 16000)
        if res:
            sess.add_segments(res, offset=i / sr, expand_codes=not args.no_lexicon)

    # 取回重采样器尾部残余样本，避免最后半句被截掉。
    # 注意：soxr 有时返回 0 样本，必须显式判空 ——
    # Gemini 引擎对空输入不早退，会拿空音频去问模型，导致凭空生成大量句子。
    tail = rs.flush()
    if len(tail):
        res = tr.transcribe(tail, 16000)
        if res:
            sess.add_segments(res, offset=total / sr, expand_codes=not args.no_lexicon)

    doc = sess.close(extra_meta={"asr": tr.stats, "source_file": src.name})
    print(f"\n完成：{sess.md_path}")
    print(f"      {sess.json_path}")
    print(f"      共 {len(doc['segments'])} 句")
    return 0


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="wxlive",
        description="微信直播录音 → 转写 → 结构化文本",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    sub = p.add_subparsers(dest="cmd", required=True)

    def add_asr_opts(sp: argparse.ArgumentParser) -> None:
        sp.add_argument("--engine", default="auto", choices=["auto", "local", "gemini"],
                        help="转写引擎：auto=Gemini优先失败降级本地(默认) / local=纯本地 / gemini=纯云端")
        sp.add_argument("--gemini-model", default="group/hydrogen",
                        help="Gemini 模型（4-hydrogen 最快(默认) / 4-argon 最准 / 3.8-flash 快但会串繁体）")
        sp.add_argument("--no-lexicon", action="store_true",
                        help="关闭 XSC 领域词表（默认开启：注入校名/术语 + 错字纠正 + 暗语展开）")
        sp.add_argument("--lexicon-path", default="",
                        help="自定义词表 JSON 路径（默认 lexicon/xsc.json）")
        sp.add_argument("--model", default="small",
                        choices=["tiny", "base", "small", "medium", "large-v3"],
                        help="本地 whisper 模型（默认 small；CPU 上 medium 会明显变慢）")
        sp.add_argument("--device-type", default="cpu", choices=["cpu", "cuda"])
        sp.add_argument("--compute-type", default="int8",
                        choices=["int8", "int8_float16", "float16", "float32"])
        sp.add_argument("--language", default="zh")
        sp.add_argument("--workers", type=int, default=4,
                        help="Gemini 并发路数（默认 4，实测 2.6x；8 路因争抢反而慢）。"
                             "设 1 可强制串行")

    sp = sub.add_parser("gui", help="打开图形界面（推荐）")
    sp.set_defaults(func=cmd_gui)

    sp = sub.add_parser("devices", help="列出可录制的回环设备")
    sp.set_defaults(func=cmd_devices)

    sp = sub.add_parser("doctor", help="环境自检")
    sp.add_argument("--device", default="", help="设备名关键字")
    sp.add_argument("--model", default="small")
    sp.add_argument("--gemini-model", default="group/hydrogen")
    sp.set_defaults(func=cmd_doctor)

    sp = sub.add_parser("stream", help="流地址直录（拿到直播流地址时用，音质最好）")
    sp.add_argument("url", help="直播流地址（flv / m3u8 / rtmp）")
    sp.add_argument("--name", default="", help="文件名标记")
    sp.set_defaults(func=cmd_stream)

    sp = sub.add_parser("record", help="开始录制并实时转写")
    sp.add_argument("--minutes", type=float, default=0, help="录制时长（分钟），0 表示一直录")
    sp.add_argument("--device", default="", help="设备名关键字，如 Realtek")
    sp.add_argument("--name", default="", help="会话名，会体现在文件名里")
    add_asr_opts(sp)
    sp.set_defaults(func=cmd_record)

    sp = sub.add_parser("fetch", help="从视频号下载（自动解密），可选直接转写")
    sp.add_argument("target", nargs="?", default="",
                    help="标题关键词或内容 id；不填配合 --list 查看")
    sp.add_argument("--list", action="store_true", help="列出工具已捕获的内容")
    sp.add_argument("--api", default="http://127.0.0.1:2022",
                    help="抓流工具的 API 地址（默认 127.0.0.1:2022）")
    sp.add_argument("--name", default="", help="输出文件名（默认用标题）")
    sp.add_argument("--no-video", action="store_true",
                    help="只留音频，下载完删掉视频")
    sp.add_argument("--transcribe", action="store_true",
                    help="抽出音频后立即转写")
    add_asr_opts(sp)
    sp.set_defaults(func=cmd_fetch)

    sp = sub.add_parser("transcribe", help="转写已有音频文件")
    sp.add_argument("input", help="音频文件路径（wav/mp3/m4a 等）")
    add_asr_opts(sp)
    sp.set_defaults(func=cmd_transcribe)

    return p


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        return int(args.func(args) or 0)
    except KeyboardInterrupt:
        print("\n已中断。")
        return 130


if __name__ == "__main__":
    sys.exit(main())
