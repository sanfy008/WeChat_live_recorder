"""只录音、不转写。用于长回放等"先保音频、事后慢慢转"的场景。

为什么单独做这个：
  130 分钟回放如果边录边转，转写线程和采集线程抢 CPU，
  且中途任何转写异常都可能影响录音。先纯录（极低 CPU），
  录完再离线批量转，链路最稳、可重复、可断点。

用法：
  python record_only.py --minutes 130 --name 回放标题
  python record_only.py --until-ctrl-c
"""
from __future__ import annotations

import argparse
import sys
import time
from pathlib import Path

import numpy as np
import soundfile as sf

from wxlive.audio_capture import LoopbackRecorder
from wxlive.config import RECORDINGS_DIR, ensure_dirs


def main() -> int:
    ap = argparse.ArgumentParser(description="只录系统音频，不做转写")
    ap.add_argument("--minutes", type=float, default=0, help="录制时长（分钟），0=一直录到 Ctrl+C")
    ap.add_argument("--name", default="", help="文件名标记")
    ap.add_argument("--device", default="", help="设备名关键字")
    ap.add_argument("--silence-stop", type=float, default=0,
                    help="连续静音超过 N 秒自动停止（0=不启用）。回放播完可以用它自动收工")
    ap.add_argument("--sample-rate", type=int, default=16000,
                    help="输出采样率（默认 16000，ASR 直接可吃；体积是 48k 的 1/3）")
    args = ap.parse_args()

    ensure_dirs()
    # WASAPI loopback 只能按设备原生率打开，降采样在 recorder 消费侧完成。
    rec = LoopbackRecorder(device_hint=args.device, chunk_seconds=0.1,
                           out_rate=args.sample_rate)
    rec.start()

    sr = rec.out_rate
    stamp = time.strftime("%Y%m%d_%H%M%S")
    safe = "".join(c for c in (args.name or "rec") if c.isalnum() or c in "-_") or "rec"
    out = RECORDINGS_DIR / f"{stamp}_{safe}.wav"
    wav = sf.SoundFile(str(out), mode="w", samplerate=sr, channels=1, subtype="PCM_16")

    limit = args.minutes * 60 if args.minutes else 0
    t0 = time.time()
    peak = 0.0
    silent_run = 0.0
    total_written = 0
    # 停止原因：事后再也不用靠"WAV 尾部静音多久"去猜当时为什么断
    stop_reason = "用户中断"
    next_report = 15.0

    print(f"[record] 输出 {out.name}  ({sr}Hz mono)")
    print(f"[record] 时长 {'Ctrl+C 结束' if not limit else f'{args.minutes} 分钟'}"
          f"{f'，静音 {args.silence_stop}s 自动停止' if args.silence_stop else ''}")

    try:
        while True:
            elapsed = time.time() - t0
            if limit and elapsed >= limit:
                print(f"\n[record] 达到设定时长 {args.minutes} 分钟，停止")
                stop_reason = f"达到设定时长 {args.minutes} 分钟"
                break

            chunk = rec.read(timeout=0.5)
            if chunk is None:
                stop_reason = "采集端已结束"
                break
            if len(chunk) == 0:
                continue

            wav.write(np.clip(chunk, -1.0, 1.0))
            total_written += len(chunk)
            peak = max(peak, float(np.max(np.abs(chunk))))

            rms = float(np.sqrt(np.mean(chunk**2)))
            if rms < 0.002:
                silent_run += len(chunk) / sr
            else:
                silent_run = 0.0
            if args.silence_stop and silent_run >= args.silence_stop:
                print(f"\n[record] 已静音 {silent_run:.0f}s，判定播放结束，自动停止")
                stop_reason = f"静音 {silent_run:.0f}s 自动停止"
                break

            if elapsed >= next_report:
                mb = total_written * 2 / 1024 / 1024
                print(f"\r  {elapsed/60:5.1f} 分钟 | 已写 {mb:6.1f} MB | 峰值 {peak:.2f} | "
                      f"静音 {silent_run:4.1f}s", end="", flush=True)
                next_report += 15.0
    except KeyboardInterrupt:
        print("\n[record] 收到中断，正在收尾…")
    finally:
        # 先停采集再 drain：stop() 会 flush 重采样器，最后几十毫秒挂到 read() 上，
        # 不 drain 出来就会丢在 WAV 结尾。
        rec.stop(reason=stop_reason)
        while True:
            tail = rec.read(timeout=0.0)
            if tail is None:
                break
            if len(tail):
                wav.write(np.clip(tail, -1.0, 1.0))
                total_written += len(tail)
                peak = max(peak, float(np.max(np.abs(tail))))
        wav.close()

    dur = total_written / sr
    mb = total_written * 2 / 1024 / 1024
    print(f"\n[record] 完成: {out.name}")
    print(f"         时长 {dur/60:.1f} 分钟  大小 {mb:.0f} MB  峰值 {peak:.2f}")
    print(f"\n下一步转写:")
    print(f"  .venv/Scripts/python.exe -m wxlive transcribe \"{out}\"")
    return 0


if __name__ == "__main__":
    sys.exit(main())
