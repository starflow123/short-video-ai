"""厂商注册表：业务层按 key 取适配器实例。"""
from __future__ import annotations

from app.services.providers.agnes import AgnesProvider
from app.services.providers.base import BaseProvider, ProviderSpec
from app.services.providers.deepseek import DeepSeekProvider
from app.services.providers.magichour import MagicHourProvider

_REGISTRY: dict[str, type[BaseProvider]] = {
    DeepSeekProvider.spec.key: DeepSeekProvider,
    AgnesProvider.spec.key: AgnesProvider,
    MagicHourProvider.spec.key: MagicHourProvider,
}


def list_specs() -> list[ProviderSpec]:
    return [provider.spec for provider in _REGISTRY.values()]


def get_spec(key: str) -> ProviderSpec | None:
    provider = _REGISTRY.get(key)
    return provider.spec if provider else None


def build_provider(key: str, api_key: str, base_url: str | None = None) -> BaseProvider:
    provider = _REGISTRY.get(key)
    if not provider:
        raise ValueError(f"未知厂商：{key}")
    return provider(api_key=api_key, base_url=base_url)
