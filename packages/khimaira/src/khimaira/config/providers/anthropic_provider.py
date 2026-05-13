"""Anthropic (Claude) provider."""

import os
import time

from anthropic import AsyncAnthropic

from khimaira.usage import get_recorder

from .base import Provider


class AnthropicProvider(Provider):
    """Routes requests to Claude via the Anthropic API."""

    def __init__(
        self,
        model: str = "claude-sonnet-4-20250514",
        api_key: str | None = None,
        max_tokens: int = 4096,
    ):
        self._model = model
        self._max_tokens = max_tokens
        self._client = AsyncAnthropic(
            api_key=api_key or os.environ.get("ANTHROPIC_API_KEY")
        )

    @property
    def name(self) -> str:
        return f"Claude ({self._model})"

    async def generate(self, prompt: str, system_prompt: str = "") -> str:
        """Send a message to Claude and return the text response."""
        t0 = time.monotonic()
        message = await self._client.messages.create(
            model=self._model,
            max_tokens=self._max_tokens,
            system=system_prompt if system_prompt else [],
            messages=[{"role": "user", "content": prompt}],
        )
        latency = time.monotonic() - t0

        # Record usage. Bypasses LangChain so it has its own hook —
        # otherwise these calls would be invisible in /api/usage.
        usage = getattr(message, "usage", None)
        await get_recorder().record_async(
            provider="anthropic",
            model=self._model,
            input_tokens=getattr(usage, "input_tokens", 0) if usage else 0,
            output_tokens=getattr(usage, "output_tokens", 0) if usage else 0,
            latency_s=latency,
            source="anthropic_sdk",
        )

        # Extract text from content blocks
        return "".join(
            block.text for block in message.content if hasattr(block, "text")
        )
