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

__version__ = "0.1.0"
