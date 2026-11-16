# Handover Book — 微信直播录音转录（wxlive）

> 交接簿：给下一个接手的 AI 或未来的自己看。
> 架构：Zone A 活性仪表板（每次 save 更新）｜Zone B 累积知识库（只增不删）｜Zone C 会话归档（冻结）

---

## Zone A — Living Dashboard（每次更新为最新状态）

### A1. 🎯 Core Intent Anchor（意图锚点）

**把电脑端微信直播的声音变成可信的文字。**

三根支柱，任何改动都不许破坏：

1. **不碰微信进程。** 走 WASAPI loopback 从声卡输出端录，不注入、不 hook、不改微信。
   微信升级不影响录制。
2. **"能跑"不等于"可信"。** 采集要能数学级验证（已知信号 → 峰值/RMS 对得上），
   ASR 产物要有第三方基准对照（whisper）才算修对。
3. **原始音频不贬值。** `<会话>.wav` 永不为省空间删除——模型会升级，音频不会。

**反面清单（明确不做）**：不做"看起来成功"的假验证；不做牺牲可复现性的优化；
不引入常驻系统级副作用（如自动改注册表）而不告知用户。

### A2. 📝 Active Handoff Status（任务接力板）

**✅ Milestones Achieved（已达成）**

- WASAPI loopback 采集链路**数学级验证通过**（0.25 振幅正弦 → 峰值精确 0.2500）
- 多引擎 ASR + 词表纠错，XSC 暗语词表 194 校名 / 123 术语 / 247 暗语 / 51 纠错
- ASR 质量治理：幻觉拦截 + 分句修复 + `ts_source` 诚信标注（密度双向收敛 ≈5.46-5.47 字/秒）
- 视频号下载：解密拉流 + GUI 列表/下载/解析 + 系统代理防断网
- **GUI 子进程黑框闪烁已消除**（本轮，一次自检子进程 6 → 0）
- 环境精简 `.venv-trim`（316MB，启动脚本优先用）

**🚧 Work In Progress（进行中）**

- 无。本轮收口干净，工作区无未提交改动。

**🧱 Active Blockers（当前阻塞）**

- 无硬阻塞。一个未定位的既有问题见 A3 技术债务 DEBT-004。

**🗂️ Latest Daily Review（最近一份日复盘）**

- `docs/daily__pipeline_reviews/2026-11-16.md`（首次建立，2026-11-16 23:47）

**🚀 Next Prompt Recommendations（下一轮建议 Prompt）**

1. **继续排查 tk.tcl 偶发错误**
   > "跑 pytest 全量到复现 `tk.tcl` 错误为止，然后在 App() 前后 dump
   > sys.executable / TCL_LIBRARY / tkinter.Tcl().eval('info library')，
   > 判断是 pytest 用例顺序还是 Tk 多实例初始化竞态。不要给用例加 skip 掩盖。"
2. **全仓排查同类初始化顺序 bug**
   > "检查 wxlive/gui*.py 里所有 `_build_*_tab()` 方法，找出'方法体中段调用了
   > 依赖后置字段的回调'的模式，列出风险点并给出修复。参考
   > gui_fetch.py 的 `_ui_queue` 那个 bug。"
3. **真机长跑验证**
   > "用真实微信直播场景跑 30 分钟以上，确认 GUI 无任何黑框闪烁，
   > 同时确认状态条每 60 秒确实在刷新（不是停在旧值）。"

### A3. 🔍 Quality & Gap Analysis（质量缺口）

**已有的好习惯（继续保持）**

- 定量验证优先于"看起来对了"（正弦信号验证采集链路、whisper 基准验证 ASR）
- 桩函数只能验证分支，判据松紧必须用真实数据校准
- 防回归测试盯"因"不盯"果"（盯子进程数，不盯"快不快"）

**当前缺口**

- **真实长跑样本不足**。验证窗口普遍偏短（75 秒、10 轮模拟），
  真机连续数小时的行为只有回放场景验证过。
- **GUI 侧测试的环境依赖脆弱**。`tk.tcl` 偶发失败说明 Tk 测试环境不够稳。
- **`README.md` 承担了双重职责**（GitHub 说明 + 事实上的唯一文档入口），
  但缺少面向"下一个接手者"的状态快照——本 Handover Book 是补充。

**🏗️ 技术债务台账**

- **[DEBT-001]** 系统代理还原依赖 GUI 在线 | Introduced: S-20261116-1612 | Status: `Open`
  - 📍 Location: `wxlive/systemproxy.py`
  - ⚡ Impact: Medium。GUI 没开时若抓流工具崩溃，用户不知道代理停在 2023
  - 💡 Suggested Fix: 已有 GUI 状态条兜底（本轮修好后才真正生效），暂不加常驻守护

- **[DEBT-002]** 词表靠手工维护，无自动发现新词 | Introduced: 早期 | Status: `Open`
  - 📍 Location: `lexicon/`
  - ⚡ Impact: Low。新暗语需人工补充
  - 💡 Suggested Fix: 从高频未登录词里半自动挖掘候选

- **[DEBT-003]** 句级时间戳是估算值（`ts_source="estimated"`） | Status: `Open`（有意为之）
  - 📍 Location: `wxlive/gemini_asr.py`
  - ⚡ Impact: Low。已明确标注，块级锚点可信
  - 💡 Suggested Fix: 不修。实测误差中位 19.3s/最大 59.0s，改成"精确"是伪精度

- **[DEBT-004]** pytest 全量偶发 `TclError: Can't find a usable tk.tcl` | Introduced: 未知（早于本轮） | Status: `Open`
  - 📍 Location: Tk 测试环境（`tests/test_gui_fetch.py` fixture）
  - ⚡ Impact: Low（不影响产品，只影响 CI 稳定性）。约 1/3 概率 1 个用例失败
  - 💡 Suggested Fix: 见 A2 下一轮 Prompt #1。**注意：不要用 skip 掩盖**

### A4. 📊 Skill Dashboard（效用仪表板）

| Skill 名称 | 累计触发 | 用户满意 | 用户纠正 | 效用评分 |
|---|---|---|---|---|
| king-workstyle | 多次（跨会话） | — | — | ⭐⭐⭐⭐⭐ |
| project-save | 1 | — | — | — |
| wechat-live-recorder | 多次 | — | — | ⭐⭐⭐⭐⭐ |
| windows-audio-capture | 多次 | — | — | ⭐⭐⭐⭐⭐ |
| tkinter-gui-testing | 多次 | — | — | ⭐⭐⭐⭐⭐ |

> 本轮未触发的 Skill 保持空白由后续 session 补齐。满意度需用户显式反馈后才计分，不自行判定。

### A5. 🧪 Experiment Log（实验日志）

| 时间 | Skill | 尝试 | 结果 | 保留? |
|------|-------|------|------|-------|
| 23:10 | — | 探针用 `sys.executable` 起子进程测控制台 | **假阴性**：`sys.executable` 是 `pythonw.exe`，GUI 子系统永不分配控制台，测出"都有/都没有" | discard |
| 23:15 | — | 探针用 `EnumWindows` 抓 `ConsoleWindowClass` 按 PID 过滤 | **假阴性**：控制台窗口归属 `conhost.exe` 不是子进程，抓到 0 个 | discard |
| 23:20 | — | 子进程 `sleep 1.2s` 后再枚举 + 改用 `GetConsoleWindow()` | **成功**：三组对照精确复现差异（python.exe/0 → False；pythonw/0 → True；pythonw/no-window → False） | keep |
| 23:28 | — | 修复候选：给 `reg query` 加 `CREATE_NO_WINDOW` 即可 | **不彻底**：止住闪烁但仍每分钟起 6 个子进程，耗时 180ms | discard |
| 23:30 | — | 修复方案：改 `winreg` 直读 + 统一 `winproc` 封装 | **采纳**：6 → 0 子进程，180ms → 0.94ms，且全部调用点收编 | keep |

---

## Zone B — Cumulative Knowledge Base（累积模式，只增不删）

### B1. 🧠 Decision Ledger（决策账本）

- **[DEC-001] 读系统代理改用 winreg 直读，不用 reg.exe** | Session: S-20261116-2345 | 2026-11-16
  - 🔍 Context: 每 60 秒自检起 6 个 `reg query`，在 `pythonw` 下每个都分配控制台窗口 → 屏幕每分钟闪 6 下
  - ✅ Decision: `systemproxy.read_all()` 用 `winreg.OpenKey` 同进程读三个值
  - ❌ Alternatives: ①只给 `reg query` 加 `CREATE_NO_WINDOW`（仍起 6 进程、180ms，浪费且脆弱）；②降低自检频率到 5 分钟（牺牲防断网时效）
  - 📌 Status: `Active`

- **[DEC-002] 子进程统一走 wxlive/winproc.py，不给扫描器开例外** | Session: S-20261116-2345 | 2026-11-16
  - 🔍 Context: 全仓扫描测试拦下 3 处裸 `subprocess`（macOS/Linux 分支 + ffmpeg 录流），当时那些平台不受影响
  - ✅ Decision: 全部收编进 `winproc.run/popen`，测试不留白名单
  - ❌ Alternatives: ①给扫描器加平台判断跳过非 Windows 分支（口子一开规则形同虚设，且 ffmpeg 录流是真的会闪）；②只修 Windows 相关的那处
  - 📌 Status: `Active`

- **[DEC-003] 不覆盖 README.md，状态快照另存 Handover Book** | Session: S-20261116-2345 | 2026-11-16
  - 🔍 Context: project-save skill 要求维护 `ReadMe.md`，但项目已有 `README.md`（21KB，面向 GitHub 用户）。Windows 文件名大小写不敏感，实测两者 inode 相同（`1688849861429484`）→ **是同一个文件**
  - ✅ Decision: 保留 `README.md` 原样不动；交接状态写进 `Handover Book.md` + `docs/daily__pipeline_reviews/`
  - ❌ Alternatives: ①按 skill 覆盖 `README.md`（会摧毁 GitHub 说明，不可接受）；②重命名现有 README（破坏 GitHub 仓库导航和外部链接）
  - 📌 Status: `Active`。**下次接手者不要"按 skill 补 ReadMe.md"，那会覆盖 GitHub 文档**

- **[DEC-004] 修黑框时连带修了 _ui_queue 初始化顺序 bug** | Session: S-20261116-2345 | 2026-11-16
  - 🔍 Context: 验证修复效果时发现状态条恒为「读取中…」，追下去是既有 bug（启动探测早于 `_ui_queue` 创建），导致防断网自检**从上线起就没生效过**
  - ✅ Decision: 一并修，并把 5 个状态变量提前到 `_build_fetch_tab()` 开头
  - ❌ Alternatives: 只报不改（问题会被交接遗漏，而且"防断网"是 DEC 里明确要的中心能力，不能是假的）
  - 📌 Status: `Active`

### B2. 🎨 Style DNA（用户偏好档案）

- `[S-20261116-2345]` **用户先提出自己的因果假设，但允许被推翻。** 本轮他说"是不是 GOI 调用了什么工具"，实际是工具在轮询自己的状态条。他接受纠正，关注点在"能不能优化"。→ 不要因为用户给了假设就顺着编解释，要独立验证。
- `[S-20261116-2345]` **对"隔段时间闪一下"这类间歇现象，用户要的是根因 + 定量前后对比**，不是"已经修好了"的一句话。
- `[S-20261116-2345]` 说"复盘总结沉淀经验教训"时，期待的是**带证据路径的结构化档案**，而不是聊天里的一段总结。
- `[跨会话]` 偏好 GUI 操作技术工作流；技术讨论到代码/测试/系统配置级别。
- `[跨会话]` 喜欢"双线赛马"——同一任务并行跑多个 AI 再对擂取优。
- `[跨会话]` 说"go ahead"时期待直接从评审进入实现 + 配套回归测试。

### B3. ❌ Failed Attempts Archive（失败档案）

- **[FAIL-001] 探针用 `sys.executable` 起子进程，测不出控制台分配** | Session: S-20261116-2345 | 2026-11-16
  - 🔧 Approach: 父进程 `pythonw`，子进程用 `sys.executable`
  - 💥 Failure Reason: `sys.executable` 在 `pythonw` 进程里就是 `pythonw.exe`，GUI 子系统程序**永远不会被分配控制台**。测出 noflag 和 withflag 都是 `has_console=False`，得出"现象无法复现"的错误结论
  - 💡 Lesson: **验证 A 机制时，激励源/被测对象必须是能体现差异的那一类。** 这里子进程必须是 console 子系统（`python.exe`）。同 MEMORY.md 里 `Start-Process` 播 WAV 没发声那次是同一类错误——**验证链路时，两端的行为都必须确定**

- **[FAIL-002] 用 `EnumWindows` 抓 `ConsoleWindowClass` 判断有无控制台** | Session: S-20261116-2345 | 2026-11-16
  - 🔧 Approach: 子进程枚举顶层窗口，按 PID 过滤找控制台类窗口
  - 💥 Failure Reason: 控制台窗口归属 `conhost.exe` 而非子进程，按子进程 PID 过滤恒为 0
  - 💡 Lesson: 判断"进程有没有控制台"用 `GetConsoleWindow()`，不要用窗口枚举

- **[FAIL-003] 只加 `CREATE_NO_WINDOW` 的修复方案被否决** | Session: S-20261116-2345 | 2026-11-16
  - 🔧 Approach: 认为"止住闪烁就够了"，计划只给 `reg query` 加 flags
  - 💥 Failure Reason: 定量的角度看这仍不对——每分钟仍起 6 个子进程、耗 180ms 去做一件 0.94ms 能完成的事。本质是"轮询方式选错了"，不是"调用姿势不对"
  - 💡 Lesson: **修到"现象消失"不够，要修到"这件事本来就不该这么做"。** 问一句"这个操作有没有零成本的替代实现"

### B4. 🏗️ Technical Debt Register

见 Zone A3 的台账（DEBT-001 ~ DEBT-004）。此处不重复，保持单一事实来源。

---

## Zone C — Session Archive（冻结区，新快照在最上方）

---

### 📸 Session Snapshot: S-20261116-2345 | v0.1.0 | 2026-11-16 23:45

**Session Summary**: 用户报「GUI 隔段时间闪一下黑框」。定位到根因不是后台进程，而是状态条每 60 秒自检时用 `reg query` 读注册表——`pythonw` 无控制台，每个 console 子进程都会分配一个新控制台窗口。改用 `winreg` 直读（一次自检子进程 6 → 0）+ 新增 `winproc` 统一注入 `CREATE_NO_WINDOW`。途中撞出一个既有 bug：`_ui_queue` 初始化顺序错误导致防断网自检**从上线起从未生效**，一并修复。

**Delta from Previous Save (增量变化)**

- 📁 New files:
  - `wxlive/winproc.py`（54 行，统一隐藏窗口封装）
  - `tests/test_no_console_flash.py`（210 行 / 9 条断言）
  - `docs/GUI黑框闪烁治理.md`（142 行）
  - `Handover Book.md`（本文件）
  - `docs/daily__pipeline_reviews/2026-11-16.md` + `_TEMPLATE.md`
  - `docs/project-state/RESUME.md`
- ✏️ Modified files:
  - `wxlive/systemproxy.py`（+110/-58，`winreg` 直读 + 共享状态）
  - `wxlive/gui_fetch.py`（+48/-23，状态变量前移 + 不可见时跳过自检）
  - `wxlive/fetch.py`（ffmpeg/ffprobe 改 `winproc`）
  - `wxlive/stream_capture.py`（ffprobe/ffmpeg 改 `winproc`）
  - `auto_pipeline.py`（powershell 改 `winproc`）
  - `tests/test_gui_fetch.py`（+1 用例）
  - `TODO.md`
- 🗑️ Deleted files: 无
- 🔑 Key changes: 一次完整自检的子进程调用次数 **6 → 0**，耗时 180ms → 0.94ms；
  防回归测试盯"子进程个数"这个因，而非"看起来不闪"这个果

**Decisions Made This Session**: DEC-001, DEC-002, DEC-003, DEC-004

**Failed Attempts This Session**: FAIL-001, FAIL-002, FAIL-003

**Cognitive State at Close（会话关闭时的思维状态）**

- 主线已收口：修复、测试、文档、提交全部完成，工作区干净，236 passed 连跑三轮。
- 一条未闭合的线索：`tk.tcl` 偶发错误仍未定位。已确认是既有问题（HEAD 对照），
  但根因不明。**下一位接手者别急着用 skip 消掉它**。
- 另一个待验证的细节：`_fetch_view_visible()` 在"最小化到任务栏"时是否真返回 False。
  本轮只在一个 `withdraw()` 的场景验证过不抛异常，**没有验证跳过逻辑真的生效**。

**Context Window Highlights**

- 用户一开始猜"是不是 Go 之类的在后台跑"，这个假设被证伪。真正的洞察是：
  **间歇性 UI 现象先怀疑周期性动作，而周期性动作先查自己代码里的 `after()` 定时器。**
- 用户明确要求"我不想每次隔段时间屏幕就闪烁一下"——这不是洁癖，是可用性问题。

**Session Reflect Report**

- **R1 — 成功模式**
  - **"定量验证优先"救了这个任务。** 如果只满足于"黑框不见了"，就会停在
    "加个 flags"（FAIL-003）。正是因为绕回来看"一次自检到底起了几个进程"，
    才发现根因是轮询方式选错，而不是调用姿势不对。
  - **防回归测试盯因不盯果。** 把 `subprocess.run` patch 成炸弹 + 全仓扫描，
    这两条断言以后能自动拦住同类问题，而不是等用户再报"又闪了"。
  - **HEAD worktree 对照实验**（`git worktree add /tmp/head-probe HEAD`）是判断
    "这个 bug 是不是我改出来的"的高效手段，比反复 stash 干净。本轮用它证明了
    `tk.tcl` 错误与改动无关。

- **R2 — 边界案例**
  - **两次探针假阴性**（FAIL-001/002）。连续两次测出"现象无法复现"，
    差点得出"用户描述有误"的错误结论。转折点是想清楚"我测的对象本身
    就不可能体现出差异"。
  - **`ReadMe.md` / `README.md` 冲突**。skill 要求写 `ReadMe.md`，
    但那个名字在 Windows 上就是 GitHub 的 `README.md`。
    直接按 skill 执行会摧毁已提交的文档——这类"规则与现实的冲突"需要停下来判断，
    不能机械执行（见 DEC-003）。

- **R3 — 改进信号**
  - **下次做"间歇性 UI 现象"排查时，第一步就该问"有没有周期性定时器"，而不是
    "有没有后台进程"。** 本轮绕了远路（先去想 Go 后台），虽然最终收敛，但
    次序可以更好。
  - **验证探针本身要先自检。** 两次假阴性都是"探针没有能力体现差异"。
    以后写验证脚本前先问一句："如果 bug 存在，我这段代码能看出来吗？"
  - **project-save skill 的文件名假设需要适配 Windows**（`ReadMe.md` vs `README.md`）。
    已在 DEC-003 记录，但 skill 层面若继续在其他 Windows 项目复用，会重复踩。

**Compact File Tree Snapshot**

```
14-WeChat_live/
├── README.md                    # GitHub 面向用户说明（勿覆盖）
├── Handover Book.md             # ← 本文件（交接簿）
├── TODO.md
├── auto_pipeline.py
├── 启动.bat / 启动-调试.bat
├── wxlive/                      # 27 个模块
│   ├── winproc.py               # ★本轮新增：统一隐藏窗口
│   ├── systemproxy.py           # ★winreg 直读
│   ├── gui_fetch.py             # ★状态变量前移
│   ├── audio_capture.py / stream_capture.py
│   ├── gemini_asr.py / quality.py / transcriber.py
│   ├── lexicon.py / postprocess.py / render.py
│   └── ...
├── tests/                       # 18 个文件 / 236 用例
│   ├── test_no_console_flash.py # ★本轮新增 9 条
│   └── test_gui_fetch.py        # ★+1 条
├── docs/                        # 13 篇文档
│   ├── GUI黑框闪烁治理.md        # ★本轮新增
│   ├── daily__pipeline_reviews/ # ★本轮建立
│   └── project-state/RESUME.md  # ★本轮建立
├── lexicon/ / config/ / output/
└── tools/
```

---
