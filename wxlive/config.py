"""集中配置。所有可调参数集中在此，避免散落在各处。"""

from dataclasses import dataclass, field
import os
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
OUTPUT_DIR = Path(os.environ.get("WXLIVE_OUTPUT_DIR") or (ROOT / "output"))
RECORDINGS_DIR = OUTPUT_DIR / "recordings"
TRANSCRIPTS_DIR = OUTPUT_DIR / "transcripts"

# 文本整理（纠错 / 要点）的出厂默认值。
# 模型选择的实测依据：workbuddy/deepseek-v4.1-flash 与 group/deepseek
# 在 27 批纠错里都是 27/27，但 group/deepseek 会撞风控
# （实测 HTTP 403/502、返回行数不符），故默认用前者。
DEFAULT_TEXT_MODEL = "workbuddy/deepseek-v4.1-flash"
DEFAULT_REASONING_EFFORT = "low"

# workbuddy/deepseek-v4.1-flash 的合法思考档位。
# 除这三档之外的取值会被网关静默接受，所以必须显式校验：
# 实测 group/deepseek 传 medium 返回 200 但行为不稳；
# 2026-11-26 39 轮复测确认 max 耗时 10x 且 403/502 静默零纠错，已禁用。
VALID_REASONING_EFFORTS = ("none", "low", "high")


def validate_reasoning_effort(value: str | None) -> str:
    """校验并归一化思考档位；非法值抛 ValueError（不静默接受）。"""
    v = (value or "").strip().lower()
    if v not in VALID_REASONING_EFFORTS:
        raise ValueError(
            f"非法思考强度 {value!r}，只支持 {'/'.join(VALID_REASONING_EFFORTS)}")
    return v


@dataclass
class AudioConfig:
    """采集参数。"""

    # 录制输出的目标采样率。WASAPI loopback 只能按设备原生率（常见 48k）打开，
    # 所以采集回调里保持原生率，在消费侧（LoopbackRecorder.read）重采样到该值。
    # ASR 统一用 16k，16k WAV 体积是 48k 的 1/3，且崩溃后仍完整可读。
    sample_rate: int = 16000
    # 采集块大小（秒）。0.1s = 1600 samples，兼顾延迟与回调开销。
    chunk_seconds: float = 0.1
    # 设备名过滤关键字；为空则用系统默认 loopback 设备。
    device_hint: str = ""
    # 单声道（ASR 不需要立体声，且能减半数据量）
    mono: bool = True


@dataclass
class VadConfig:
    """语音活动检测参数。"""

    # 块能量低于该阈值视为静音（16k 归一化 float 的 RMS）
    silence_rms: float = 0.004
    # 连续静音超过该秒数 → 切分一个片段
    min_silence_seconds: float = 0.7
    # 片段最短长度，太短丢弃（咳嗽、键盘声）
    min_speech_seconds: float = 0.5
    # 云端引擎的合并阈值：Gemini 单次 HTTP 往返固定开销约 3 秒，
    # 切太碎会让 RTF 飙到 3.0（实测 1.1s 音频耗时 3.5s）。
    # 所以云端模式下，短于该值的片段先攒着，攒够再一次性发出。
    cloud_min_segment_seconds: float = 8.0
    # 片段最长长度，超时强制切分（防止内存堆积、保证实时性）。
    # 直播场景能量 VAD 常找不到静音间隙（主播连说 + 背景 BGM），靠它兜底。
    # 但不宜太短：切得太碎会破坏句子完整性，ASR 反而更容易错。
    # 经验值 25-30s：whisper 内部的 Silero VAD 会在这个窗口内做精细切句，
    # 所以外层粗切 + 内层精切，是准确率最高的组合。
    max_segment_seconds: float = 28.0


@dataclass
class AsrConfig:
    """转写参数。"""

    # 引擎选择：
    #   "local"  = 纯本地（默认 SenseVoice-Small INT8，隐私安全、快且零幻觉）
    #   "gemini" = magpie 网关调 Gemini（依赖网络+云端额度）
    #   "auto"   = Gemini 优先，失败自动熔断降级到本地
    #
    # 2026-11-20 i7-6700 真机实测：SenseVoice-Small INT8 (2t) RTF 0.08、
    # 峰值内存 ~473MB、无自回归幻觉，日常直接用本地最稳；云端改为按需手切。
    engine: str = "local"
    # 本地 ASR 后端：
    #   "sensevoice" = sherpa-onnx SenseVoice-Small INT8（默认首选）
    #   "whisper"    = faster-whisper（兼容备选）
    local_backend: str = "sensevoice"
    sensevoice_model: str = "csukuangfj/sherpa-onnx-sense-voice-zh-en-ja-ko-yue-2024-07-17"
    local_threads: int = 2
    # Gemini 模型。实测质量排序：4-argon（最准）> 3.8-flash（快，但会串繁体）≈ 4-hydrogen（最快）
    gemini_model: str = "gcp-ai/gemini-4-hydrogen"
    gemini_endpoint: str = "http://127.0.0.1:3425/v1/chat/completions"
    gemini_api_key: str = "magpie-alma"
    # Gemini 并发路数。只对云端引擎生效，本地 whisper 走 CPU 并发无益。
    #
    # 实测（2026-11-17 复测，真实录音 2400s / 22 块，每档跑 2 次）：
    #   4 路 69.2s / 8 路 37.2s（1.86x）/ 12 路 37.5s（1.85x）
    # 8 路是拐点，12 路无额外收益。早期注释写「8 路因争抢回落到 1.6x」已被复测否定。
    #
    # 注意：af5e530 那次「默认 4 → 8」只改了 cli.py 的 argparse 默认值，
    # 这里长期停留在 4，导致 GUI / Config() 路径（非 CLI）实际只跑 4 路。
    # 现在统一到 8，避免两条路径行为不一致。
    gemini_workers: int = 8

    # faster-whisper 模型：tiny/base/small/medium/large-v3
    # 中文场景 CPU 实时性优先选 small；有 GPU 可用 medium/large-v3
    model_size: str = "small"
    device: str = "cpu"  # cpu / cuda
    compute_type: str = "int8"  # int8 / float16 / float32
    language: str = "zh"
    beam_size: int = 5
    # 是否做 VAD 预过滤（faster-whisper 内置 Silero VAD）
    vad_filter: bool = True
    # 是否加载 XSC 领域词表（学校名/暗语/术语/错字纠正）。
    # 开启后会：1) 注入 ASR prompt 提升专有名词识别；
    #           2) 转写后按实测错字表纠正；
    #           3) 输出时把暗语展开成"暗语（正式名）"便于检索。
    use_lexicon: bool = True
    # 外部词表路径（为 None 用默认 lexicon/xsc.json，与内置表合并）
    lexicon_path: str = ""

    # 初始提示词。默认置空 —— 这是实测踩出来的结论：
    # 一旦给了 prompt，whisper 会把它当"上文"来续写，
    # 在音频不清楚的片段直接复读 prompt 的内容（实测出现
    # "包含口语表达,互动语表达,互动语表达…" 这类无意义重复）。
    # 本地引擎的领域偏置改由 lexicon 注入（措辞已规避复读风险）。
    initial_prompt: str = ""


@dataclass
class TextConfig:
    """文本后处理参数（LLM 纠错 / 要点整理）。"""

    model: str = DEFAULT_TEXT_MODEL
    # 思考档位。见 VALID_REASONING_EFFORTS —— 只有这三档合法，
    # 且必须显式校验，非法档会被网关静默接受（实测 medium 返回 200 但行为不稳，max 已禁用）。
    reasoning_effort: str = DEFAULT_REASONING_EFFORT


@dataclass
class OutputConfig:
    """输出参数。"""

    # 是否保留原始 WAV（强烈建议保留：ASR 模型会迭代，原始音频不会贬值）
    keep_audio: bool = True
    # 每转录完一个片段就落盘一次，防止程序崩溃丢数据
    flush_interval_segments: int = 1


@dataclass
class Config:
    audio: AudioConfig = field(default_factory=AudioConfig)
    vad: VadConfig = field(default_factory=VadConfig)
    asr: AsrConfig = field(default_factory=AsrConfig)
    text: TextConfig = field(default_factory=TextConfig)
    output: OutputConfig = field(default_factory=OutputConfig)
    session_name: str = ""


def ensure_dirs() -> None:
    for d in (OUTPUT_DIR, RECORDINGS_DIR, TRANSCRIPTS_DIR):
        d.mkdir(parents=True, exist_ok=True)
