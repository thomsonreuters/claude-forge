"""Pydantic models for API request/response validation.

Defines data models for the proxy API, including models for:
- Content blocks (text, images, tool use)
- Messages
- API requests and responses
- Model name mapping between Claude and Gemini
"""

import logging
from typing import Any, Dict, List, Literal, Optional, Union

from pydantic import BaseModel, Field, model_validator

from forge.config import config, is_openai_model

logger = logging.getLogger(__name__)


def _detect_tier(values: dict) -> dict:
    """Detect Claude tier (haiku/sonnet/opus) from model name in request dict.

    Sets `original_model_name`, `tier`, and `has_explicit_tier` fields.
    Used by model_validator(mode="before") on request models.
    """
    if isinstance(values, dict) and "model" in values:
        model_name = values["model"]
        values["original_model_name"] = model_name

        model_lower = model_name.lower()
        if "haiku" in model_lower:
            values["tier"] = "haiku"
            values["has_explicit_tier"] = True
        elif "sonnet" in model_lower:
            values["tier"] = "sonnet"
            values["has_explicit_tier"] = True
        elif "opus" in model_lower:
            values["tier"] = "opus"
            values["has_explicit_tier"] = True
        else:
            values["tier"] = None
            values["has_explicit_tier"] = False

    return values


class CacheControl(BaseModel):
    """Cache control directive for prompt caching (Anthropic API).

    The "ephemeral" type indicates content should be cached for the session.
    Only affects Anthropic/Bedrock models — other providers cache automatically
    or don't support the field.
    """

    type: Literal["ephemeral"] = "ephemeral"


class ContentBlockText(BaseModel):
    type: Literal["text"]
    text: str
    cache_control: Optional[CacheControl] = None


class ContentBlockImageSource(BaseModel):
    type: Literal["base64"]
    media_type: str
    data: str


class ContentBlockImage(BaseModel):
    type: Literal["image"]
    source: ContentBlockImageSource


class ContentBlockToolUse(BaseModel):
    type: Literal["tool_use"]
    id: str
    name: str
    input: Dict[str, Any]


class ContentBlockToolResult(BaseModel):
    type: Literal["tool_result"]
    tool_use_id: str
    content: Union[str, List[Dict[str, Any]]]
    is_error: Optional[bool] = False


class ContentBlockThinking(BaseModel):
    """Anthropic extended thinking block (sent in conversation history on --resume)."""

    type: Literal["thinking"]
    thinking: str = ""
    signature: Optional[str] = None


class ContentBlockRedactedThinking(BaseModel):
    """Anthropic redacted thinking block (opaque, sent back for continuity)."""

    type: Literal["redacted_thinking"]
    data: str = ""


ContentBlock = Union[
    ContentBlockText,
    ContentBlockImage,
    ContentBlockToolUse,
    ContentBlockToolResult,
    ContentBlockThinking,
    ContentBlockRedactedThinking,
]


class SystemContent(BaseModel):
    type: Literal["text"]
    text: str
    cache_control: Optional[CacheControl] = None


class Message(BaseModel):
    role: Literal["user", "assistant"]
    content: Union[str, List[ContentBlock]]


class ToolInputSchema(BaseModel):
    type: Literal["object"] = "object"
    properties: Dict[str, Any]
    required: Optional[List[str]] = None


class ToolDefinition(BaseModel):
    name: str
    description: Optional[str] = None
    input_schema: ToolInputSchema


class MessagesRequest(BaseModel):
    model: str  # Raw client-supplied model string; mapped in handler after config reload
    messages: List[Message]
    system: Optional[Union[str, List[SystemContent]]] = None
    max_tokens: int = Field(ge=1)
    metadata: Optional[Dict[str, Any]] = None
    stop_sequences: Optional[List[str]] = None
    stream: Optional[bool] = False
    temperature: Optional[float] = None
    top_p: Optional[float] = None
    top_k: Optional[int] = None
    # Reasoning/thinking overrides (explicit request overrides are allowed)
    reasoning_effort: Optional[str] = None
    verbosity: Optional[str] = None
    thinking: Optional[Dict[str, Any]] = None
    tools: Optional[List[ToolDefinition]] = None
    tool_choice: Optional[Dict[str, Any]] = None
    original_model_name: Optional[str] = None  # Internal field to store original name pre-mapping
    tier: Optional[str] = None  # Internal field to store detected tier (haiku/sonnet/opus)
    has_explicit_tier: bool = False  # Whether tier was explicit in model name (not defaulted)

    @model_validator(mode="before")
    @classmethod
    def store_original_model(cls, values):
        return _detect_tier(values)


class TokenCountRequest(BaseModel):
    model: str  # Raw client-supplied model string; mapped in handler after config reload
    messages: List[Message]
    system: Optional[Union[str, List[SystemContent]]] = None
    original_model_name: Optional[str] = None  # Internal field
    tier: Optional[str] = None  # Internal field to store detected tier (haiku/sonnet/opus)
    has_explicit_tier: bool = False  # Whether tier was explicit in model name

    @model_validator(mode="before")
    @classmethod
    def store_original_model_token_count(cls, values):
        return _detect_tier(values)


class TokenCountResponse(BaseModel):
    input_tokens: int


class Usage(BaseModel):
    input_tokens: int
    output_tokens: int


class MessagesResponse(BaseModel):
    id: str
    type: Literal["message"] = "message"
    role: Literal["assistant"] = "assistant"
    model: str  # Original Anthropic model name
    content: List[ContentBlock]
    stop_reason: Optional[Literal["end_turn", "max_tokens", "stop_sequence", "tool_use", "content_filtered"]] = None
    stop_sequence: Optional[str] = None
    usage: Usage


def map_model_name(anthropic_model_name: str) -> str:
    """Map Anthropic model names (haiku, sonnet, opus) to backend models.

    Uses unified config for model mappings. Handles:
    - Pass-through for known backend models (openai/, vertex_ai/, gemini/)
    - Mapping Anthropic-style names to current provider's tier equivalents
    - Default provider fallback for ambiguous names

    Returns:
        The mapped model name for the backend provider.
    """
    original = anthropic_model_name
    preferred = config.proxy.preferred_provider or None

    def _normalize(name: str) -> str:
        n = name.strip().lower().split("@", 1)[0]
        for prefix in ("anthropic/", "openai/", "gemini/"):
            if n.startswith(prefix):
                n = n[len(prefix) :]
                break
        return n

    def _anthropic_flavor(name: str) -> str | None:
        if "haiku" in name:
            return "haiku"
        if "sonnet" in name:
            return "sonnet"
        if "opus" in name:
            return "opus"
        return None

    def _is_openai(name: str) -> bool:
        return is_openai_model(name)

    def _is_gemini(name: str) -> bool:
        # Use unified config for Gemini model detection
        known = {
            config.proxy.gemini.tiers.haiku.lower(),
            config.proxy.gemini.tiers.sonnet.lower(),
            config.proxy.gemini.tiers.opus.lower(),
        }
        return name.startswith("gemini-") or name in known

    def _is_litellm(name: str) -> bool:
        """Check if model name is a LiteLLM model (has provider prefix)."""
        return "/" in name and any(
            name.startswith(prefix)
            for prefix in [
                "openai/",
                "anthropic/",
                "vertex_ai/",
                "bedrock/",
                "replicate/",
                "together_ai/",
                "gemini/",  # Local LiteLLM with Google GenAI SDK
            ]
        )

    def _get_provider_models(provider_name: str) -> dict[str, str]:
        """Get tier->model mappings from unified config."""
        provider = config.proxy.get_provider(provider_name)
        return {
            "haiku": provider.tiers.haiku,
            "sonnet": provider.tiers.sonnet,
            "opus": provider.tiers.opus,
            "default": provider.tiers.sonnet,
        }

    name = _normalize(original)
    flavor = _anthropic_flavor(name)

    # OpenRouter: pass-through model IDs as-is (OpenRouter handles routing)
    if preferred == "openrouter":
        if "/" in original:
            logger.info(f"Using OpenRouter model: '{original}' (pass-through)")
            return original

        # Map Anthropic flavors to OpenRouter tier models
        provider_models = _get_provider_models("openrouter")
        if flavor:
            mapped = provider_models[flavor]
            logger.info(f"Mapping '{original}' ({flavor.title()}) -> OpenRouter '{mapped}'")
            return mapped

        mapped = provider_models["default"]
        logger.warning(f"Unknown model '{original}' with provider preference 'openrouter', defaulting to '{mapped}'")
        return mapped

    # Forced provider: symmetric handling for OpenAI, Gemini, and LiteLLM
    if preferred in ("openai", "gemini", "litellm"):
        target = preferred

        # Pass-through if already the target provider
        if (
            (target == "openai" and _is_openai(name))
            or (target == "gemini" and _is_gemini(name))
            or (target == "litellm" and _is_litellm(original))
        ):
            # Return original for LiteLLM to preserve the provider prefix
            result = original if target == "litellm" else name
            logger.info(f"Using {target} model: '{result}' (provider preference: {target})")
            return result

        # Map Anthropic flavors to the target provider
        provider_models = _get_provider_models(target)
        if flavor:
            mapped = provider_models[flavor]
            logger.info(f"Mapping '{original}' ({flavor.title()}) -> {target.title()} '{mapped}'")
            return mapped

        # Otherwise default to target provider's default
        mapped = provider_models["default"]
        logger.warning(
            f"Unknown/other model '{original}' with provider preference '{target}', defaulting to '{mapped}'"
        )
        return mapped

    # No forced provider: pass-through known provider models
    if _is_litellm(original):
        logger.info(f"Detected LiteLLM model: '{original}'")
        return original
    if _is_openai(name):
        logger.info(f"Detected OpenAI model: '{original}' -> '{name}'")
        return name
    if _is_gemini(name):
        logger.info(f"Detected Gemini model: '{original}' -> '{name}'")
        return name

    # Anthropic or unknown: map Anthropic by flavor, else default to Gemini
    target = "gemini"
    provider_models = _get_provider_models(target)
    if flavor:
        mapped = provider_models[flavor]
        logger.info(f"Mapping '{original}' ({flavor.title()}) -> {target.title()} '{mapped}'")
        return mapped

    # Fail-closed: reject completely unknown models rather than silently routing to default
    raise ValueError(
        f"Unrecognized model '{original}'. Cannot route to backend. "
        "Check model name or configure a mapping in the proxy template."
    )
