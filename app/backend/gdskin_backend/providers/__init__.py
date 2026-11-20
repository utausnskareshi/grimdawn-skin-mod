"""3D 生成 AI の一覧。新しい生成 AI はクラスを書いて REGISTRY に登録する（providers/base.py）。"""
from __future__ import annotations

from ..errors import BackendError
from ..i18n import tr
from .base import (CheckItem, GenerationOutput, GenerationProvider, GenerationRequest, ProgressUpdate,  # noqa: F401
                   ProviderRun)
from .trellis import TrellisProvider

__all__ = ["REGISTRY", "register", "get", "GenerationProvider", "GenerationRequest", "GenerationOutput", "ProgressUpdate",
           "ProviderRun", "CheckItem", "TrellisProvider"]

REGISTRY: dict[str, GenerationProvider] = {}


def register(provider: GenerationProvider) -> None:
    REGISTRY[provider.id] = provider


def get(provider_id: str) -> GenerationProvider:
    p = REGISTRY.get(provider_id)
    if p is None:
        raise BackendError("INVALID_INPUT", tr(f"3D 生成 AI が不明です: {provider_id}（{', '.join(REGISTRY)}）",
                                               f"Unknown 3D generation AI: {provider_id} ({', '.join(REGISTRY)})"))
    return p


register(TrellisProvider())
