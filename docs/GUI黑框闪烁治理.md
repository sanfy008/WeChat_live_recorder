# GUI 黑框闪烁治理（子进程控制台窗口）

## 现象

打开 GUI 之后，隔一段时间屏幕就闪一下黑色命令提示框，一闪而逝，但一直反复。

## 根因

`启动.bat` 用 `pythonw.exe` 起 GUI。`pythonw.exe` 是 **GUI 子系统**程序，
进程**没有控制台**。

Windows 的规则：

> 一个没有控制台的进程，去启动一个 **console 子系统**程序（`reg.exe`、
> `ffmpeg.exe`、`powershell.exe`、`python.exe`……），系统会为子进程
> **新分配一个控制台窗口**。

下载页的状态条每 **60 秒**自检一次系统代理。旧实现一次自检的调用链：

```
_fetch_proxy_poll()
  → _fetch_refresh_proxy()
      → systemproxy.describe()  → read() → 3 次 reg query
      → systemproxy.diagnose()  → read() → 3 次 reg query
```

**6 个子进程，全部没带 `CREATE_NO_WINDOW`** → 每分钟闪 6 下。

这就是"隔段时间闪一下"的来源。不是 Go 或什么后台守护在跑，
纯粹是它自己在轮询自己的状态条。

## 决定性证据

用 `pythonw.exe` 起探针（父进程无控制台），子进程是真 `python.exe`，
跑起来后读 `GetConsoleWindow()`：

| 父进程 | creationflags | `GetConsoleWindow()` |
|---|---|---|
| `python.exe`（自身有控制台） | 0 | `False` |
| **`pythonw.exe`（无控制台）** | **0** | **`True` ← 新分配了控制台，就是那个黑框** |
| **`pythonw.exe`（无控制台）** | **`CREATE_NO_WINDOW`** | **`False` ← 修好** |

两个踩过的坑，值得记：

1. **这个 bug 只在 `pythonw` 下出现。** 同一份代码从命令行（`python.exe`）
   跑完全正常 —— 所以靠跑测试永远复现不了，必须用 `pythonw` 起。
2. **`EnumWindows` 抓 `ConsoleWindowClass` 抓不到。** 控制台窗口归属
   `conhost.exe` 而不是子进程，按 PID 过滤会得到 0 个。
   判断"有没有控制台"要用 `GetConsoleWindow()`。

## 修法（两层）

### 第 1 层：读注册表不再起子进程

`systemproxy.read()` 改用 `winreg` 直读，同进程内完成。

| 指标 | 旧（`reg query` × 6） | 新（`winreg`） |
|---|---|---|
| 子进程数 / 一次自检 | **6** | **0** |
| 耗时 | ~180ms（6 × 30ms） | **0.94ms** |
| 屏幕闪烁 | 每分钟 6 下 | 无 |

同时把 `describe()` / `diagnose()` 改成可复用同一份状态，避免各读一遍。
写入（`reg add`）仍然走子进程 —— 那要 `/f` 强制覆盖，且只在用户点
「还原系统代理」按钮时发生一次，不是一个周期动作。

### 第 2 层：所有子进程统一隐藏窗口

新增 `wxlive/winproc.py`，提供 `winproc.run` / `winproc.popen`，
自动注入 `CREATE_NO_WINDOW`（非 Windows 平台不加）。

覆盖到的调用点：

| 位置 | 用途 | 触发频率 |
|---|---|---|
| `systemproxy._reg_set` | `reg add` 写代理 | 用户点按钮 |
| `fetch.extract_audio` | ffmpeg 抽音轨 | 每次下载 |
| `fetch.probe_media` | ffprobe 探时长 | 每次解析 |
| `stream_capture.probe_stream` | ffprobe 探流 | 每次拉流前 |
| `stream_capture.StreamRecorder.start` | ffmpeg 录流 | 每次拉流 |
| `auto_pipeline.recorder_alive` | powershell 查进程 | 守护轮询 |

## 顺带优化

窗口不可见时（最小化/隐藏）跳过自检。用户看不见状态条，查了也没人看；
等窗口重新显示时手动刷新即可。判断不出来时按"可见"处理，不会漏检。

## 防回归

`tests/test_no_console_flash.py` 共 9 条，盯两件事：

1. **一次完整自检必须零子进程** —— 直接 patch `subprocess.run/Popen`
   为炸弹，一调用就失败。
2. **全仓扫描**：产品代码里出现裸 `subprocess.run(` / `subprocess.Popen(`
   就失败（`winproc.py` 自身除外），并指出文件行号。

这样以后新加调用点忘了隐藏窗口，测试会立刻拦住 —— 而不是等用户
"隔段时间看到屏幕闪一下"再来报。

## 顺带修掉的既有 bug（重要）

修这个问题时撞出来的，**不是本次引入**。

`_build_fetch_tab()` 第 95 行附近会调 `_fetch_refresh_proxy()` 做启动探测，
它内部走 `_ui()` → `self._ui_queue.put()`。但 `_ui_queue`（以及
`_fetch_cancel` / `_fetch_items` / `_fetch_proxy_after`）原本在同一方法
**末尾**才创建。

后果：

- 启动时后台线程抛 `AttributeError: '_tkinter.tkapp' object has no attribute '_ui_queue'`
- 界面上**只表现为状态条永远停在「读取中…」** —— 看起来像"读得慢"，
  实际是崩了，而且异常在后台线程里、不会弹窗
- 每 60 秒自检走同一条路径，**同样抛异常** → 防断网自检从上线起就没生效过

修法：把这 5 个状态变量提前到方法开头，早于任何可能触发的回调。

回归测试 `test_fetch_state_ready_before_any_callback`，已验证回退修复后该用例
会失败（报"启动时缺少 _ui_queue，回调会抛 AttributeError"）。
修复后状态条正确显示 `系统代理：127.0.0.1:10808（正常）`。

## 验证

```
236 passed（连跑三轮全绿）
```

端到端：模拟 10 轮状态条自检（约 10 分钟 GUI 运行）

| 指标 | 修复前 | 修复后 |
|---|---|---|
| 子进程启动次数 | 60 | **0** |
| 其中会闪窗的 | 60 | **0** |

真实环境：用 `pythonw -m wxlive gui` 启动实际 GUI，连续监控 **75 秒**
（跨过至少一次 60 秒自检）：

```
t=5s … t=75s   reg.exe=0  ffmpeg=0   （每 5 秒采样，共 15 次，全部为零）
```

修复前同样场景应能在 60 秒节点看到 6 个 `reg.exe` 短暂出现。
