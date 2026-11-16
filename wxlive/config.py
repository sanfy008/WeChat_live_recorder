"""集中配置。所有可调参数集中在此，避免散落在各处。"""

from dataclasses import dataclass, field
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
OUTPUT_DIR = ROOT / "output"
RECORDINGS_DIR = OUTPUT_DIR / "recordings"
TRANSCRIPTS_DIR = OUTPUT_DIR / "transcripts"


@dataclass
class AudioConfig:
    """采集参数。"""

    # 目标采样率。ASR 统一用 16k，采集端直接降采样可省一半 IO。
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
    #   "local"  = 纯本地 faster-whisper（隐私安全、永不失效，质量中等）
    #   "gemini" = magpie 网关调 Gemini（质量高、快，但依赖网络+云端额度）
    #   "auto"   = Gemini 优先，失败自动熔断降级到本地（推荐）
    #
    # 实测：Gemini 中文准确率显著高于 whisper，RTF 0.07-0.14 vs 0.41；
    # 但有 120s 单次上限和内容安全过滤风险，所以必须有本地兜底。
    engine: str = "auto"
    # Gemini 模型。实测质量排序：4-argon（最准）> 3.8-flash（快，但会串繁体）≈ 4-hydrogen（最快）
    gemini_model: str = "google-vertex/gemini-4-hydrogen"
    gemini_endpoint: str = "http://127.0.0.1:3425/v1/chat/completions"
    gemini_api_key: str = "magpie-alma"

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
    output: OutputConfig = field(default_factory=OutputConfig)
    session_name: str = ""


def ensure_dirs() -> None:
    for d in (OUTPUT_DIR, RECORDINGS_DIR, TRANSCRIPTS_DIR):
        d.mkdir(parents=True, exist_ok=True)
