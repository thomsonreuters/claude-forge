"""Data models for multi-model review.

Defines model specifications, review results, and the default
model catalog. Models declare identity and provider refs; concrete
routing is derived at runtime by ``forge.review.routing``.
"""

from __future__ import annotations

from dataclasses import dataclass, field, replace
from typing import Literal

PromptMode = Literal["override", "prefix"]


@dataclass(frozen=True)
class ModelSpec:
    """A model backend for multi-model review.

    Attributes:
        name: Human-readable identifier (e.g., "gpt-5.5").
        model_id: Forge-canonical model ID (e.g., "gpt-5.5").
        family: Model family (e.g., "openai", "anthropic", "gemini").
        provider_refs: Ordered provider preference as (namespace, model_ref)
            pairs. ``("direct", "claude-opus-4-6")`` means direct Anthropic;
            ``("openrouter", "openai/gpt-5.5")`` means OpenRouter routing.
        description: What this model is good at.
        preferred_proxy: Catalog recommendation for proxy routing (soft,
            overridable). None for direct-only models.
        prompt: Per-worker prompt override. When set, this worker receives
            this prompt according to prompt_mode.
        prompt_mode: "override" means prompt replaces the global prompt.
            "prefix" means prompt is prepended to the global prompt as a hint.
        worker_id: Stable key for JSON output. Defaults to ``name`` when None.
    """

    name: str
    model_id: str
    family: str
    provider_refs: tuple[tuple[str, str], ...]
    description: str
    preferred_proxy: str | None = None
    prompt: str | None = None
    prompt_mode: PromptMode = "override"
    worker_id: str | None = None

    @property
    def effective_worker_id(self) -> str:
        """Stable key for result maps and JSON output."""
        return self.worker_id if self.worker_id is not None else self.name


@dataclass
class ReviewResult:
    """Result from one model's review."""

    model_name: str
    stdout: str
    stderr: str
    success: bool
    duration_seconds: float
    error: str | None = None


@dataclass
class MultiReviewOutput:
    """Aggregate output from a multi-model review run."""

    prompt: str
    results: list[ReviewResult] = field(default_factory=list)

    @property
    def successful(self) -> int:
        return sum(1 for r in self.results if r.success)

    @property
    def failed(self) -> int:
        return sum(1 for r in self.results if not r.success)


_CLAUDE_47_BOUNDED_REVIEW_PROMPT = """\
You are the Claude Opus 4.7 bounded-review worker in a Forge quorum.

Use the provided target and prompt as the complete task scope. Prefer concrete
evidence over broad narrative: cite file:line locations for every substantive
finding, quote only the minimum necessary code, and separate confirmed issues
from hypotheses. Do not rely on vague prior referents or unstated conversation
history. If the prompt lacks a needed target, say exactly what is missing.
"""


def _build_available_models() -> dict[str, ModelSpec]:
    """Build available review models from the model catalog.

    Model names derive from model_catalog.yaml so updating defaults is
    a single YAML change. Provider-specific model refs come from the
    corresponding proxy template tier configs.
    """
    from forge.core.models.catalog import get_default_model

    openai_opus = get_default_model("openai", "opus")
    gemini_opus = get_default_model("gemini", "opus")
    anthropic_opus = get_default_model("anthropic", "opus")
    deepseek_opus = get_default_model("deepseek", "opus")
    minimax_opus = get_default_model("minimax", "opus")
    qwen_opus = get_default_model("qwen", "opus")
    glm_opus = get_default_model("glm", "opus")
    kimi_opus = get_default_model("kimi", "opus")

    return {
        openai_opus: ModelSpec(
            name=openai_opus,
            model_id=openai_opus,
            family="openai",
            provider_refs=(("openrouter", "openai/gpt-5.5"), ("litellm", "openai/gpt-5.5")),
            preferred_proxy="openrouter-openai",
            description="Logical problems, systematic code review",
        ),
        gemini_opus: ModelSpec(
            name=gemini_opus,
            model_id=gemini_opus,
            family="gemini",
            provider_refs=(
                ("openrouter", "google/gemini-3.1-pro-preview"),
                ("litellm", "google/gemini-3.1-pro-preview"),
            ),
            preferred_proxy="openrouter-gemini",
            description="Balanced analysis, pragmatic suggestions, large context",
        ),
        deepseek_opus: ModelSpec(
            name=deepseek_opus,
            model_id=deepseek_opus,
            family="deepseek",
            provider_refs=(("openrouter", "deepseek/deepseek-v4-pro"),),
            preferred_proxy="openrouter-deepseek",
            description="Cost-efficient reasoning, strong code analysis",
        ),
        minimax_opus: ModelSpec(
            name=minimax_opus,
            model_id=minimax_opus,
            family="minimax",
            provider_refs=(("openrouter", "minimax/minimax-m2.7"),),
            preferred_proxy="openrouter-minimax",
            description="Cost-efficient agentic analysis, broad coverage",
        ),
        qwen_opus: ModelSpec(
            name=qwen_opus,
            model_id=qwen_opus,
            family="qwen",
            provider_refs=(("openrouter", "qwen/qwen3.6-max-preview"),),
            preferred_proxy="openrouter-qwen",
            description="Large context multilingual analysis",
        ),
        glm_opus: ModelSpec(
            name=glm_opus,
            model_id=glm_opus,
            family="glm",
            provider_refs=(("openrouter", "z-ai/glm-5.1"),),
            preferred_proxy="openrouter-glm",
            description="Cost-efficient general analysis",
        ),
        kimi_opus: ModelSpec(
            name=kimi_opus,
            model_id=kimi_opus,
            family="kimi",
            provider_refs=(("openrouter", "moonshotai/kimi-k2.6"),),
            preferred_proxy="openrouter-kimi",
            description="Agentic code generation and analysis",
        ),
        "claude-opus": ModelSpec(
            name="claude-opus",
            model_id="claude-opus",
            family="anthropic",
            provider_refs=(("direct", anthropic_opus),),
            description="Deep architectural analysis, complex reasoning",
        ),
        "claude-opus-4.6": ModelSpec(
            name="claude-opus-4.6",
            model_id="claude-opus-4.6",
            family="anthropic",
            provider_refs=(("direct", "claude-opus-4-6"),),
            description="Stable Claude Opus 4.6 direct worker",
        ),
        "claude-opus-4.6-1m": ModelSpec(
            name="claude-opus-4.6-1m",
            model_id="claude-opus-4.6-1m",
            family="anthropic",
            provider_refs=(("direct", "claude-opus-4-6[1m]"),),
            description="Stable Claude Opus 4.6 direct worker with 1M context pin",
        ),
        "claude-opus-4.7": ModelSpec(
            name="claude-opus-4.7",
            model_id="claude-opus-4.7",
            family="anthropic",
            provider_refs=(("direct", "claude-opus-4-7"),),
            description="Bounded single-shot review and quorum dissent",
            prompt=_CLAUDE_47_BOUNDED_REVIEW_PROMPT,
            prompt_mode="prefix",
        ),
    }


def _build_default_model_names() -> tuple[str, ...]:
    """Return the semantically chosen default quorum model names."""
    from forge.core.models.catalog import get_default_model

    return (
        get_default_model("openai", "opus"),
        get_default_model("gemini", "opus"),
        "claude-opus",
    )


def _build_model_aliases(available: dict[str, ModelSpec]) -> dict[str, str]:
    """Return convenience aliases mapped to canonical workflow model names."""
    from forge.core.models.catalog import get_compact_name, get_default_model

    aliases: dict[str, str] = {}
    for family in ("openai", "gemini", "deepseek", "minimax", "qwen", "glm", "kimi"):
        canonical = get_default_model(family, "opus")
        compact = get_compact_name(canonical)
        if compact != canonical and compact not in available:
            aliases[compact] = canonical
    return aliases


# Proxy_ids match Forge template names (forge proxy create <template>).
AVAILABLE_MODELS: dict[str, ModelSpec] = _build_available_models()
MODEL_ALIASES: dict[str, str] = _build_model_aliases(AVAILABLE_MODELS)
_DEFAULT_MODEL_NAMES: tuple[str, ...] = _build_default_model_names()
DEFAULT_MODELS: dict[str, ModelSpec] = {name: AVAILABLE_MODELS[name] for name in _DEFAULT_MODEL_NAMES}


def resolve_model_specs(names_str: str | None) -> list[ModelSpec]:
    """Parse comma-separated model names into ModelSpec list.

    Returns all DEFAULT_MODELS when names_str is None.
    Raises ValueError for unknown model names.
    """
    if not names_str:
        return list(DEFAULT_MODELS.values())

    names = [m.strip() for m in names_str.split(",")]
    invalid = [m for m in names if m not in AVAILABLE_MODELS and m not in MODEL_ALIASES]
    if invalid:
        available = list(AVAILABLE_MODELS.keys()) + sorted(MODEL_ALIASES.keys())
        raise ValueError(f"Unknown models: {invalid}. Available: {available}")

    specs: list[ModelSpec] = []
    for name in names:
        if name in AVAILABLE_MODELS:
            specs.append(AVAILABLE_MODELS[name])
            continue
        canonical = MODEL_ALIASES[name]
        specs.append(replace(AVAILABLE_MODELS[canonical], worker_id=name))
    return specs


def available_model_specs() -> list[ModelSpec]:
    """Return every selectable workflow model spec."""
    return list(AVAILABLE_MODELS.values())


@dataclass(frozen=True)
class ModelAvailability:
    """Availability status for a model backend."""

    spec: ModelSpec
    status: str  # "ready" | "unavailable" | "error"
    reason: str  # empty when ready


def check_model_availability(
    specs: list[ModelSpec] | None = None,
    timeout_s: float = 1.0,
) -> list[ModelAvailability]:
    """Check route availability for each model via the routing chain.

    Delegates to ``resolve_subprocess_routing()`` per spec. Does not
    fail on unavailable models -- returns status for each.
    """
    from forge.core.reactive.routing import resolve_subprocess_routing
    from forge.review.routing import derive_model_routes

    if specs is None:
        specs = list(DEFAULT_MODELS.values())

    results: list[ModelAvailability] = []

    for spec in specs:
        try:
            routes = derive_model_routes(spec)

            direct_only = bool(routes) and all(r.provider == "direct" for r in routes)
            if direct_only:
                from forge.core.auth.template_secrets import resolve_env_or_credential

                if resolve_env_or_credential("ANTHROPIC_API_KEY"):
                    results.append(ModelAvailability(spec=spec, status="ready", reason=""))
                else:
                    results.append(
                        ModelAvailability(
                            spec=spec,
                            status="unavailable",
                            reason="ANTHROPIC_API_KEY not configured",
                        )
                    )
                continue

            result = resolve_subprocess_routing(
                preferred_proxy=spec.preferred_proxy,
                routes=routes,
                require_route=True,
            )

            if result.route is not None:
                results.append(ModelAvailability(spec=spec, status="ready", reason=""))
            else:
                results.append(
                    ModelAvailability(
                        spec=spec,
                        status="unavailable",
                        reason=result.warning or "No compatible proxy found",
                    )
                )
        except Exception as e:
            results.append(ModelAvailability(spec=spec, status="error", reason=str(e)))

    return results


NAMED_ROLES: dict[str, str] = {
    "security": ("Focus on security vulnerabilities, injection risks, " "auth bypasses, and data exposure."),
    "performance": ("Focus on performance bottlenecks, memory usage, " "algorithmic complexity, and I/O patterns."),
    "architecture": ("Focus on architectural alignment, coupling, " "abstraction quality, and design patterns."),
    "maintainability": ("Focus on readability, complexity, test coverage, " "naming, and change isolation."),
    "correctness": ("Focus on logical errors, edge cases, " "off-by-one errors, and invariant violations."),
}


_VALID_STANCES = frozenset({"for", "against", "neutral", "custom"})


@dataclass
class StanceSpec:
    """A stance-injected worker for adversarial evaluation.

    Attributes:
        stance: One of "for", "against", "neutral", "custom".
        stance_prompt: Text injected via ``{stance_prompt}`` replacement.
        model: Which model runs this stance.
        display_label: User-facing label for output. Falls back to stance when None.
            Use for custom stances where the raw stance ("custom") is not informative.
    """

    stance: str
    stance_prompt: str
    model: ModelSpec
    display_label: str | None = None

    def __post_init__(self) -> None:
        if self.stance not in _VALID_STANCES:
            raise ValueError(f"Invalid stance '{self.stance}'. Must be one of: {sorted(_VALID_STANCES)}")

    @property
    def effective_label(self) -> str:
        """Label for output display and worker naming."""
        return self.display_label if self.display_label is not None else self.stance


@dataclass
class RoleSpec:
    """A role-assigned worker for consensus building.

    Unlike StanceSpec, role is not validated against a fixed set because
    custom role prompts are first-class.

    Attributes:
        role: Role name (key from NAMED_ROLES) or "custom".
        role_prompt: Text injected via ``{role_prompt}`` replacement.
        model: Which model runs this role.
        display_label: User-facing label for output. Falls back to role when None.
    """

    role: str
    role_prompt: str
    model: ModelSpec
    display_label: str | None = None

    @property
    def effective_label(self) -> str:
        """Label for output display and worker naming."""
        return self.display_label if self.display_label is not None else self.role


@dataclass
class ConsensusOutput:
    """Aggregate output from a two-round consensus workflow.

    ``role_map`` keyed by worker_id is the authoritative role mapping
    for disambiguation when duplicate models exist.
    """

    subject: str
    roles: list[str] = field(default_factory=list)
    round1_results: list[ReviewResult] = field(default_factory=list)
    round2_results: list[ReviewResult] = field(default_factory=list)
    role_map: dict[str, str] = field(default_factory=dict)
    reconciliation_brief: str = ""

    @property
    def successful(self) -> int:
        """Count successful workers in Round 2 (final output)."""
        return sum(1 for r in self.round2_results if r.success)

    @property
    def failed(self) -> int:
        """Count failed workers in Round 2 (final output)."""
        return sum(1 for r in self.round2_results if not r.success)


@dataclass
class AdversarialOutput:
    """Aggregate output from an adversarial evaluation run."""

    resource_path: str
    stances: list[str] = field(default_factory=list)
    results: list[ReviewResult] = field(default_factory=list)
    stance_map: dict[str, str] = field(default_factory=dict)

    @property
    def successful(self) -> int:
        return sum(1 for r in self.results if r.success)

    @property
    def failed(self) -> int:
        return sum(1 for r in self.results if not r.success)
