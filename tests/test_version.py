"""版本号一致性测试。

## 为什么需要这组测试

2026-11-28 King 问「GUI 的版本号是不是一直没变」，查下来比"没变"更糟：
项目里同时存在三个互不相干的版本号。

| 位置 | 当时的值 | 最后变动 |
|---|---|---|
| 标题栏 `gui.APP_VER` | `1.0` | 2026-11-16 首次提交后**再没动过** |
| 包 `__init__.__version__` | `0.2.0` | 11-17 |
| `RESUME.md: project_version` | `v0.4.0` | 11-27 |

62 个提交、12 天演进，标题栏还写着 v1.0。后果不是"不好看"，而是
**没法回答「屏幕上这个窗口是哪一版」** —— 和这次 401 故障里
「分不清新旧进程、以为是功能缺失」是同一类错误。

所以：版本号收口到 `wxlive.__version__` 唯一真相源，并用测试锁住，
避免下次改 GUI 又忘记抬版本。
"""

from pathlib import Path

import pytest


def test_gui_version_comes_from_package():
    """标题栏版本必须来自包的 __version__，不能另写一份常量。

    这是「单一真相源」的硬约束：gui 里再出现硬编码版本串，这条就红。
    """
    import wxlive
    from wxlive import gui

    assert gui.APP_VER == f"v{wxlive.__version__}", (
        f"标题栏版本 {gui.APP_VER!r} 与包版本 v{wxlive.__version__} 不一致；"
        "版本号只应定义在 wxlive/__init__.py"
    )


def test_gui_version_not_hardcoded_in_source():
    """源码里不能再出现 `APP_VER = "1.0"` 这种硬编码。

    直接查源码文本 —— 因为"值恰好相同"不代表"以后还会同步"。
    """
    src = (Path(__file__).resolve().parent.parent
           / "wxlive" / "gui.py").read_text(encoding="utf-8")
    assert 'APP_VER = "1.0"' not in src, "gui.py 里又出现硬编码版本号了"
    assert "APP_VER = VERSION_LABEL" in src, "APP_VER 应从 VERSION_LABEL 取值"


def test_version_is_semver_like():
    """版本号必须是 x.y.z 形式，否则排序/比较会失真。"""
    import wxlive

    parts = wxlive.__version__.split(".")
    assert len(parts) == 3, f"版本号不是三段式：{wxlive.__version__!r}"
    assert all(p.isdigit() for p in parts), f"版本号含非数字段：{wxlive.__version__!r}"


def test_build_stamp_is_stable_and_short():
    """源码指纹：同一次运行内必须稳定，且足够短便于显示。"""
    import wxlive

    a = wxlive.build_stamp()
    b = wxlive.build_stamp()
    assert a == b, "同一次运行里指纹不该变"
    assert len(a) == 6 and all(c in "0123456789abcdef" for c in a)


def test_resume_version_matches_package():
    """RESUME.md 的 project_version 必须和包版本一致。

    文档说 v0.4、代码是 v0.5 这种漂移，会让人按过期契约工作。
    文件不存在时跳过（例如只拷了 wxlive/ 的场景）。
    """
    import re

    import wxlive

    resume = (Path(__file__).resolve().parent.parent
              / "docs" / "project-state" / "RESUME.md")
    if not resume.exists():
        pytest.skip("RESUME.md 不存在")
    text = resume.read_text(encoding="utf-8")
    m = re.search(r"^project_version:\s*v?(\S+)", text, re.MULTILINE)
    assert m, "RESUME.md 缺少 project_version 字段"
    assert m.group(1) == wxlive.__version__, (
        f"RESUME.md 记为 v{m.group(1)}，包版本是 v{wxlive.__version__}；"
        "抬版本时要一起改"
    )
