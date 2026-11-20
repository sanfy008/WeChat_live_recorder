"""自动发现可用的 LLM / ASR 模型。

两个来源：
1. 本地 magpie 网关的 /v1/models —— 列出所有可用模型
2. 内置推荐列表 —— 标注每个模型适合干什么（转写 vs 整理）

GUI 打开时会自动拉一次，拉不到就用内置列表，不阻塞用户操作。
"""

from __future__ import annotations

import json
import urllib.request

DEFAULT_ENDPOINT = "http://127.0.0.1:3425/v1"

# 内置推荐：按用途分组，并标注实测结论
KNOWN_ASR = [
    ("google-vertex/gemini-4-hydrogen", "Gemini 4 Hydrogen", "最快，实测推荐（2倍速音频也准确）"),
    ("google-vertex/gemini-4-argon", "Gemini 4 Argon", "最准，但慢 2 倍"),
    ("google-vertex/gemini-3.8-flash", "Gemini 3.8 Flash", "较快，可能输出繁体（已自动转简）"),
    ("google-vertex/gemini-4-carbon", "Gemini 4 Carbon", "备选"),
    ("google-vertex/gemini-4-argon", "Gemini 4 Argon", "备选"),
]

# 文本整理模型（做摘要、纠错、暗语展开、要点提取）
KNOWN_TEXT = [
    ("workbuddy/deepseek-v4.1-flash", "DeepSeek V4.1 Flash (WorkBuddy)", "便宜快速，适合批量纠错"),
    ("imported/DeepSeek-V4-Pro", "DeepSeek V4 Pro", "更强，适合深度整理"),
    ("imported/DeepSeek-V4-Flash", "DeepSeek V4 Flash", "极便宜"),
    ("group/deepseek", "DeepSeek 路由组", "可能撞风控，不稳定"),
    ("anyrouter/claude-opus-5-5", "Claude Opus 5.5", "长文整理质量高"),
    ("anyrouter/gpt-6.1-sol", "GPT-6.1 Sol", "综合能力强"),
    ("google-vertex/gemini-4-argon", "Gemini 4 Argon", "可兼做文本整理"),
]

LOCAL_SENSEVOICE = [
    ("sensevoice-small-int8", "SenseVoice Small INT8（本地首选）",
     "RTF ~0.08、内存 ~473MB、全简体带标点"),
]

LOCAL_WHISPER = [
    ("tiny", "Whisper Tiny（本地）", "极快，质量差，只用于测试"),
    ("base", "Whisper Base（本地）", "快，质量一般"),
    ("small", "Whisper Small（本地备选）", "同音字错多但永不失效"),
    ("medium", "Whisper Medium（本地）", "CPU 上很慢，不推荐"),
    ("large-v3", "Whisper Large v3（本地）", "需 GPU"),
]


def fetch_models(base_url: str = DEFAULT_ENDPOINT, api_key: str = "magpie-alma",
                 timeout: int = 6) -> list[str]:
    """从 OpenAI 兼容端点拉模型列表。失败返回空列表，绝不抛异常阻断 GUI。"""
    url = base_url.rstrip("/") + "/models"
    try:
        req = urllib.request.Request(url, headers={"Authorization": f"Bearer {api_key}"})
        with urllib.request.urlopen(req, timeout=timeout) as r:  # noqa: S310
            data = json.loads(r.read().decode())
        items = data.get("data") or []
        out = []
        for it in items:
            mid = it.get("id") if isinstance(it, dict) else None
            if mid:
                out.append(str(mid))
        return sorted(set(out))
    except Exception:
        return []


def merge_asr_options(discovered: list[str]) -> list[tuple[str, str, str]]:
    """把拉到的模型与内置推荐合并，只保留能转写的（Gemini 系列）+ 本地模型。"""
    opts = list(KNOWN_ASR)
    have = {x[0] for x in opts}
    for mid in discovered:
        low = mid.lower()
        if ("gemini" in low) and mid not in have:
            opts.append((mid, mid.split("/")[-1], "自动发现"))
            have.add(mid)
    opts.extend(LOCAL_SENSEVOICE)
    opts.extend(LOCAL_WHISPER)
    # 去重保序
    seen, uniq = set(), []
    for o in opts:
        if o[0] not in seen:
            uniq.append(o)
            seen.add(o[0])
    return uniq


def resolve_asr_model_fields(selected: str, current: dict | None = None) -> dict:
    """把 ASR 下拉框选中项映射到 AsrConfig 字段，避免本地模型误写进 gemini_model。"""
    cur = dict(current or {})
    val = (selected or "").strip()
    whisper_ids = {x[0] for x in LOCAL_WHISPER}
    sensevoice_ids = {x[0] for x in LOCAL_SENSEVOICE}
    if val in sensevoice_ids:
        cur["local_backend"] = "sensevoice"
    elif val in whisper_ids:
        cur["local_backend"] = "whisper"
        cur["model_size"] = val
    elif val:
        cur["gemini_model"] = val
    return cur


def display_asr_model(asr_cfg) -> str:
    """按当前引擎选择界面应展示的模型 ID。"""
    if (getattr(asr_cfg, "engine", "") or "").lower() == "local":
        backend = (getattr(asr_cfg, "local_backend", "sensevoice") or "sensevoice").lower()
        if backend == "sensevoice":
            return LOCAL_SENSEVOICE[0][0]
        return getattr(asr_cfg, "model_size", "small")
    return getattr(asr_cfg, "gemini_model", KNOWN_ASR[0][0])


def merge_text_options(discovered: list[str]) -> list[tuple[str, str, str]]:
    """文本整理模型候选：内置推荐 + 自动发现的非 gemini 聊天模型。"""
    opts = list(KNOWN_TEXT)
    have = {x[0] for x in opts}
    for mid in discovered:
        low = mid.lower()
        if mid in have:
            continue
        if any(k in low for k in ("deepseek", "claude", "gpt", "glm", "kimi",
                                  "minimax", "qwen", "gemini")):
            opts.append((mid, mid, "自动发现"))
            have.add(mid)
    seen, uniq = set(), []
    for o in opts:
        if o[0] not in seen:
            uniq.append(o)
            seen.add(o[0])
    return uniq
