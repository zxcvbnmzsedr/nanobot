"""Signed, institution-scoped Skill marketplace support."""

from importlib import import_module

_LAZY_EXPORTS = {
    "SkillArtifactError": (".errors", "SkillArtifactError"),
    "SkillMarketError": (".errors", "SkillMarketError"),
    "SkillMarketService": (".service", "SkillMarketService"),
    "SkillMarketSettings": (".settings", "SkillMarketSettings"),
    "SkillOperationResult": (".models", "SkillOperationResult"),
    "SkillPackageLimits": (".settings", "SkillPackageLimits"),
    "SkillSnapshot": (".models", "SkillSnapshot"),
    "SyncResult": (".models", "SyncResult"),
}


def __getattr__(name: str):
    target = _LAZY_EXPORTS.get(name)
    if target is None:
        raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
    module = import_module(target[0], __name__)
    value = getattr(module, target[1])
    globals()[name] = value
    return value

__all__ = [
    "SkillArtifactError",
    "SkillMarketError",
    "SkillMarketService",
    "SkillMarketSettings",
    "SkillOperationResult",
    "SkillPackageLimits",
    "SkillSnapshot",
    "SyncResult",
]
