"""wxlive —— 微信直播录音转录流水线。

Windows 上 Python 默认用 GBK 编码 stdout，遇到 emoji / 部分中文会抛
UnicodeEncodeError 直接崩掉。这里在包导入时统一改成 UTF-8，
保证任何入口（python -m、脚本、测试）都安全。
"""

import sys


def _force_utf8() -> None:
    for name in ("stdout", "stderr"):
        stream = getattr(sys, name, None)
        if stream is None:
            continue
        try:
            if (stream.encoding or "").lower().replace("-", "") != "utf8":
                stream.reconfigure(encoding="utf-8", errors="replace")
        except Exception:
            pass


_force_utf8()


# ============ 版本号 ============
#
# 唯一真相源就是这里。GUI 标题栏、启动日志、`alma` 诊断都从这读，
# 不再各写一份。
#
# 为什么要收口：2026-11-28 排查 401 时发现项目里同时存在三个版本号
# （标题栏 1.0 / 包 0.2.0 / RESUME v0.4.0），而标题栏那个从 11-16
# 首次提交后再没动过。结果就是「屏幕上那个窗口到底是哪一版」根本
# 说不清 —— 和这次故障里「分不清新旧进程」是同一类问题。
#
# 规则：改 GUI 可见行为就要抬这个号（King 的明确要求）。
__version__ = "0.5.0"

# 人类可读的版本标签：GUI 标题栏和启动日志都用它
VERSION_LABEL = f"v{__version__}"


def build_stamp() -> str:
    """返回「运行中代码」的指纹，用来分辨屏幕上是不是旧进程。

    为什么需要：pythonw 起的窗口从不打印任何东西，改了代码但只要没重启，
    界面就还是旧逻辑。这次 401 排查光靠时间戳比对（进程启动 vs 文件 mtime）
    才发现，太绕了。直接算一次源码指纹，标题栏上一看便知新不新。

    取 wxlive/*.py 的 mtime 与大小做短哈希：够区分版本，又不依赖 git
    （用户机器上未必有 git，且 pythonw 下跑 git 会弹控制台黑框）。
    """
    import hashlib
    from pathlib import Path

    h = hashlib.sha256()
    try:
        pkg = Path(__file__).resolve().parent
        for p in sorted(pkg.glob("*.py")):
            try:
                st = p.stat()
                h.update(f"{p.name}:{int(st.st_mtime)}:{st.st_size}".encode())
            except OSError:
                continue
    except Exception:  # noqa: BLE001
        return "unknown"
    return h.hexdigest()[:6]
