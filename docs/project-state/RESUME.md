---
updated: 2026-11-28T08:20:00+08:00
session: S-20261128-0820
project_version: v0.5.0
status: active
evidence_level: mixed
---

# Resume Card

> 一分钟确认当前契约，再按证据继续工作。历史细节见 `Handover Book.md` 与日复盘。

## Current Contract

- **Goal**: 巩固「PC 抓流/解密直推 + 手机端公网直推 + NAS 无人值守录制/转写/摘要」全链路，补齐分块摘要重试、禁用 `max` 思考档位、统一 XSC 词表注入及 `WXLIVE_TOKEN` / `WXLIVE_ALLOWED_HOSTS` 鉴权闭环，并完成当前阶段复盘存档。
- **Scope（本轮已完成）**:
  - 词表扩充与注入统一（`adfa9b9`）：扩充 `lexicon/xsc.json`，`wxlive/lexicon.py` 新增 `build_vocabulary(60, 40)` 并统一接入 `auto_pipeline.py` / `scripts/auto_transcribe_after_stop.py` / `wxlive/gui.py` / `wxlive/nas_runner.py`
  - 分块摘要重试与禁用 `max`（`043429f`）：`wxlive/postprocess.py` 新增 `_complete_multi_with_retry`（`DIGEST_RETRY_DELAYS = (2.0, 6.0)`）；`wxlive/config.py` 将 `VALID_REASONING_EFFORTS` 收紧为 `("none", "low", "high")`；`wxlive/server.py` 与 `wxlive/nas_runner.py` 移除/降级 `max`
  - 全端鉴权与 Host 白名单闭环（`043429f`）：`wxlive/server.py` 实现 `WXLIVE_ALLOWED_HOSTS`、`WXLIVE_READ_AUTH` 与 Web UI「🔑 令牌」入口；`wxlive/fetch.py` / `wxlive/gui_fetch.py` 透传 `X-WXLIVE-Token`；`docker/docker-compose.yml` 与 `docker/.env.example` 透传环境变量
  - 手机端联调与 NAS 现网配置：排查今早 `20261127_080703_167a58` 串台假停播根因，输出手机端整改交接清单（用户已反馈手机端 `agy` 修复完成）；等待任务结束后重建 `wxlive-nas`（`sha256:dca75fdff`），并在 NAS `.env` 配置 43 位强随机 `WXLIVE_TOKEN`（不回显）、`WXLIVE_ALLOWED_HOSTS=one.20161213.xyz`、`WXLIVE_READ_AUTH=false`
- **Non-goals（故意不做）**:
  - **不覆盖 `README.md`**（Windows 下与 `ReadMe.md` 为同一文件，保留 GitHub 用户文档，DEC-003）
  - **不开启 `WXLIVE_READ_AUTH=true`**（用户明确偏好网页免密只读浏览，仅对写操作/推送强制 `WXLIVE_TOKEN`，DEC-016）
  - **不给 Tk 测试加 skip 消掉 `tk.tcl` 偶发 error**
  - **不把句级时间戳改成精确值**（保持 `ts_source="estimated"`）
- **Acceptance check**:
  - 本地全量测试：`cd /d/File/01_Vibe_Coding/14-WeChat_live && .venv-trim/Scripts/python.exe -m pytest -q` → **570 passed** (96.92s)
  - NAS 内网与公网鉴权实测：`GET /api/health` → `200` (`auth_required=True`, `read_auth_required=False`)；无令牌 `GET /` 与 `GET /api/jobs` → `200` (`jobs=16`)；无令牌 `POST /api/probe` → `401`；带有效 `X-WXLIVE-Token` `POST /api/probe` → `422`

## Verified State

- [verified] 正式目录 `D:/File/01_Vibe_Coding/14-WeChat_live`（`main` 分支）全量测试通过：`.venv-trim/Scripts/python.exe -m pytest -q` → **570 passed in 96.92s**
- [verified] NAS 容器 `wxlive-nas` 已基于最新代码重建并重载 `.env`（镜像 `sha256:dca75fdff`，`WXLIVE_TOKEN` 已生效、`WXLIVE_ALLOWED_HOSTS=one.20161213.xyz`、`WXLIVE_READ_AUTH=false`）
- [verified] 内网（`http://192.168.68.49:8910`）与公网（`http://one.20161213.xyz:16666`）双端实测：网页/只读 API 免密 `200`（`jobs=16`），写接口无令牌 `401`、携带有效 `X-WXLIVE-Token` 通过鉴权

**未验证（明确标注）**:

- [inferred] 手机端 `wechat_live_nas_pusher` 读入 NAS `.env` 令牌并修复串台后的真实直播间滑切推送与公网带令牌闭环 — 用户反馈手机端已按清单修复，但尚未在开启 Token 后的现网执行真机实播推送验证
- [inferred] 新版容器下 2 小时以上长直播的 `summarize_digest` 分块重试与 `meta.quality` 落盘表现 — 尚无重建后的长场次完成样本
- [inferred] 桌面 GUI 在填写 `WXLIVE_TOKEN` 后对 NAS 的探活/加密回放直推，以及 `txTime` 剩余有效期 / Clash 断网态告警条实机点击验证 — 仅自动化单测覆盖

## Blockers and Do-Not-Repeat

- **跑 `pytest` 必须先 `cd` 进入目标仓库根目录**：从其他工作树目录用绝对路径调用 `pytest D:/.../tests` 会导入旧工作树的 `wxlive` 包并报 `ModuleNotFoundError: No module named 'wxlive.artifacts'`（FAIL-012）。
- **不得擅自将 `WXLIVE_READ_AUTH` 设为 `true`**：用户明确拒绝打开网页弹窗输密码（FAIL-011 / DEC-016）。
- **`.dockerignore` 放错位置会静默失效**：compose 的 `build.context` 是 `..`（仓库根），必须保留在仓库根 `.dockerignore`。
- **本机 curl 访问局域网 NAS 必须加 `--noproxy '*'`**，否则会被 `HTTP_PROXY=10808` 拦截。
- **NAS `docker compose` 必须在 `/vol1/1000/docker/wxlive-nas/docker/` 目录执行**（非交互 SSH 使用管理员 `wong` 授权提权）。

## One Next Action

- **action**: 将正式目录 `D:/File/01_Vibe_Coding/14-WeChat_live` 的 `main` 分支（含本轮存档文档与领先 `origin/main` 的代码提交）推送到远程仓库，并由手机端真机带 `X-WXLIVE-Token` 滑切直播间推送一场真实直播到 `http://one.20161213.xyz:16666` 做闭环验收。
- **completion check**: `git status` 干净且 `main` 与 `origin/main` 同步；手机端弹窗标题/流与实际录制一致，NAS 日志无 401/串台，结束后 `meta.transcript_complete == true`、`meta.quality.ok == true` 且 `llm_status` 非 `partial`。

## Read First

- `Handover Book.md`（Zone A 仪表板 + B1 DEC-014~016 + B3 FAIL-010~012 + Zone C `S-20261127-1430`）
- `docs/daily__pipeline_reviews/2026-11-27.md`（本轮完整复盘 + 上次事项追踪）
