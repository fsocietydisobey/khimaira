"""Shared schemas across khimaira packages.

Anything that crosses package boundaries lives here so we have one canonical
definition. Imports from khimaira, scarlet, seance, specter all resolve here.
"""

__version__ = "0.1.0.dev0"

from .classification import (
    ComplexityTier,
    TaskClassification,
    TaskType,
    ThinkingLevel,
)
from .context import ContextBundle, FileContext, ResolutionSource
from .routing import RoutingDecision
from .runtime import ComponentHealth, ComponentStatus, RuntimeStatus
from .usage import Provider, Source, UsageRecord

__all__ = [
    # classification (AMR)
    "TaskClassification",
    "TaskType",
    "ComplexityTier",
    "ThinkingLevel",
    # context (resolver)
    "FileContext",
    "ContextBundle",
    "ResolutionSource",
    # usage (tracker)
    "UsageRecord",
    "Provider",
    "Source",
    # routing (router decision)
    "RoutingDecision",
    # runtime (khimaira dev status)
    "RuntimeStatus",
    "ComponentHealth",
    "ComponentStatus",
]
