"""转录引擎注册表。"""

from .base import BaseEngine
from .bcut import BcutEngine
from .jianying import JianYingEngine
from .siliconflow import SiliconFlowEngine

ENGINES = {
    "bcut": BcutEngine,
    "jianying": JianYingEngine,
    "siliconflow": SiliconFlowEngine,
}


def create_engine(name: str, **kwargs) -> BaseEngine:
    if name not in ENGINES:
        raise ValueError(f"未知引擎: {name}（可选: {', '.join(sorted(ENGINES))}）")
    return ENGINES[name](**kwargs)
