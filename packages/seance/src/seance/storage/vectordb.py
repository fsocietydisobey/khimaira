"""Qdrant vector storage for persistent, per-project code search.

Runs in local embedded mode — no Docker, no server process. The Qdrant
engine runs inside the Python process and persists data to disk under
the configured storage directory.
"""

from __future__ import annotations

import uuid
from typing import Any

from qdrant_client import QdrantClient
from qdrant_client.models import (
    Distance,
    FieldCondition,
    Filter,
    MatchValue,
    PointStruct,
    VectorParams,
)

from seance.config import SeanceConfig
from seance.indexer.chunker import CodeChunk

# Google gemini-embedding-001 outputs 3072-dimensional vectors
EMBEDDING_DIM = 3072


class VectorStore:
    """Persistent vector storage backed by Qdrant (local embedded mode)."""

    def __init__(self, config: SeanceConfig) -> None:
        db_path = config.storage_dir / "qdrant"
        db_path.mkdir(parents=True, exist_ok=True)
        self._client = QdrantClient(path=str(db_path))

    def get_or_create_collection(self, project_name: str) -> str:
        """Ensure a Qdrant collection exists for the project. Returns the collection name."""
        safe_name = project_name.replace("-", "_").replace(".", "_")[:63]

        if not self._client.collection_exists(safe_name):
            self._client.create_collection(
                collection_name=safe_name,
                vectors_config=VectorParams(
                    size=EMBEDDING_DIM,
                    distance=Distance.COSINE,
                ),
            )

        return safe_name

    def upsert_chunks(
        self,
        project_name: str,
        chunks: list[CodeChunk],
        embeddings: list[list[float]],
    ) -> int:
        """Insert or update chunks in the vector store.

        Args:
            project_name: Name of the project collection.
            chunks: Code chunks to store.
            embeddings: Corresponding embedding vectors.

        Returns:
            Number of chunks upserted.
        """
        collection = self.get_or_create_collection(project_name)

        points = [
            PointStruct(
                id=str(uuid.uuid5(uuid.NAMESPACE_URL, c.chunk_id)),
                vector=embedding,
                payload={
                    "chunk_id": c.chunk_id,
                    "text": c.text,
                    "file_path": c.file_path,
                    "symbol_name": c.symbol_name,
                    "chunk_type": c.chunk_type.value,
                    "language": c.language.value,
                    "start_line": c.start_line,
                    "end_line": c.end_line,
                },
            )
            for c, embedding in zip(chunks, embeddings)
        ]

        # Qdrant accepts batches of up to 100 points efficiently
        batch_size = 100
        for i in range(0, len(points), batch_size):
            self._client.upsert(
                collection_name=collection,
                points=points[i : i + batch_size],
            )

        return len(chunks)

    def delete_by_file(self, project_name: str, file_path: str) -> None:
        """Remove all chunks for a given file (used before re-indexing a file)."""
        collection = self.get_or_create_collection(project_name)
        self._client.delete(
            collection_name=collection,
            points_selector=Filter(
                must=[FieldCondition(key="file_path", match=MatchValue(value=file_path))]
            ),
        )

    def query(
        self,
        project_name: str,
        query_embedding: list[float],
        top_k: int = 10,
        where: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """Query the vector store for nearest neighbors.

        Args:
            project_name: Name of the project collection.
            query_embedding: Embedding vector for the search query.
            top_k: Number of results to return.
            where: Optional metadata filter dict.

        Returns:
            Dict with ids, documents, metadatas, distances lists.
        """
        collection = self.get_or_create_collection(project_name)

        query_filter = self._build_filter(where) if where else None

        results = self._client.query_points(
            collection_name=collection,
            query=query_embedding,
            limit=top_k,
            query_filter=query_filter,
            with_payload=True,
        )

        ids: list[str] = []
        documents: list[str] = []
        metadatas: list[dict[str, Any]] = []
        distances: list[float] = []

        for point in results.points:
            ids.append(str(point.id))
            documents.append(point.payload.get("text", ""))
            metadatas.append(
                {
                    "file_path": point.payload.get("file_path", ""),
                    "symbol_name": point.payload.get("symbol_name", ""),
                    "chunk_type": point.payload.get("chunk_type", ""),
                    "language": point.payload.get("language", ""),
                    "start_line": point.payload.get("start_line", 0),
                    "end_line": point.payload.get("end_line", 0),
                }
            )
            # Qdrant returns similarity score (higher = better for cosine).
            # Convert to distance (lower = better) for consistent scoring.
            distances.append(1.0 - point.score)

        return {
            "ids": [ids],
            "documents": [documents],
            "metadatas": [metadatas],
            "distances": [distances],
        }

    def list_projects(self) -> list[dict[str, Any]]:
        """List all indexed projects with chunk counts."""
        collections = self._client.get_collections().collections
        return [
            {
                "name": col.name,
                "chunks": self._client.get_collection(col.name).points_count,
            }
            for col in collections
        ]

    def delete_project(self, project_name: str) -> None:
        """Delete an entire project's collection."""
        safe_name = project_name.replace("-", "_").replace(".", "_")[:63]
        if self._client.collection_exists(safe_name):
            self._client.delete_collection(collection_name=safe_name)

    def _build_filter(self, where: dict[str, Any]) -> Filter:
        """Convert a simple filter dict to a Qdrant Filter object."""
        if "$and" in where:
            conditions = [
                FieldCondition(key=k, match=MatchValue(value=v))
                for clause in where["$and"]
                for k, v in clause.items()
            ]
            return Filter(must=conditions)

        conditions = [
            FieldCondition(key=k, match=MatchValue(value=v))
            for k, v in where.items()
        ]
        return Filter(must=conditions)
