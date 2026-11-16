---
updated: 2026-11-16T23:48:30+08:00
session: S-20261116-2345
project_version: v0.1.0
status: active
evidence_level: mixed
---

# Resume Card

> 一分钟确认当前契约，再按证据继续工作。历史细节见 `Handover Book.md` 与日复盘。

## Current Contract

- **Goal**: 消除 GUI「隔段时间闪一下黑框」并防止复发。
- **Scope**: 已完成。`wxlive/systemproxy.py` 改 `winreg` 直读；新增
  `wxlive/winproc.py` 统一注入 `CREATE_NO_WINDOW`；顺带修了 `_ui_queue`
  初始化顺序 bug（该 bug 导致防断网自检从未生效）。
- **Non-goals（故意不做）**:
  - **不覆盖 `README.md`。** skill 要求的 `ReadMe.md` 在 Windows 下与
    GitHub 的 `README.md` 是同一文件（inode 相同）。交接状态写在
    `Handover Book.md`，不要"按 skill 补 ReadMe.md"。
  - **不给 Tk 测试用例加 skip 来消掉 `tk.tcl` error。** 那会埋掉真实信号。
  - **不改句级时间戳为"精确"。** 实测误差中位 19.3s，标注 `estimated` 是有意为之。
- **Constraints**: WASAPI loopback 路线不碰微信进程；原始 WAV 不删。
- **Acceptance check**: `python -m pytest tests -q` → 236 passed；
  `pythonw -m wxlive gui` 运行期间 `reg.exe` / `ffmpeg.exe` 子进程恒为 0。

## Verified State

- [verified] 一次完整代理自检的**子进程数 = 0** — `wxlive/systemproxy.py` +
  `tests/test_no_console_flash.py::test_read_proxy_state_spawns_no_subprocess`
- [verified] `pythonw` 无控制台父进程启动 console 子进程时**确实分配新控制台**；
  `CREATE_NO_WINDOW` 可消除 — 探针实测三组对照，记录在
  `docs/GUI黑框闪烁治理.md` 与日复盘
- [verified] 真实 `pythonw -m wxlive gui` 跑 75 秒（跨过 60 秒自检周期），
  每 5 秒采样 15 次，`reg.exe` 恒为 0 — 本轮终端输出（未落盘为文件）
- [verified] 状态条修复后显示 `系统代理：127.0.0.1:10808（正常）`，
  修复前恒为「读取中…」 — 真实构造 `App()` 实测
- [verified] 测试 236 passed，连跑三轮全绿（此前 225） — 本轮终端输出
- [verified] `tk.tcl` 偶发错误是**既有问题，与本轮改动无关** —
  `git worktree add /tmp/head-probe HEAD` 用未修改代码复现（已删除该 worktree）

## Blockers and Do-Not-Repeat

- **`tk.tcl` 偶发错误未定位** — 约 1/3 概率 1 个 GUI 用例失败，每次用例不同、单跑必过。
  已排除：文件缺失、`TCL_LIBRARY` 路径错误。未排除：pytest 多实例 Tk 初始化竞态。
  证据：HEAD worktree 对照 + `TODO.md` 低优先级表格。
  **不要用 skip 掩盖。**
- **探针假阴性（两次）** — ①子进程用 `sys.executable`（`pythonw.exe`，GUI 子系统
  永不分配控制台）；②用 `EnumWindows` 抓 `ConsoleWindowClass`（窗口归属
  `conhost.exe` 不是子进程）。教训：验证机制前先自检"探针有能力体现差异吗"。
  证据：`Handover Book.md` FAIL-001 / FAIL-002。
- **`Handover Book.md` 的 Zone C 归档** — 已有 1 条快照，未超过 5 条上限，
  暂不需要 `Handover Book - Archive.md`。

## One Next Action

- **action**: 复现并定位 pytest 的 `tk.tcl` 偶发错误。跑到复现为止，
  然后在 `App()` 调用前后 dump `sys.executable`、`os.environ.get("TCL_LIBRARY")`、
  `tkinter.Tcl().eval("info library")`，判断是用例执行顺序问题还是 Tk 多实例初始化竞态。
- **completion check**: 连续跑 5 次全量测试均无 error（236 passed × 5），
  或找到确切根因并写进 `TODO.md` / `Handover Book.md` DEBT-004。

## Read First

- `Handover Book.md`（Zone A 仪表板 + B1 决策账本，尤其 DEC-003 文件名冲突）
- `docs/daily__pipeline_reviews/2026-11-16.md`（本轮完整复盘 + 证据清单）
- `docs/GUI黑框闪烁治理.md`（技术细节全文）
- `tests/test_no_console_flash.py`（防回归机制，改 GUI 子进程前必读）
- `TODO.md`（遗留项）
