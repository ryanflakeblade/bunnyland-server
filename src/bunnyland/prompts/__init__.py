"""Prompt generation (spec 16): one foundation for humans and LLMs.

The builder assembles a structured ``PromptContext`` from ECS state and projections, then
renders it to text. Mechanics own their own fragments (needs, etc.) and are injected as
fragment providers, so the builder holds no domain-specific phrasing of its own.
"""

from .builder import (
    PerceivedPromptEvent,
    PromptBuilder,
    PromptContext,
    render_compact_prompt,
    render_prompt,
)
from .context import (
    ComponentPromptContext,
    PerspectiveName,
    PerspectivePhrase,
    PromptAccess,
    PromptPerspective,
)
from .facts import (
    DETAILED_DETAIL_CUTOFF,
    STANDARD_DETAIL_CUTOFF,
    PromptFact,
    PromptFactLike,
)
from .filters import (
    AutomaticPromptFilter,
    PromptFilterContext,
    PromptFilterDefinition,
    PromptFilterRuntime,
    apply_prompt_filters,
)

__all__ = [
    "AutomaticPromptFilter",
    "ComponentPromptContext",
    "DETAILED_DETAIL_CUTOFF",
    "PromptFact",
    "PromptFactLike",
    "PromptFilterContext",
    "PromptFilterDefinition",
    "PromptFilterRuntime",
    "PerspectiveName",
    "PerspectivePhrase",
    "PerceivedPromptEvent",
    "PromptAccess",
    "PromptBuilder",
    "PromptContext",
    "PromptPerspective",
    "STANDARD_DETAIL_CUTOFF",
    "apply_prompt_filters",
    "render_compact_prompt",
    "render_prompt",
]
