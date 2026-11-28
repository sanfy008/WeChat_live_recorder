---
updated: 2026-11-28T08:30:00+08:00
session: S-20261128-0824
project_version: v0.5.0
status: active
evidence_level: mixed
---

# Resume Card

> 一分钟确认当前契约，再按证据继续工作。历史细节见 `Handover Book.md` 与日复盘。

## Current Contract

- **Goal**: 巩固「PC 抓流/解密直推 + 手机端公网直推 + NAS 无人值守录制/转写/摘要」全链路；本轮聚焦**PC 端 GUI 的可用性与可辨识性** —— 让令牌看得见、存得住、体检能鉴别真伪，并让「跑的是哪一版」一眼可辨。
- **Scope（本轮已完成，v0.5.0）**:
  - **NAS 令牌可用性修复（`21858e6`）**：令牌框 `width` 12→44 并加「显示」开关（NAS 令牌 43 位）；新增 `NasConfig(base_url/token)` 接入 `SAVEABLE` 白名单，失焦/回车落盘、启动回填（环境变量 `WXLIVE_TOKEN` 仍可临时覆盖）；修掉「保存设置整体覆盖写会抹掉令牌」的静默丢失；「体检」由单打免令牌 `/api/health` 改为 `health` + 空 body `POST /api/probe` 两步探针（`401`=令牌问题、`422`=通过）；401 弹窗改为可执行三步指引
  - **版本号治理（`a0a8d31`）**：`wxlive.__version__` 收口为唯一真相源，删除 `gui.py` 硬编码 `APP_VER = "1.0"`；标题栏改为 `v0.5.0 · <源码指纹>`；新增 `build_stamp()`（对 `wxlive/*.py` mtime+size 取 6 位哈希，识别"改了代码没重启"）；启动写日志留痕；新增 `tests/test_version.py`
  - **测试污染治理（`05127e2`）**：fixture 隔离 `wxlive.gui.OUTPUT_DIR`（注意必须改 gui 模块名，改 config 无效），避免每跑一次 pytest 就往生产日志写假「启动」记录
- **重要结论（推翻上轮表述）**: 「桌面 GUI 令牌输入框待实测」这条遗留项**表述有误** —— 不是没测，而是当时运行的进程（2026-11-26 17:55 启动）里**根本没有该控件**（重码于 11-27 11:14）。已更正并写入 FAIL 记录。
- **Non-goals（故意不做）**:
  - **不实现 `X-WXLIVE-Token` 的"自动获取"**（DEC-017）：它是用户在 NAS `.env` 自设的密钥，能自动拿到 = 鉴权失效。只做「存一次免填」。
  - **不覆盖 `README.md`**（Windows 下与 `ReadMe.md` 为同一文件，保留 GitHub 用户文档，DEC-003）
  - **不开启 `WXLIVE_READ_AUTH=true`**（用户明确偏好网页免密只读浏览，仅对写操作/推送强制 `WXLIVE_TOKEN`，DEC-016）
  - **不给 Tk 测试加 skip 消掉 `tk.tcl` 偶发 error**
  - **不把句级时间戳改成精确值**（保持 `ts_source="estimated"`）
- **Acceptance check**:
  - 本地全量测试：`cd /d/File/01_Vibe_Coding/14-WeChat_live && .venv-trim/Scripts/python.exe -m pytest -q` → **581 passed**
  - 令牌三向探针（真实 NAS）：空令牌→`在线，但令牌没通过 · 令牌为空`/`verified=False`；错令牌→`…与服务端不一致`/`False`；正确令牌→`在线 · 令牌有效 · ffmpeg=有`/`True`
  - 推送链路：带真实令牌 `POST /api/probe` → **200**（修复前必然 401）
  - 版本一致性：包 `0.5.0` == 标题栏 `v0.5.0` == RESUME `v0.5.0`；真实窗口标题 `微信直播录音转写 v0.5.0 · 72698a`
- **Non-goals（故意不做）**:
  - **不覆盖 `README.md`**（Windows 下与 `ReadMe.md` 为同一文件，保留 GitHub 用户文档，DEC-003）
  - **不开启 `WXLIVE_READ_AUTH=true`**（用户明确偏好网页免密只读浏览，仅对写操作/推送强制 `WXLIVE_TOKEN`，DEC-016）
  - **不给 Tk 测试加 skip 消掉 `tk.tcl` 偶发 error**
  - **不把句级时间戳改成精确值**（保持 `ts_source="estimated"`）
- **Acceptance check**:
  - 本地全量测试：`cd /d/File/01_Vibe_Coding/14-WeChat_live && .venv-trim/Scripts/python.exe -m pytest -q` → **570 passed** (96.92s)
  - NAS 内网与公网鉴权实测：`GET /api/health` → `200` (`auth_required=True`, `read_auth_required=False`)；无令牌 `GET /` 与 `GET /api/jobs` → `200` (`jobs=16`)；无令牌 `POST /api/probe` → `401`；带有效 `X-WXLIVE-Token` `POST /api/probe` → `422`

## Verified State

- [verified] 正式目录 `D:/File/01_Vibe_Coding/14-WeChat_live`（`main` 分支）全量测试通过：`.venv-trim/Scripts/python.exe -m pytest -q` → **581 passed**
- [verified] NAS 容器 `wxlive-nas` 基于最新代码重建并重载 `.env`（镜像 `sha256:dca75fdff`，`WXLIVE_TOKEN` 已生效、`WXLIVE_ALLOWED_HOSTS=one.20161213.xyz`、`WXLIVE_READ_AUTH=false`）
- [verified] 内网（`http://192.168.68.49:8910`）与公网（`http://one.20161213.xyz:16666`）双端实测：网页/只读 API 免密 `200`，写接口无令牌 `401`、携带有效 `X-WXLIVE-Token` 通过鉴权
- [verified] **GUI 令牌链路端到端可用**：真实 NAS 三向探针（空/错/对）判定正确；带真实令牌 `POST /api/probe` 返回 `200`；磁盘 `config/user.json` 的 `nas` 段写入并重开回填；`_save_settings()` 前后令牌长度不变（41→41）
- [verified] **运行中的 GUI 确认为 v0.5.0**：进程 PID `25768` @ `08:20:10`，日志 `启动 微信直播录音转写 v0.5.0 · 源码指纹 4b20f7 · PID 25768`
- [verified] **测试零污染**：跑 `tests/test_gui_fetch.py` 前后生产日志行数均为 `206`（原会写入 107 条假「启动」）
- [verified] **新测试有效性反证**：`git stash` 回退修复后 6 个 NAS 用例 6/6 失败，恢复后 6/6 通过

**未验证（明确标注）**:

- [user-confirmed] 手机端 `wechat_live_nas_pusher` 读入 NAS `.env` 令牌并修复串台后的真实直播间滑切推送与公网带令牌闭环 — **King 于 2026-11-28 08:30 明确告知「手机端已经完成优化验收」**。
  证据级别：用户口头确认，非本机可复现（手机端代码不在本仓库，`wechat_live_nas_pusher` 为独立项目）。
  台账动作：DEBT-009 由 `Open` 转为 `User-verified`，仍保留「微信大版本升级即失效」的风险描述。
- [inferred] 新版容器下 2 小时以上长直播的 `summarize_digest` 分块重试与 `meta.quality` 落盘表现 — 今日 `20261128_073432_00dbef` 正在录制，可作为首场样本，但产物未取数
- [inferred] `txTime` 剩余有效期显示与 Clash 断网态橙色告警条的真实点击验证 — 仅自动化单测覆盖（**连续两轮遗留**）

## Blockers and Do-Not-Repeat

- **跑 `pytest` 必须先 `cd` 进入目标仓库根目录**：从其他工作树目录用绝对路径调用 `pytest D:/.../tests` 会导入旧工作树的 `wxlive` 包并报 `ModuleNotFoundError: No module named 'wxlive.artifacts'`（FAIL-012）。
- **不得擅自将 `WXLIVE_READ_AUTH` 设为 `true`**：用户明确拒绝打开网页弹窗输密码（FAIL-011 / DEC-016）。
- **报告「GUI 缺某功能」前必须先比对进程启动时间与代码 mtime**（FAIL-013）：`pythonw` 起的窗口不打印任何东西，多天不退出的进程跑的是旧代码。本轮「找不到令牌输入框」就是窗口比重码早 17 小时导致的误判。**现在的判据：看标题栏的源码指纹**（`v0.5.0 · 4b20f7`），指纹与当前代码算出来的不一致 = 旧进程。
- **验证鉴权不得用免鉴权端点**（FAIL-014）：`/api/health` 刻意放开令牌，拿它当体检 = 令牌错也显示"在线"。必须打需要鉴权的端点（如空 body `POST /api/probe`）。**绿灯必须与失败同层。**
- **新增配置段要检查整体覆盖写会不会抹掉它**（FAIL-015）：`save_user_config` 是覆盖写，`_collect_settings` 漏带 `nas` 段就会在点「保存设置」时静默清零令牌。
- **给运行中产物加写副作用前，先确认测试 fixture 已隔离对应目录**（FAIL-016）：启动日志曾每跑一次 pytest 就往生产日志写一条假「启动」，一轮全量 107 条。判据：跑测试前后比对目标文件行数。
- **`.dockerignore` 放错位置会静默失效**：compose 的 `build.context` 是 `..`（仓库根），必须保留在仓库根 `.dockerignore`。
- **本机 curl 访问局域网 NAS 必须加 `--noproxy '*'`**，否则会被 `HTTP_PROXY=10808` 拦截。
- **NAS `docker compose` 必须在 `/vol1/1000/docker/wxlive-nas/docker/` 目录执行**（非交互 SSH 使用管理员 `wong` 授权提权）。

## One Next Action

- **action**: 将正式目录 `D:/File/01_Vibe_Coding/14-WeChat_live` 的 `main` 分支（含本轮存档文档与领先 `origin/main` 的代码提交）推送到远程仓库，并由手机端真机带 `X-WXLIVE-Token` 滑切直播间推送一场真实直播到 `http://one.20161213.xyz:16666` 做闭环验收。
- **completion check**: `git status` 干净且 `main` 与 `origin/main` 同步；手机端弹窗标题/流与实际录制一致，NAS 日志无 401/串台，结束后 `meta.transcript_complete == true`、`meta.quality.ok == true` 且 `llm_status` 非 `partial`。

## Read First

- `Handover Book.md`（Zone A 仪表板 + B1 DEC-014~016 + B3 FAIL-010~012 + Zone C `S-20261127-1430`）
- `docs/daily__pipeline_reviews/2026-11-27.md`（本轮完整复盘 + 上次事项追踪）
