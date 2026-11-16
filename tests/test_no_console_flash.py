"""子进程不许闪黑框（GUI 从 pythonw 启动时的真实故障）。

**故障现象**（2026-11-16 King 报的）
    打开 GUI 后，隔一段时间屏幕就闪一下黑色命令提示框，一闪而逝。

**根因**
    ``启动.bat`` 用 ``pythonw.exe`` 起 GUI，进程**没有控制台**。
    Windows 的规则：无控制台进程去启动一个 console 程序时，系统会为子进程
    **新分配一个控制台窗口**。原先下载页状态条每 60 秒自检一次系统代理，
    而一次自检 = ``describe()`` + ``diagnose()`` = 2 次 ``read()`` = **6 个
    ``reg query``**，且全都没带 ``CREATE_NO_WINDOW`` —— 于是每分钟闪 6 下。

**修法（两层，缺一不可）**
    1. 读注册表改用 ``winreg`` 直读，同进程内完成 → 一次自检**零子进程**
    2. 所有仍然需要起的子进程（reg add / ffmpeg / ffprobe / powershell）
       统一走 ``wxlive.winproc.run``，带上 ``CREATE_NO_WINDOW``

这里的断言都盯"子进程个数"和"creationflags 是否为 0"，
因为这才是屏幕上闪不闪的直接原因 —— 不是"快不快"。

**实测证据**（2026-11-16 用 pythonw 起探针做的对照，不是推断）
子进程是 python.exe（console 子系统），跑起来后读 ``GetConsoleWindow()``：

| 父进程 | creationflags | GetConsoleWindow() |
|---|---|---|
| python.exe（自身有控制台） | 0 | False |
| **pythonw.exe（无控制台）** | **0** | **True ← 新分配了控制台，就是那个黑框** |
| **pythonw.exe（无控制台）** | **CREATE_NO_WINDOW** | **False ← 修好** |

两点值得记：
1. 只有"父进程本身没有控制台"时才会新开窗口。同一个子进程从
   ``python.exe`` 起就完全正常 —— 所以这个 bug 只在 pythonw 下出现，
   用命令行跑测试永远复现不了。
2. ``EnumWindows`` 找 ``ConsoleWindowClass`` 抓不到，因为控制台窗口归属
   conhost.exe 而不是子进程。判断"有没有控制台"要用 ``GetConsoleWindow()``。
"""

import subprocess
import sys

import pytest

from wxlive import winproc


# ---------- 第 1 层：读注册表不起子进程 ----------

def test_read_proxy_state_spawns_no_subprocess(monkeypatch):
    """``read()+describe()+diagnose()`` 一整轮自检必须零子进程。

    这是最核心的一条：状态条 60 秒自检一次，只要这里起进程就一定会闪。
    """
    from wxlive import systemproxy as sp

    spawned = []

    def spy_run(cmd, *a, **kw):
        spawned.append(cmd)
        raise AssertionError(f"自检不该起子进程，却调用了：{cmd}")

    def spy_popen(cmd, *a, **kw):
        spawned.append(cmd)
        raise AssertionError(f"自检不该起子进程，却调用了：{cmd}")

    monkeypatch.setattr(subprocess, "run", spy_run)
    monkeypatch.setattr(subprocess, "Popen", spy_popen)
    # winproc 里持有 subprocess 的模块引用，一并换掉才拦得住
    monkeypatch.setattr(winproc.subprocess, "run", spy_run)
    monkeypatch.setattr(winproc.subprocess, "Popen", spy_popen)

    st = sp.read()
    sp.describe(st)
    sp.diagnose(st)

    assert spawned == [], f"自检起了 {len(spawned)} 个子进程：{spawned}"


def test_read_all_returns_expected_shape():
    """``read_all`` 的返回结构不能变 —— 上层多处依赖这三个键。"""
    from wxlive import systemproxy as sp

    st = sp.read_all()
    assert set(st) == {"enabled", "server", "override"}
    assert isinstance(st["enabled"], bool)
    assert isinstance(st["server"], str)
    assert isinstance(st["override"], str)


def test_describe_and_diagnose_accept_shared_state(monkeypatch):
    """传入 st 时不该再去读注册表 —— 状态条靠这个把 2 次读降到 1 次。"""
    from wxlive import systemproxy as sp

    calls = {"n": 0}

    def counting_read():
        calls["n"] += 1
        return {"enabled": True, "server": sp.FIXED_PROXY, "override": ""}

    monkeypatch.setattr(sp, "read", counting_read)
    st = {"enabled": True, "server": sp.FIXED_PROXY, "override": ""}
    text = sp.describe(st)
    diag = sp.diagnose(st)

    assert calls["n"] == 0, "传了 st 还去读了注册表"
    assert "正常" in text
    assert "正常" in diag


def test_describe_diagnose_fall_back_when_state_omitted():
    """不传 st 时仍要能工作（向后兼容旧调用点）。"""
    from wxlive import systemproxy as sp

    assert sp.describe()
    assert sp.diagnose()


# ---------- 第 2 层：需要起的子进程都得隐藏窗口 ----------

def test_winproc_run_sets_create_no_window(monkeypatch):
    """``winproc.run`` 必须带上 CREATE_NO_WINDOW。"""
    if sys.platform != "win32":
        pytest.skip("Windows 专属")

    seen = {}

    def fake_run(cmd, **kw):
        seen.update(kw)
        return subprocess.CompletedProcess(cmd, 0, "", "")

    monkeypatch.setattr(winproc.subprocess, "run", fake_run)
    winproc.run(["cmd"])

    assert seen.get("creationflags", 0) & 0x08000000, \
        f"没带 CREATE_NO_WINDOW：creationflags={seen.get('creationflags')}"


def test_winproc_popen_sets_create_no_window(monkeypatch):
    """``winproc.popen`` 同样要带。"""
    if sys.platform != "win32":
        pytest.skip("Windows 专属")

    seen = {}

    class FakeProc:
        pass

    def fake_popen(cmd, **kw):
        seen.update(kw)
        return FakeProc()

    monkeypatch.setattr(winproc.subprocess, "Popen", fake_popen)
    winproc.popen(["cmd"])

    assert seen.get("creationflags", 0) & 0x08000000


def test_winproc_does_not_override_caller_flags(monkeypatch):
    """调用方显式给了 creationflags 时以调用方为准。"""
    seen = {}

    def fake_run(cmd, **kw):
        seen.update(kw)
        return subprocess.CompletedProcess(cmd, 0, "", "")

    monkeypatch.setattr(winproc.subprocess, "run", fake_run)
    winproc.run(["cmd"], creationflags=0x00000200)
    assert seen["creationflags"] == 0x00000200


def test_hidden_kwargs_empty_on_non_windows(monkeypatch):
    """非 Windows 平台不该塞 creationflags（POSIX 不认这个参数）。"""
    monkeypatch.setattr(winproc.sys, "platform", "linux")
    assert winproc.hidden_kwargs() == {}


# ---------- 全仓扫描：别再有人写裸 subprocess ----------

def test_no_bare_subprocess_spawn_left_in_product_code():
    """产品代码里不该再有裸 ``subprocess.run/Popen``（winproc 自身除外）。

    这条是防回归的：以后新加一个调用点忘了隐藏窗口，屏幕又会闪，
    而且是"隔段时间闪一下"这种极难复现的现象。
    """
    from pathlib import Path

    root = Path(__file__).resolve().parent.parent
    offenders = []

    for path in list(root.glob("wxlive/*.py")) + [root / "auto_pipeline.py"]:
        if path.name == "winproc.py":
            continue
        if not path.exists():
            continue
        for lineno, line in enumerate(
                path.read_text(encoding="utf-8", errors="replace").splitlines(), 1
        ):
            stripped = line.strip()
            if stripped.startswith("#"):
                continue
            # 文档字符串/注释里的说明文字不算（含"旧实现"字样的是注释说明）
            if '"""' in stripped or stripped.startswith("旧实现"):
                continue
            for pat in ("subprocess.run(", "subprocess.Popen("):
                if pat in line and "winproc" not in line:
                    offenders.append(f"{path.name}:{lineno}: {stripped}")

    assert not offenders, (
        "以下位置直接起了子进程，窗口会闪（请改用 winproc.run / winproc.popen）：\n"
        + "\n".join(offenders)
    )
