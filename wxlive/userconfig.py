"""用户配置持久化。

GUI 的所有设置存在 `config/user.json`，与代码里的默认值分离：
- 代码里的 dataclass 默认值 = 出厂设置
- user.json = 用户实际选择，只覆盖改过的字段

这样升级代码不会冲掉用户配置，用户也能一键"恢复默认"。
"""

from __future__ import annotations

import json
from dataclasses import asdict, fields, is_dataclass
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parent.parent
USER_CONFIG_PATH = ROOT / "config" / "user.json"

# GUI 允许保存的字段（白名单，避免把内部状态写进配置）
SAVEABLE = {
    "audio": ["device_hint", "chunk_seconds", "mono"],
    "vad": ["silence_rms", "min_silence_seconds", "min_speech_seconds",
            "max_segment_seconds", "cloud_min_segment_seconds"],
    "asr": ["engine", "gemini_model", "gemini_endpoint", "gemini_api_key",
            "model_size", "device", "compute_type", "language",
            "use_lexicon", "lexicon_path", "beam_size", "vad_filter"],
    "text": ["model", "reasoning_effort"],
    "output": ["keep_audio"],
}


def default_config_dict() -> dict:
    """从 dataclass 里取出厂默认值。"""
    from .config import AsrConfig, AudioConfig, OutputConfig, TextConfig, VadConfig

    return {
        "audio": asdict(AudioConfig()),
        "vad": asdict(VadConfig()),
        "asr": asdict(AsrConfig()),
        "text": asdict(TextConfig()),
        "output": asdict(OutputConfig()),
    }


def load_user_config() -> dict:
    """读取用户配置；不存在或损坏时返回空 dict（用默认值）。"""
    if not USER_CONFIG_PATH.exists():
        return {}
    try:
        data = json.loads(USER_CONFIG_PATH.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except Exception as e:  # noqa: BLE001
        print(f"[config] 用户配置读取失败，使用默认值: {e}")
        return {}


def save_user_config(data: dict) -> None:
    USER_CONFIG_PATH.parent.mkdir(parents=True, exist_ok=True)
    # 只保存白名单字段
    clean: dict[str, Any] = {}
    for section, keys in SAVEABLE.items():
        src = data.get(section) or {}
        picked = {k: src[k] for k in keys if k in src}
        if picked:
            clean[section] = picked
    USER_CONFIG_PATH.write_text(
        json.dumps(clean, ensure_ascii=False, indent=2), encoding="utf-8"
    )


def build_config_from_user(base=None):
    """把 user.json 叠加到出厂 Config 上，返回 Config 实例。"""
    from .config import AsrConfig, AudioConfig, Config, OutputConfig, TextConfig, VadConfig

    cfg = base or Config()
    user = load_user_config()

    def _merge(obj, section: str):
        data = user.get(section) or {}
        valid = {f.name for f in fields(obj)}
        for k, v in data.items():
            if k in valid:
                try:
                    setattr(obj, k, v)
                except Exception:
                    pass
        return obj

    cfg.audio = _merge(cfg.audio if isinstance(cfg.audio, AudioConfig) else AudioConfig(), "audio")
    cfg.vad = _merge(cfg.vad if isinstance(cfg.vad, VadConfig) else VadConfig(), "vad")
    cfg.asr = _merge(cfg.asr if isinstance(cfg.asr, AsrConfig) else AsrConfig(), "asr")
    # 向后兼容：老 user.json 没有 text 段时保留出厂默认值，不报错。
    cfg.text = _merge(cfg.text if isinstance(cfg.text, TextConfig) else TextConfig(), "text")
    cfg.output = _merge(cfg.output if isinstance(cfg.output, OutputConfig) else OutputConfig(),
                        "output")
    return cfg


def reset_user_config() -> None:
    if USER_CONFIG_PATH.exists():
        USER_CONFIG_PATH.unlink()
