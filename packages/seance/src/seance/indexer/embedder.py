"""Embedding interface using Google's gemini-embedding-001 model.

Handles batching and rate limiting. Google's free tier allows ~100
embed requests per minute (each text in a batch counts separately).
"""

from __future__ import annotations

import logging
import time

from google import genai
from google.genai import errors as genai_errors

from seance.config import SeanceConfig

logger = logging.getLogger(__name__)

# Google's embedding API accepts up to 100 texts per batch request.
BATCH_SIZE = 100

# Small pause between batches to be a good API citizen.
BATCH_DELAY = 0.1

# Max retries on rate-limit (429) errors.
MAX_RETRIES = 5


class Embedder:
    """Generates vector embeddings for code chunks."""

    def __init__(self, config: SeanceConfig) -> None:
        self._client = genai.Client(api_key=config.google_api_key)
        self._model = config.embedding_model

    def embed_texts(self, texts: list[str]) -> list[list[float]]:
        """Embed a list of texts, handling batching and rate limits.

        Args:
            texts: Source code strings to embed.

        Returns:
            List of embedding vectors, one per input text.
        """
        all_embeddings: list[list[float]] = []

        for i in range(0, len(texts), BATCH_SIZE):
            batch = texts[i : i + BATCH_SIZE]
            embeddings = self._embed_with_retry(batch)
            all_embeddings.extend(embeddings)

            # Pause between batches to stay under rate limit
            remaining = len(texts) - (i + BATCH_SIZE)
            if remaining > 0:
                time.sleep(BATCH_DELAY)

        return all_embeddings

    def embed_query(self, query: str) -> list[float]:
        """Embed a single search query.

        Args:
            query: Natural language search query.

        Returns:
            Embedding vector for the query.
        """
        embeddings = self._embed_with_retry([query])
        return embeddings[0]

    def _embed_with_retry(self, texts: list[str]) -> list[list[float]]:
        """Call the embedding API with retry on rate-limit errors."""
        for attempt in range(MAX_RETRIES):
            try:
                result = self._client.models.embed_content(
                    model=self._model,
                    contents=texts,
                )
                return [e.values for e in result.embeddings]
            except genai_errors.ClientError as e:
                if "429" in str(e) or "RESOURCE_EXHAUSTED" in str(e):
                    wait = min(2 ** attempt * 5, 60)  # 5s, 10s, 20s, 40s, 60s
                    logger.warning(
                        "Rate limited (attempt %d/%d). Waiting %ds...",
                        attempt + 1,
                        MAX_RETRIES,
                        wait,
                    )
                    time.sleep(wait)
                else:
                    raise

        raise RuntimeError(
            f"Embedding failed after {MAX_RETRIES} retries due to rate limiting. "
            "Try again in a minute or upgrade your Google AI API plan."
        )
