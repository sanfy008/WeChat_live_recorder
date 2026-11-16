"""启动子进程时统一隐藏控制台窗口（Windows）。

**为什么需要这个模块**
    GUI 由 ``pythonw.exe`` 启动，自身**没有控制台**。Windows 的规则是：
    一个没有控制台的进程，去启动一个 console 程序（reg.exe / ffmpeg.exe /
    powershell.exe），系统会为子进程**新分配一个控制台窗口**。
    在桌面上看到的就是一个黑色命令提示框一闪而过。

    这不是无害的观感问题：下载页的状态条每 60 秒自检一次系统代理，
    旧实现一次自检要跑 **6 个** ``reg query``（``describe()`` 和
    ``diagnose()`` 各自把 3 个值读一遍），于是每分钟闪 6 下。

**怎么做**
    给 subprocess 加 ``CREATE_NO_WINDOW``。封装成模块级函数，
    是为了避免每个调用点各写一遍 —— 漏一处就继续闪。

**为什么不干脆去掉自检**
    自检本身有价值（工具崩溃后系统代理会残留指向 2023 端口，导致全网断）。
    真正该删的是"用子进程读注册表"这种笨办法：``winreg`` 直读同进程内完成，
    实测 0.94ms、零子进程。见 ``systemproxy.read()``。
"""

from __future__ import annotations

import subprocess
import sys

# Windows 专属：不为子进程分配控制台窗口
_NO_WINDOW = 0x08000000  # CREATE_NO_WINDOW
_CREATE_NEW_PROCESS_GROUP = 0x00000200


def hidden_kwargs() -> dict:
    """subprocess 的"别弹窗"参数。非 Windows 返回空 dict。"""
    if sys.platform != "win32":
        return {}
    return {"creationflags": getattr(subprocess, "CREATE_NO_WINDOW", _NO_WINDOW)}


def run(cmd, **kwargs):
    """``subprocess.run`` 的隐藏窗口版本。

    调用方若已显式给了 ``creationflags``，以调用方为准（不覆盖）。
    """
    if "creationflags" not in kwargs:
        kwargs.update(hidden_kwargs())
    return subprocess.run(cmd, **kwargs)


def popen(cmd, **kwargs):
    """``subprocess.Popen`` 的隐藏窗口版本。"""
    if "creationflags" not in kwargs:
        kwargs.update(hidden_kwargs())
    return subprocess.Popen(cmd, **kwargs)
