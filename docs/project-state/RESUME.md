---
updated: 2026-11-26T00:30:00+08:00
session: S-20261126-0030
project_version: v0.3.0
status: active
evidence_level: verified_with_live_nas_deploy
---

# Resume Card

> 一分钟确认当前契约，再按证据继续工作。历史细节见 `Handover Book.md` 与日复盘。

## Current Contract

- **Goal**: 打通并加固「本地抓流/代理 → NAS 远程拉流/转码/保留 → ASR/后处理/完整性判定」全链路，完成 NAS 容器重建验收并沉淀复盘。
- **Scope（本轮已完成）**:
  - 本地代理与抓流：`systemproxy.py` 探测上游 `127.0.0.1:10808`、`gui_fetch.py` `content_id` 精确行绑定 + `txTime` 剩余有效期显示 + 告警原话展示
  - NAS 拉流/转码/保留：`nas_runner.py` / `stream_capture.py` / `server.py` / `retention.py` 修复启动卡 `recording`、断点续转、音频缺失置 `failed`、归因码、单场 `meta.asr`、中文目录下载、显式 ffmpeg 校验、原片保留清理，仓库根新增 `.dockerignore`
  - ASR/后处理/完整性：`quality.py` `compute_completeness()` 统一完整性、`session.py` 原子写盘 + 保留 `asr_text`、`engine.py` `_content_failure_calls`、`pipeline.py` 停止事件 + 2s 哨兵、`auto_transcribe_after_stop.py` 1MB 头扫描、`postprocess.py` 逐轮放大 `max_tokens`（8k→16k→32k）+ 剥离序号保护小数、`lexicon.py` 缺失 warning
  - NAS 端部署与验收：同步 14 个 `wxlive/*.py` + `.dockerignore`，重建并启动 `wxlive-nas` 容器，全套验收通过
- **Non-goals（故意不做）**:
  - **不覆盖 `README.md`**（Windows 下与 `ReadMe.md` 为同一文件，DEC-003）
  - **不给 Tk 测试加 skip 消掉 `tk.tcl` error**
  - **不把句级时间戳改成精确值**（保持 `estimated`）
  - **不修改 `audio_capture.py` / `gui.py` 的 NAS 侧差异**（本轮未改这两个文件，保持原样）
- **Acceptance check**:
  - 本地全量测试：`python -m pytest -q` → **512 passed, 2 skipped** (55.43s)
  - NAS 端验收：`GET /api/health` 200 (`ok: true`, `ffmpeg_path: "/host-ffmpeg/ffmpeg"`), `/host-ffmpeg/ffmpeg` 真实编码 59844B flv, 中文库下载 200 (112544B), 容器出网 `baidu.com` / `channels.weixin.qq.com` 均 200

## Verified State

- [verified] 全量单元与集成测试通过：`python -m pytest -q` → **512 passed, 2 skipped**
- [verified] NAS 容器 `wxlive-nas` 使用最新代码重建并启动：容器内 `server.py` / `nas_runner.py` / `stream_capture.py` / `retention.py` MD5 与本地 100% 一致
- [verified] `.dockerignore` 置于仓库根后 BuildKit 生效：`COPY wxlive` 2.7s、`COPY lexicon` 2.4s
- [verified] NAS 宿主机 ffmpeg 挂载正常：`/host-ffmpeg/ffmpeg` (`8.1.1-mediasrv`) 真实编码产出 `59844` 字节 flv
- [verified] NAS 中文标题库下载正常：`/api/library/20261124_080917_欢迎来到我的直播间_stream/download/md` → `200`
- [verified] NAS 容器出网正常：`https://www.baidu.com` 与 `https://channels.weixin.qq.com` 均返回 `200`

**未验证（明确标注）**:

- [inferred] 真实桌面 GUI 在 Clash 断网态下的橙色告警条与 `txTime` 倒计时显示 — 仅单测覆盖，未开桌面 GUI 实测
- [inferred] 新版 `wxlive-nas` 容器在真实 1 小时以上微信直播推流下的端到端长跑表现 — 尚未开播实测

## Blockers and Do-Not-Repeat

- **`.dockerignore` 放错位置会静默失效** — compose 的 `build.context` 是 `..`（仓库根），放 `docker/.dockerignore` 不会被 BuildKit 读取，必须放仓库根 `.dockerignore`。
- **本机 curl 访问局域网 NAS 必须加 `--noproxy '*'`** — 否则会被 `HTTP_PROXY=10808` 拦截导致超时或 000。
- **NAS Docker 命令权限**：`wong` 不在 `docker` 组，非交互 SSH 需通过 `sudo -S`（使用已授权凭据）执行 `docker` / `docker compose`。

## One Next Action

- **action**: 推送一场真实微信直播到新版 `wxlive-nas` 容器，并在桌面 GUI 上验证 `txTime` 剩余有效期显示与完整录制 → 转码 → ASR → 后处理闭环。
- **completion check**: 录制期间 `audio_bytes > 0` 且持续增长，结束后 `meta.transcript_complete == true` 且三份产物齐全无截断。

## Read First

- `Handover Book.md`（Zone A 仪表板 + B1 DEC-011~013 + B3 FAIL-008~009 + Zone C `S-20261126-0030`）
- `docs/daily__pipeline_reviews/2026-11-26.md`（本轮完整复盘 + 上次事项追踪）
