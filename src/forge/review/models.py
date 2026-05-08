"""Data models for multi-model review.

Defines model specifications, review results, and the default
model catalog. Models reference ``proxy`` (proxy_id or template name)
for proxy resolution via ``lookup_proxy_base_url()``.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from typing import Literal

PromptMode = Literal["override", "prefix"]


@dataclass(frozen=True)
class ModelSpec:
    """A model backend for multi-model review.

    Attributes:
        name: Human-readable identifier (e.g., "gpt-5.1").
        proxy: Proxy (proxy_id or template name) for base_url resolution.
            None means direct Anthropic or ambient subprocess proxy routing.
        model_flag: Value for ``claude -p --model <flag>``.
            None means use the proxy's default model.
        direct: When True, bypass proxies and launch with Anthropic credentials.
        direct_model: Claude Code env-ready model pin used for direct workers.
        description: What this model is good at.
        prompt: Per-worker prompt override. When set, this worker receives
            this prompt according to prompt_mode.
        prompt_mode: "override" means prompt replaces the global prompt.
            "prefix" means prompt is prepended to the global prompt as a hint.
        worker_id: Stable key for JSON output. Defaults to ``name`` when None.
            Use when the same model appears multiple times with different roles
            (e.g., ``gpt-5.5-security``, ``gpt-5.5-architecture``).
    """

    name: str
    proxy: str | None
    model_flag: str | None
    description: str
    direct: bool = False
    direct_model: str | None = None
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

    Model names and flags derive from model_catalog.yaml so updating
    defaults is a single YAML change. Dict keys use compact display names
    (e.g., "gemini-3.1-pro" not "gemini-3.1-pro-preview") so the CLI
    surface stays clean.
    """
    from forge.core.models.catalog import get_compact_name, get_default_model

    openai_opus = get_default_model("openai", "opus")
    gemini_opus = get_default_model("gemini", "opus")
    anthropic_opus = get_default_model("anthropic", "opus")

    openai_name = get_compact_name(openai_opus)
    gemini_name = get_compact_name(gemini_opus)

    return {
        openai_name: ModelSpec(
            name=openai_name,
            proxy="litellm-openai",
            model_flag=None,
            description="Logical problems, systematic code review",
        ),
        gemini_name: ModelSpec(
            name=gemini_name,
            proxy="litellm-gemini",
            model_flag=None,
            description="Balanced analysis, pragmatic suggestions, large context",
        ),
        "claude-opus": ModelSpec(
            name="claude-opus",
            proxy=None,
            model_flag=anthropic_opus,
            direct=True,
            direct_model=anthropic_opus,
            description="Deep architectural analysis, complex reasoning",
        ),
        "claude-opus-4.6": ModelSpec(
            name="claude-opus-4.6",
            proxy=None,
            model_flag="claude-opus-4-6",
            direct=True,
            direct_model="claude-opus-4-6",
            description="Stable Claude Opus 4.6 direct worker",
        ),
        "claude-opus-4.6-1m": ModelSpec(
            name="claude-opus-4.6-1m",
            proxy=None,
            model_flag="claude-opus-4-6[1m]",
            direct=True,
            direct_model="claude-opus-4-6[1m]",
            description="Stable Claude Opus 4.6 direct worker with 1M context pin",
        ),
        "claude-opus-4.7": ModelSpec(
            name="claude-opus-4.7",
            proxy=None,
            model_flag="claude-opus-4-7",
            direct=True,
            direct_model="claude-opus-4-7",
            description="Bounded single-shot review and quorum dissent",
            prompt=_CLAUDE_47_BOUNDED_REVIEW_PROMPT,
            prompt_mode="prefix",
        ),
    }


def _build_default_model_names() -> tuple[str, ...]:
    """Return the semantically chosen default quorum model names."""
    from forge.core.models.catalog import get_compact_name, get_default_model

    return (
        get_compact_name(get_default_model("openai", "opus")),
        get_compact_name(get_default_model("gemini", "opus")),
        "claude-opus",
    )


# Proxy_ids match Forge template names (forge proxy create <template>).
AVAILABLE_MODELS: dict[str, ModelSpec] = _build_available_models()
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
    invalid = [m for m in names if m not in AVAILABLE_MODELS]
    if invalid:
        available = list(AVAILABLE_MODELS.keys())
        raise ValueError(f"Unknown models: {invalid}. Available: {available}")

    return [AVAILABLE_MODELS[m] for m in names]


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
    """Check proxy/credential availability for each model.

    Deduplicates proxy health checks internally. Does not fail on
    unavailable models -- returns status for each.
    """
    from forge.core.auth.template_secrets import resolve_env_or_credential
    from forge.core.reactive.env import FORGE_SUBPROCESS_PROXY_VAR
    from forge.core.reactive.proxy import check_proxy_reachable

    if specs is None:
        specs = list(DEFAULT_MODELS.values())

    proxy_cache: dict[str, tuple[str, str, str | None]] = {}
    results: list[ModelAvailability] = []

    for spec in specs:
        if spec.direct:
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

        if spec.proxy is None:
            subprocess_proxy = os.environ.get(FORGE_SUBPROCESS_PROXY_VAR)
            if subprocess_proxy:
                if subprocess_proxy in proxy_cache:
                    status, reason, _url = proxy_cache[subprocess_proxy]
                else:
                    try:
                        reachable, reason, _url = check_proxy_reachable(subprocess_proxy, timeout_s)
                        status = "ready" if reachable else "unavailable"
                    except Exception as e:
                        status, reason, _url = "error", str(e), None
                    proxy_cache[subprocess_proxy] = (status, reason, _url)
                results.append(ModelAvailability(spec=spec, status=status, reason=reason))
                continue

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

        if spec.proxy in proxy_cache:
            status, reason, _url = proxy_cache[spec.proxy]
            results.append(ModelAvailability(spec=spec, status=status, reason=reason))
            continue

        try:
            reachable, reason, _url = check_proxy_reachable(spec.proxy, timeout_s)
            if reachable:
                status, reason = "ready", ""
            else:
                status = "unavailable"
        except Exception as e:
            status, reason, _url = "error", str(e), None

        proxy_cache[spec.proxy] = (status, reason, _url)
        results.append(ModelAvailability(spec=spec, status=status, reason=reason))

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
