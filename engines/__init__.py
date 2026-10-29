"""转录引擎注册表。"""

from .base import BaseEngine
from .bcut import BcutEngine
from .faster_whisper import FasterWhisperEngine
from .jianying import JianYingEngine
from .mlx_whisper import MLXWhisperEngine
from .openai_asr import OpenAIASREngine
from .siliconflow import SiliconFlowEngine

ENGINES = {
    "bcut": BcutEngine,
    "jianying": JianYingEngine,
    "siliconflow": SiliconFlowEngine,
    "openai-asr": OpenAIASREngine,
    "faster-whisper": FasterWhisperEngine,
    "mlx-whisper": MLXWhisperEngine,
}

# 引擎元信息：给 Web 界面与 CLI 共用
ENGINE_META = {
    "bcut": {
        "label": "B站必剪",
        "requires_key": False,
        "has_timing": True,
        "local": False,
        "note": "免费、免登录，毫秒级时间戳。默认首选。",
    },
    "jianying": {
        "label": "剪映",
        "requires_key": False,
        "has_timing": True,
        "local": False,
        "note": "免费，但依赖第三方签名服务，可能失效。",
    },
    "siliconflow": {
        "label": "硅基流动",
        "requires_key": True,
        "has_timing": False,
        "local": False,
        "note": "需 SILICONFLOW_API_KEY。Qwen3-ASR，只返回整段文本，无时间戳。",
    },
    "openai-asr": {
        "label": "OpenAI Whisper 兼容",
        "requires_key": True,
        "has_timing": True,
        "local": False,
        "note": "任意兼容 /audio/transcriptions 的服务：OpenAI、Groq、自建等。",
    },
    "faster-whisper": {
        "label": "faster-whisper（本地）",
        "requires_key": False,
        "has_timing": True,
        "local": True,
        "note": "本地推理，跨平台，需先下载模型。",
    },
    "mlx-whisper": {
        "label": "MLX Whisper（本地）",
        "requires_key": False,
        "has_timing": True,
        "local": True,
        "note": "Apple Silicon 专用，速度最快。其他平台不可用。",
    },
}


def create_engine(name: str, **kwargs) -> BaseEngine:
    if name not in ENGINES or ENGINES[name] is None:
        raise ValueError(f"未知引擎: {name}（可选: {', '.join(sorted(k for k, v in ENGINES.items() if v))}）")
    return ENGINES[name](**kwargs)
