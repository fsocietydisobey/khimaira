"""System prompts for each AI role."""

from .architect import ARCHITECT_SYSTEM_PROMPT
from .brainstorm import BRAINSTORM_SYSTEM_PROMPT
from .brainstorm_research import BRAINSTORM_RESEARCH_SYSTEM_PROMPT
from .classifier import CLASSIFIER_SYSTEM_PROMPT
from .research import RESEARCH_SYSTEM_PROMPT

__all__ = [
    "RESEARCH_SYSTEM_PROMPT",
    "ARCHITECT_SYSTEM_PROMPT",
    "CLASSIFIER_SYSTEM_PROMPT",
    "BRAINSTORM_SYSTEM_PROMPT",
    "BRAINSTORM_RESEARCH_SYSTEM_PROMPT",
]
