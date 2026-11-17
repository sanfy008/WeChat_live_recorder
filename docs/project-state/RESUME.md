---
updated: 2026-11-17T19:00:00+08:00
session: S-20261117-1900
project_version: v0.2.0
status: active
evidence_level: mixed
---

# Resume Card

> 一分钟确认当前契约，再按证据继续工作。历史细节见 `Handover Book.md` 与日复盘。

## Current Contract

- **Goal**: 把录音转写的整条链路做快、做稳、做可追溯，并确保"产物看起来正常"不会掩盖真实缺口。
- **Scope（本轮已完成）**:
  - 后处理并发 + 退避重试：**615s → 51–80s**（8–12 倍）
  - 录音改 16k：**5.76 → 1.82 MB/分钟**（-67%）
  - ASR 并发 4 → 8 路（1.86x，推翻旧注释）
  - 产物与音频分目录（`Session.audio_dir`）
  - 收尾超时可发现（`drain_timeout` + `meta.transcript_complete`）
  - ASR 统计不再"假健康"（`failed_chunk_count` / `has_content_loss`）
  - 停止原因可追溯（`stop(reason=)` → meta）
- **Non-goals（故意不做）**:
  - **不改音频压缩格式。** Opus 省 97% 但崩溃丢最后 1 秒；FLAC 崩溃后彻底损坏。
    16k WAV 无损、崩溃安全、省 67%，已是更优点。**别因为"还能再省"去动它**，
    要做必须先补"每格式跑 ≥5 次取均值"的可判别实验。
  - **不改看门狗去检测"活着但静音"。** 直播有正常静默期（念评论/喝水/看弹幕），
    贸然重连会打断正常录制。**先收集 2–3 次真实 `stop_reason` 再决定。**
  - **不覆盖 `README.md`。** Windows 下与 `ReadMe.md` 是同一文件（DEC-003）。
  - **不给 Tk 测试加 skip 消掉 `tk.tcl` error。** 那会埋掉真实信号。
  - **不把句级时间戳改成"精确"。** 实测误差中位 19.3s，标 `estimated` 是有意为之。
- **Constraints**: WASAPI loopback 不碰微信进程；原始 WAV 不删；
  loopback **无法直接以 16k 打开**（`-9997`），只能消费侧重采样。
- **Acceptance check**: `python -m pytest tests -q` → **293 passed**（1 个 tk.tcl 偶发不算失败）。

## Verified State

- [verified] 后处理 1048 行从 615s 降到 51–80s，行数恒 1048 —
  三轮复跑 78.4s/50.9s/60.9s + 独立 79.6s，见 `docs/思考强度与后处理耗时实测.md`
- [verified] 16k 重采样正确且只做一次 — 合成 48k/10s/440Hz/振幅0.3 →
  16k/10.00s/440.0Hz/RMS 0.2121（理论 0.3/√2=0.212）
- [verified] 录音体积 1.82MB/分钟 — 真实 10 分钟录制实测 18MB / 9.9min
- [verified] 产物与音频分目录在实际运行中生效 —
  10 分钟录制期间观察到 WAV 在 `recordings/`、md/jsonl 在 `transcripts/`
- [verified] ASR 并发 8 路是拐点，12 路无额外收益 — 2400s/22 块各跑 2 次：
  4路 69.2s / 8路 37.2s / 12路 37.5s
- [verified] 网关瞬时抖动（403/502/行数不符）单独重试都能成功 —
  这是退避重试的依据；不重试会漏纠 4–12%
- [verified] FLAC 崩溃后彻底损坏、Opus 丢约 1 秒、WAV 30s 完整可读 —
  模拟硬退出（`os._exit`，不走 close）
- [verified] 收尾超时确实会发生且过去是静默的 — 10 分钟实测：音频 592s、
  转写只到 404.9s、json 完全未生成
- [verified] 补转证明音频本身完好 — 405s 之后补转出 32 句

**未验证（明确标注）**:

- [inferred] 格式压缩是否让**专有名词**更易出错 —— 测试片段内 40 个词表校名命中数全为 0，
  **该项未验证**。因已决定不采用压缩格式，暂不影响结论。
- [inferred] `transcript_complete=False` 在真实 1 小时以上场次的触发率 —— 未测。
- [inferred] GUI `_run_live`（录制+转写）路径的端到端行为 —— 本轮只测了 CLI pipeline，
  GUI 那条未真实点击验证（DEBT-008）。

## Blockers and Do-Not-Repeat

- **`tk.tcl` 偶发错误连续两天未定位** — 约 1/3 概率 1 个 GUI 用例失败，
  每次命中不同、单跑必过。已排除文件缺失与 `TCL_LIBRARY` 路径。
  证据：`TODO.md` + 多次全量运行结果。**不要用 skip 掩盖。**
- **验证链路前必须先量化噪声（本轮栽了 3 次）** —
  ①格式对比没测 A/A 基线（FAIL-004）；②PowerShell 播测试音录到自己蜂鸣声（FAIL-005）；
  ③靠 grep 数出现次数判"未使用导入"（FAIL-006）。
  证据：`Handover Book.md` B3 FAIL-004~006。
- **"降级不报错"会让统计失真** — `gemini_asr` 内部把整块失败吞成占位符返回值，
  导致 `primary_ok=4 / primary_fail=0` 而转写全废（FAIL-007）。
  凡遇到包装层降级，必须单独检查统计口径。
- **收尾超时的产物会"看起来正常"** — md/jsonl 分布均匀，只有对比音频时长才能发现。
  判断完整性要看 `meta.transcript_complete`，不要看文件是否存在。

## One Next Action

- **action**: 验证 `transcript_complete` 在真实长直播下是否真的拦住缺口。
  跑一场 1 小时以上的真实直播；若中途出现 RTF 飙高或队列积压，确认三件事：
  ① 日志有没有打 `[main][warn] 收尾超时`；
  ② 产物 meta 里 `transcript_complete` 是否为 `False`；
  ③ `output/spilled/` 有没有落下未转片段。
- **completion check**: 三项证据任一出现即可确认机制有效；
  若整场 `transcript_complete=True` 且
  `|音频时长 - 转写末句时间| < 5s`，说明本轮修复在长场景下也成立。

## Read First

- `Handover Book.md`（Zone A 仪表板 + B1 决策 DEC-005~010 + B3 失败 FAIL-004~007）
- `docs/daily__pipeline_reviews/2026-11-17.md`（本轮完整复盘 + 上次事项追踪）
- `docs/思考强度与后处理耗时实测.md`（四层验证法 + 全部实测数据）
- `docs/全流程实测-2026-11-17.md`（10 分钟真实录制 + 两个缺陷的现场记录）
- `docs/项目体检-2026-11-17.md`（7 项发现 + 修复对照）
- `tests/test_output_layout.py`（防回归：目录/收尾/停止原因/统计真实性）
