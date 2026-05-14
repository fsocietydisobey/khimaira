"""Search engine: embeds a natural language query and retrieves nearest code chunks."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from seance.config import SeanceConfig
from seance.indexer.embedder import Embedder
from seance.storage.vectordb import VectorStore


@dataclass(frozen=True)
class SearchResult:
    """A single search result with relevance score and code context."""

    text: str
    file_path: str
    symbol_name: str
    chunk_type: str
    language: str
    start_line: int
    end_line: int
    score: float  # 0.0 = perfect match, higher = less similar (cosine distance)

    def to_dict(self) -> dict[str, Any]:
        """Serialize for MCP tool response."""
        return {
            "file_path": self.file_path,
            "symbol_name": self.symbol_name,
            "chunk_type": self.chunk_type,
            "language": self.language,
            "start_line": self.start_line,
            "end_line": self.end_line,
            "score": round(self.score, 4),
            "text": self.text,
        }


class SearchEngine:
    """Semantic search over indexed codebases."""

    def __init__(self, config: SeanceConfig) -> None:
        self._embedder = Embedder(config)
        self._store = VectorStore(config)

    def search(
        self,
        project_name: str,
        query: str,
        top_k: int = 10,
        language: str | None = None,
        chunk_type: str | None = None,
    ) -> list[SearchResult]:
        """Search for code chunks semantically similar to a natural language query.

        Args:
            project_name: Name of the indexed project.
            query: Natural language search query.
            top_k: Maximum number of results.
            language: Optional filter by language (python, typescript, javascript).
            chunk_type: Optional filter by chunk type (function, class, etc.).

        Returns:
            List of SearchResult objects, sorted by relevance.
        """
        query_embedding = self._embedder.embed_query(query)

        where: dict[str, Any] | None = None
        if language or chunk_type:
            conditions = []
            if language:
                conditions.append({"language": language})
            if chunk_type:
                conditions.append({"chunk_type": chunk_type})

            if len(conditions) == 1:
                where = conditions[0]
            else:
                where = {"$and": conditions}

        raw = self._store.query(
            project_name=project_name,
            query_embedding=query_embedding,
            top_k=top_k,
            where=where,
        )

        results: list[SearchResult] = []
        if not raw["ids"] or not raw["ids"][0]:
            return results

        for i, doc_id in enumerate(raw["ids"][0]):
            meta = raw["metadatas"][0][i]
            results.append(
                SearchResult(
                    text=raw["documents"][0][i],
                    file_path=meta["file_path"],
                    symbol_name=meta["symbol_name"],
                    chunk_type=meta["chunk_type"],
                    language=meta["language"],
                    start_line=meta["start_line"],
                    end_line=meta["end_line"],
                    score=raw["distances"][0][i],
                )
            )

        return results

    def find_similar(
        self,
        project_name: str,
        file_path: str,
        symbol_name: str,
        top_k: int = 5,
    ) -> list[SearchResult]:
        """Find code similar to a specific symbol in the index.

        Looks up the symbol's chunk, uses its embedding as the query vector,
        and returns the most similar chunks (excluding the original).

        Args:
            project_name: Name of the indexed project.
            file_path: Path to the file containing the symbol.
            symbol_name: Name of the symbol to find similar code for.
            top_k: Maximum number of results.

        Returns:
            List of SearchResult objects, sorted by similarity.
        """
        # Use the symbol's source text as the search query
        query = f"{symbol_name} in {file_path}"
        return self.search(project_name, query, top_k=top_k + 1)[1:]  # Skip self-match
