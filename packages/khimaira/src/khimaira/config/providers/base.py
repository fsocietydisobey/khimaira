"""Base provider interface — all AI providers implement this."""

from abc import ABC, abstractmethod


class Provider(ABC):
    """Abstract base for AI model providers."""

    @abstractmethod
    async def generate(self, prompt: str, system_prompt: str = "") -> str:
        """Send a prompt to the model and return the text response.

        Args:
            prompt: The user message / task description.
            system_prompt: Optional system instructions for the model.

        Returns:
            The model's text response.
        """
        ...

    @property
    @abstractmethod
    def name(self) -> str:
        """Human-readable provider name (e.g. 'Claude Opus', 'Gemini Pro')."""
        ...
