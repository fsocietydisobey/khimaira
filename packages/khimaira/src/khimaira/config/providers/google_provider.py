"""Google (Gemini) provider using the google-genai SDK."""

import os

from google import genai

from .base import Provider


class GoogleProvider(Provider):
    """Routes requests to Gemini via the Google AI API."""

    def __init__(
        self,
        model: str = "gemini-2.0-flash",
        api_key: str | None = None,
    ):
        self._model_name = model
        key = api_key or os.environ.get("GOOGLE_AI_API_KEY")
        self._client = genai.Client(api_key=key)

    @property
    def name(self) -> str:
        return f"Gemini ({self._model_name})"

    async def generate(self, prompt: str, system_prompt: str = "") -> str:
        """Send a message to Gemini and return the text response."""
        config = {}
        if system_prompt:
            config["system_instruction"] = system_prompt

        response = await self._client.aio.models.generate_content(
            model=self._model_name,
            contents=prompt,
            config=config if config else None,
        )
        return response.text
