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
"""

from __future__ import annotations

import subprocess
import sys

# 本机固定出口代理（MEMORY.md 记录：长期不变）
FIXED_PROXY = "127.0.0.1:10808"
# 抓流工具的 MITM 端口（它接管系统代理时写这个值）
TOOL_PROXY = "127.0.0.1:2023"

_REG_PATH = r"HKCU\Software\Microsoft\Windows\CurrentVersion\Internet Settings"


def _reg_query(name: str) -> str | None:
    """读一个注册表值；读不到返回 None。"""
    try:
        r = subprocess.run(["reg", "query", _REG_PATH, "/v", name],
                           capture_output=True, text=True, timeout=10)
    except Exception:  # noqa: BLE001
        return None
    if r.returncode != 0:
        return None
    for line in (r.stdout or "").splitlines():
        parts = line.split()
        # 形如：    ProxyServer    REG_SZ    127.0.0.1:2023
        if len(parts) >= 3 and parts[0] == name:
            return parts[-1]
    return None


def _reg_set(name: str, value: str, kind: str = "REG_SZ") -> bool:
    try:
        r = subprocess.run(["reg", "add", _REG_PATH, "/v", name,
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
    enabled_raw = _reg_query("ProxyEnable")
    enabled = False
    if enabled_raw:
        try:
            enabled = int(enabled_raw, 0) == 1
        except ValueError:
            enabled = enabled_raw.strip() == "1"
    return {
        "enabled": enabled,
        "server": _reg_query("ProxyServer") or "",
        "override": _reg_query("ProxyOverride") or "",
    }


def is_tool_takeover() -> bool:
    """系统代理是否正被抓流工具接管（指向 TOOL_PROXY）。"""
    st = read()
    return bool(st["enabled"]) and TOOL_PROXY in (st["server"] or "")


def is_fixed_proxy() -> bool:
    """系统代理是否已是本机固定出口。"""
    st = read()
    return bool(st["enabled"]) and FIXED_PROXY in (st["server"] or "")


def describe() -> str:
    """给用户看的一行状态描述。"""
    st = read()
    if not st["enabled"]:
        return "系统代理：已关闭（直连）"
    server = st["server"] or "?"
    if TOOL_PROXY in server:
        return f"系统代理：{server}（抓流工具接管中，关掉工具会断网）"
    if FIXED_PROXY in server:
        return f"系统代理：{server}（正常）"
    return f"系统代理：{server}（不是预期的 {FIXED_PROXY}）"


def restore_fixed() -> tuple[bool, str]:
    """把系统代理还原成本机固定出口 10808。

    返回 (是否成功, 说明)。已经是固定值时不重复写注册表，直接报"无需还原" ——
    写注册表是系统级副作用，能不做就不做。
    """
    st = read()
    if st["enabled"] and st["server"] == FIXED_PROXY:
        return True, "系统代理已经是 " + FIXED_PROXY + "，无需还原"

    ok_enable = _reg_set("ProxyEnable", "1", kind="REG_DWORD")
    ok_server = _reg_set("ProxyServer", FIXED_PROXY)
    if ok_enable and ok_server:
        refresh_wininet()
        after = read()
        if after["enabled"] and after["server"] == FIXED_PROXY:
            return True, "已还原系统代理为 " + FIXED_PROXY
        return False, f"写入后读回不一致：{after['server']!r}"
    return False, "写注册表失败（可能需要管理员权限）"


def tool_still_running() -> bool:
    """抓流工具是否还在监听 2023（用 TCP 连接探测，不依赖进程名）。"""
    import socket

    try:
        with socket.create_connection(("127.0.0.1", 2023), timeout=1.0):
            return True
    except OSError:
        return False


def diagnose() -> str:
    """体检用：一句话说清现在该不该动手。"""
    st = read()
    if not st["enabled"]:
        return "系统代理已关闭，微信流量不会经过抓流工具，抓不到内容"
    server = st["server"] or ""
    if TOOL_PROXY in server:
        alive = tool_still_running()
        if alive:
            return (f"系统代理 {server}（工具在跑）。"
                    f"用完记得关工具或点「还原系统代理」")
        return (f"⚠ 系统代理仍指向 {server}，但工具已不在运行 —— "
                f"现在正在断网，请点「还原系统代理」")
    if FIXED_PROXY in server:
        return f"系统代理 {server}，正常"
    return f"⚠ 系统代理是 {server}，不是预期的 {FIXED_PROXY}"
