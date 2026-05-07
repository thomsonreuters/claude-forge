"""LLM client implementations.

Currently implemented:
- LiteLLMClient: For both remote and local LiteLLM instances

Deferred (not yet implemented):
- AnthropicClient: Direct Anthropic API
"""

from .litellm import LiteLLMClient
from .openai_compat import ToolCallAccumulator

__all__ = ["LiteLLMClient", "ToolCallAccumulator"]
