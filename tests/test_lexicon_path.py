"""回归：lexicon_path 失效时必须自动纠正，不能静默回退内置词表。

背景（2026-11-17 体检）：

`config/user.json` 里曾写入绝对路径（形如 D: + 项目目录 + lexicon/xsc.json）。
项目一换位置，`lexicon.load()` 里 `if p.exists()` 不成立，就**静默回退内置表**——
外部词表加的那些校名/暗语全部失效，而界面上没有任何提示。

这类"配置存在但没生效"的问题最难发现：没有报错，只是准确率悄悄下降。
所以必须有测试盯着，且要覆盖"故意给一个坏路径"的场景。
"""

from __future__ import annotations

from pathlib import Path

import pytest

from wxlive.config import ROOT
from wxlive.userconfig import _normalize_lexicon_path


class _Asr:
    def __init__(self, path: str) -> None:
        self.lexicon_path = path


class _Cfg:
    def __init__(self, path: str) -> None:
        self.asr = _Asr(path)


def _default_lexicon() -> Path:
    return ROOT / "lexicon" / "xsc.json"


@pytest.mark.skipif(not _default_lexicon().exists(), reason="项目内没有默认词表")
def test_existing_path_is_kept() -> None:
    """路径真实存在时不做任何改动（尊重用户自定义）。"""
    good = str(_default_lexicon().resolve())
    cfg = _Cfg(good)
    _normalize_lexicon_path(cfg)
    assert cfg.asr.lexicon_path == good


@pytest.mark.skipif(not _default_lexicon().exists(), reason="项目内没有默认词表")
def test_missing_absolute_path_falls_back_to_project_copy() -> None:
    """指向别处的坏路径 → 若同名文件在项目 lexicon/ 下，改用项目那份。"""
    cfg = _Cfg("D:/not_exist_anywhere/lexicon/xsc.json")
    _normalize_lexicon_path(cfg)
    assert Path(cfg.asr.lexicon_path).exists()
    assert Path(cfg.asr.lexicon_path).name == "xsc.json"


@pytest.mark.skipif(not _default_lexicon().exists(), reason="项目内没有默认词表")
def test_empty_path_uses_default() -> None:
    cfg = _Cfg("")
    _normalize_lexicon_path(cfg)
    assert Path(cfg.asr.lexicon_path) == _default_lexicon()


@pytest.mark.skipif(not _default_lexicon().exists(), reason="项目内没有默认词表")
def test_unknown_custom_name_falls_back_to_default() -> None:
    """自定义文件名在项目里也不存在 → 退回默认词表，而不是留一个死路径。"""
    cfg = _Cfg("C:/tmp/absolutely_no_such_lexicon.json")
    _normalize_lexicon_path(cfg)
    assert Path(cfg.asr.lexicon_path).exists()
