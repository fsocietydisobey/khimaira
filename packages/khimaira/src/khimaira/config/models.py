"""LangChain model factories — create Chat* wrappers from OrchestratorConfig.

Each factory reads the role config (provider, model, max_tokens) and returns
the appropriate LangChain chat model. This keeps model construction in one
place and lets nodes stay provider-agnostic.
"""

import os

from langchain_core.language_models.chat_models import BaseChatModel

from .loader import OrchestratorConfig


def _get_api_key(config: OrchestratorConfig, provider_key: str) -> str | None:
    """Resolve the API key for a provider from environment variables."""
    provider_config = config.providers.get(provider_key)
    if provider_config is None:
        return None
    return os.environ.get(provider_config.api_key_env)


def _build_model(config: OrchestratorConfig, role: str) -> BaseChatModel:
    """Build a LangChain chat model for a given role.

    Reads the role's provider and model from config, resolves the API key,
    and returns the appropriate LangChain wrapper.

    Raises:
        ValueError: If the role or provider is not found in config.
    """
    role_config = config.roles.get(role)
    if role_config is None:
        raise ValueError(
            f"Unknown role: {role}. Available: {list(config.roles.keys())}"
        )

    provider_key = role_config.provider
    api_key = _get_api_key(config, provider_key)

    # Every model gets a usage-tracking callback so credit burn shows
    # up in /api/usage and the rate-anomaly self-watch can fire. Lazy
    # import so khimaira.usage doesn't pull langchain_core into modules
    # that don't need it.
    from khimaira.usage import make_langchain_callback

    if provider_key == "anthropic":
        from langchain_anthropic import ChatAnthropic

        # Only pass api_key if resolved — otherwise let ChatAnthropic
        # pick it up from ANTHROPIC_API_KEY env var automatically
        kwargs = {
            "model": role_config.model,
            "max_tokens": role_config.max_tokens,
            "callbacks": [make_langchain_callback("anthropic", role=role)],
        }
        if api_key:
            kwargs["api_key"] = api_key
        return ChatAnthropic(**kwargs)

    elif provider_key == "google":
        from langchain_google_genai import ChatGoogleGenerativeAI

        # Only pass google_api_key if resolved — otherwise let the SDK
        # pick it up from GOOGLE_API_KEY env var automatically
        kwargs = {
            "model": role_config.model,
            "max_output_tokens": role_config.max_tokens,
            "callbacks": [make_langchain_callback("google", role=role)],
        }
        if api_key:
            kwargs["google_api_key"] = api_key
        return ChatGoogleGenerativeAI(**kwargs)
    else:
        raise ValueError(
            f"Unsupported provider '{provider_key}' for role '{role}'. "
            f"Supported: anthropic, google"
        )


def get_classify_model(config: OrchestratorConfig) -> BaseChatModel:
    """Get the LangChain chat model configured for classification (fast/cheap)."""
    return _build_model(config, "classify")
