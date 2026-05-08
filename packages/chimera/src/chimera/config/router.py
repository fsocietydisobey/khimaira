"""Router — resolves a role to a provider instance and dispatches requests."""

import json
import os

from .loader import OrchestratorConfig
from chimera.prompts import CLASSIFIER_SYSTEM_PROMPT
from .providers.anthropic_provider import AnthropicProvider
from .providers.base import Provider
from .providers.google_provider import GoogleProvider


# Maps provider config key → provider class
PROVIDER_CLASSES: dict[str, type[Provider]] = {
    "anthropic": AnthropicProvider,
    "google": GoogleProvider,
}


class Router:
    """Resolves roles to providers and dispatches generation requests."""

    def __init__(self, config: OrchestratorConfig):
        self._config = config
        self._providers: dict[str, Provider] = {}  # cache by "provider:model"

    def _get_provider(self, provider_key: str, model: str) -> Provider:
        """Get or create a provider instance for the given key and model."""
        cache_key = f"{provider_key}:{model}"
        if cache_key not in self._providers:
            provider_config = self._config.providers[provider_key]
            api_key = os.environ.get(provider_config.api_key_env)

            cls = PROVIDER_CLASSES.get(provider_key)
            if cls is None:
                raise ValueError(f"Unknown provider: {provider_key}")

            self._providers[cache_key] = cls(model=model, api_key=api_key)

        return self._providers[cache_key]

    def get_role_provider(self, role: str) -> Provider:
        """Get the provider configured for a given role."""
        role_config = self._config.roles.get(role)
        if role_config is None:
            raise ValueError(
                f"Unknown role: {role}. "
                f"Available: {list(self._config.roles.keys())}"
            )
        return self._get_provider(role_config.provider, role_config.model)

    async def classify(self, task_description: str) -> dict:
        """Classify a task into a tier using the fast/cheap classifier model.

        Returns a dict with: tier, confidence, reasoning, pipeline.
        """
        provider = self.get_role_provider("classify")
        raw = await provider.generate(
            task_description, system_prompt=CLASSIFIER_SYSTEM_PROMPT
        )

        # Parse JSON from the response (strip markdown fences if present)
        cleaned = raw.strip()
        if cleaned.startswith("```"):
            cleaned = cleaned.split("\n", 1)[1].rsplit("```", 1)[0].strip()

        try:
            return json.loads(cleaned)
        except json.JSONDecodeError:
            return {
                "tier": "architect",
                "confidence": 0.5,
                "reasoning": "Failed to parse classifier response, defaulting to architect.",
                "pipeline": ["architect", "implement"],
                "raw_response": raw,
            }
