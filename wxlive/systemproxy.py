"""系统代理读写与还原（Windows）。

**为什么需要这个模块**
    抓流工具（tools/wxcd）工作时会把**系统代理**改成 127.0.0.1:2023
    （它自己的 MITM 端口），靠 proxy.upstreamproxy 再转发到 127.0.0.1:10808。
    一旦工具被关掉/崩溃/任务管理器结束，2023 没人监听，而系统代理**还指着它** ——
    这时所有走系统代理的应用（浏览器、微信、系统更新、部分桌面客户端）全部断网。
    实测：代理指向未监听端口时 curl 报 000 并卡满超时。

    本机固定出口是 127.0.0.1:10808（见 MEMORY.md，长期不变）。
    所以"还原"= 把 ProxyServer 写回 127.0.0.1:10808。

**注意两个层次的代理**
    - 系统代理（注册表 HKCU 下的 Internet Settings）
        → 影响 WinINET/系统级应用：IE/WebView、部分桌面程序、系统设置页
    - 进程环境变量 HTTP_PROXY/HTTPS_PROXY
        → 影响多数 CLI/Python/Node 程序
    工具只改前者，不改后者。所以工具接管期间，curl（吃环境变量）还是通的，
    但浏览器（吃系统代理）会走 2023。

**只做最小必要的事**
    不常驻监控、不自动改注册表（除了用户显式点按钮）。
    提供一个"备份/还原"对，让还原指向已知的固定值 10808 而不是"恢复某次快照" ——
    快照方案在多次进出抓流状态后会失真（备份可能本身就是 2023）。

**读走 winreg，不走 reg.exe（2026-11-16 修）**
    旧实现用 ``subprocess.run(["reg", "query", ...])`` 读，一次 ``read()`` 跑 3 个
    子进程，``describe()`` + ``diagnose()`` 就是 6 个。而 GUI 从 ``pythonw.exe``
    启动、自身没有控制台，Windows 会给每个 console 子进程**新分配一个窗口** ——
    下载页 60 秒自检一次，于是每分钟黑框闪 6 下。
    改成 ``winreg`` 直读：同进程内完成，实测 **0.94ms**、零子进程、零闪烁。
    写入仍用 ``reg add``（要 ``/f`` 强制覆盖，且我们只在用户点按钮时做一次）。
"""

from __future__ import annotations

import sys

from . import winproc

# 本机固定出口代理（MEMORY.md 记录：长期不变）
FIXED_PROXY = "127.0.0.1:10808"
# 抓流工具的 MITM 端口（它接管系统代理时写这个值）
TOOL_PROXY = "127.0.0.1:2023"

_REG_PATH = r"HKCU\Software\Microsoft\Windows\CurrentVersion\Internet Settings"
# winreg 用的相对路径（不含 HKEY_CURRENT_USER 前缀）
_REG_SUBKEY = r"Software\Microsoft\Windows\CurrentVersion\Internet Settings"


def _reg_query(name: str) -> str | None:
    """读一个注册表值；读不到返回 None。

    走 ``winreg`` 直读，不起子进程 —— 见模块文档「读走 winreg」。
    """
    try:
        import winreg

        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, _REG_SUBKEY) as key:
            value, _kind = winreg.QueryValueEx(key, name)
    except (FileNotFoundError, OSError, ImportError):
        return None
    if value is None:
        return None
    return str(value)


def read_all() -> dict:
    """一次打开注册表项读全部三个值。

    比连续三次 ``read()`` 快得多，也避免"读到一半被别人改了"的中间态。
    """
    wanted = ("ProxyEnable", "ProxyServer", "ProxyOverride")
    found: dict[str, str] = {}
    try:
        import winreg

        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, _REG_SUBKEY) as key:
            for name in wanted:
                try:
                    value, _kind = winreg.QueryValueEx(key, name)
                except FileNotFoundError:
                    continue
                if value is not None:
                    found[name] = str(value)
    except (FileNotFoundError, OSError, ImportError):
        found = {}

    enabled_raw = found.get("ProxyEnable")
    enabled = False
    if enabled_raw:
        try:
            enabled = int(enabled_raw, 0) == 1
        except ValueError:
            enabled = enabled_raw.strip() == "1"
    return {
        "enabled": enabled,
        "server": found.get("ProxyServer") or "",
        "override": found.get("ProxyOverride") or "",
    }


def _reg_set(name: str, value: str, kind: str = "REG_SZ") -> bool:
    try:
        r = winproc.run(["reg", "add", _REG_PATH, "/v", name,
                         "/t", kind, "/d", str(value), "/f"],
                        capture_output=True, text=True, timeout=10)
        return r.returncode == 0
    except Exception:  # noqa: BLE001
        return False


def refresh_wininet() -> None:
    """通知系统代理设置已变。

    改完注册表后，很多已运行的程序（包括浏览器）不会立刻感知 ——
    WinINET 有缓存。这里用 InternetSetOption 发两个通知强制刷新。
    失败不影响正确性（重启程序也会生效），所以静默忽略。
    """
    if sys.platform != "win32":
        return
    try:
        import ctypes

        internet_option_settings_changed = 39
        internet_option_refresh = 37
        wininet = ctypes.windll.Wininet
        wininet.InternetSetOptionW(0, internet_option_settings_changed, 0, 0)
        wininet.InternetSetOptionW(0, internet_option_refresh, 0, 0)
    except Exception:  # noqa: BLE001
        pass


def read() -> dict:
    """读当前系统代理状态。

    返回：
        enabled  ProxyEnable 是否为 1
        server   ProxyServer 字符串（如 "127.0.0.1:2023"）
        override ProxyOverride 字符串
    """
    return read_all()


def is_tool_takeover() -> bool:
    """系统代理是否正被抓流工具接管（指向 TOOL_PROXY）。"""
    st = read()
    return bool(st["enabled"]) and TOOL_PROXY in (st["server"] or "")


def is_fixed_proxy() -> bool:
    """系统代理是否已是本机固定出口。"""
    st = read()
    return bool(st["enabled"]) and FIXED_PROXY in (st["server"] or "")


def describe(st: dict | None = None) -> str:
    """给用户看的一行状态描述。

    ``st`` 可由调用方传入，省掉重复读注册表（状态条要同时拿 describe +
    diagnose，传同一个 st 即可只读一次）。
    """
    if st is None:
        st = read()
    if not st["enabled"]:
        return "系统代理：已关闭（直连，抓不到微信内容）"
    server = st["server"] or "?"
    if TOOL_PROXY in server:
        return f"系统代理：{server}（抓流工具接管中，关掉工具会断网）"
    if FIXED_PROXY in server:
        return f"系统代理：{server}（日常出口正常，未经过抓流工具）"
    return f"系统代理：{server}（不是预期的 {FIXED_PROXY}）"


def _apply_proxy(target: str, ok_msg: str) -> tuple[bool, str]:
    """写入 ProxyEnable=1 与 ProxyServer=target，刷新 WinINET 并读回校验。"""
    ok_enable = _reg_set("ProxyEnable", "1", kind="REG_DWORD")
    ok_server = _reg_set("ProxyServer", target)
    if ok_enable and ok_server:
        refresh_wininet()
        after = read()
        if after["enabled"] and after["server"] == target:
            return True, ok_msg
        return False, f"写入后读回不一致：{after['server']!r}"
    return False, "写注册表失败（可能需要管理员权限）"


def restore_fixed() -> tuple[bool, str]:
    """把系统代理还原成本机固定出口 10808。

    返回 (是否成功, 说明)。已经是固定值时不重复写注册表，直接报"无需还原" ——
    写注册表是系统级副作用，能不做就不做。
    """
    st = read()
    if st["enabled"] and st["server"] == FIXED_PROXY:
        return True, "系统代理已经是 " + FIXED_PROXY + "，无需还原"

    return _apply_proxy(FIXED_PROXY, "已还原系统代理为 " + FIXED_PROXY)


def enable_tool_proxy() -> tuple[bool, str]:
    """把系统代理切到抓流工具端口 127.0.0.1:2023。

    切之前必须确认 2023 正在监听，绝不能把系统代理指向死端口导致断网。
    """
    if not tool_still_running():
        return False, (
            f"抓流工具未在 {TOOL_PROXY} 监听，不能切换代理（否则会断网）。"
            "请先点「重启抓流工具」。"
        )
    st = read()
    if st["enabled"] and st["server"] == TOOL_PROXY:
        return True, f"系统代理已经是 {TOOL_PROXY}（抓流工具接管中）"

    return _apply_proxy(
        TOOL_PROXY,
        f"已切换系统代理为 {TOOL_PROXY}（抓流工具接管中）",
    )


def tool_still_running() -> bool:
    """抓流工具是否还在监听 2023（用 TCP 连接探测，不依赖进程名）。"""
    import socket

    try:
        with socket.create_connection(("127.0.0.1", 2023), timeout=1.0):
            return True
    except OSError:
        return False


def diagnose(st: dict | None = None) -> str:
    """体检用：一句话说清现在该不该动手。

    ``st`` 同 :func:`describe`，可复用同一份状态。
    """
    if st is None:
        st = read()
    if not st["enabled"]:
        return "⚠ 系统代理已关闭，微信流量不会经过抓流工具，抓不到内容"
    server = st["server"] or ""
    if TOOL_PROXY in server:
        alive = tool_still_running()
        if alive:
            return (f"系统代理 {server}（抓流工具接管中）。"
                    f"用完记得点「还原系统代理」")
        return (f"⚠ 系统代理仍指向 {server}，但工具已不在运行 —— "
                f"现在正在断网，请点「还原系统代理」")
    if FIXED_PROXY in server:
        return (f"系统代理 {server}，日常出口正常"
                "（未走抓流端口 2023；要抓流请点「接管代理(抓流)」）")
    return f"⚠ 系统代理是 {server}，不是预期的 {FIXED_PROXY}"
