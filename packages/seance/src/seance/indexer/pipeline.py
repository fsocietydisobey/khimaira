"""Indexing pipeline: discovers files → chunks with tree-sitter → embeds → stores in ChromaDB.

Handles both full indexing and incremental git-diff-aware reindexing.
"""

from __future__ import annotations

import logging
import subprocess
from pathlib import Path

from seance.config import SeanceConfig
from seance.indexer.chunker import CodeChunk, chunk_file, detect_language
from seance.indexer.embedder import Embedder
from seance.storage.vectordb import VectorStore


def _build_embedding_text(chunk: CodeChunk, project_root: Path) -> str:
    """Prepend file/symbol context to chunk text before embedding.

    Folder structure carries semantic signal (especially in feature-organized
    frontend codebases). By embedding `features/auth/components/LoginForm.tsx`
    along with the code, the model can match queries like "login form in the
    auth feature" more accurately.
    """
    try:
        rel_path = Path(chunk.file_path).relative_to(project_root)
    except ValueError:
        rel_path = Path(chunk.file_path)

    header = (
        f"// File: {rel_path}\n"
        f"// Symbol: {chunk.symbol_name} ({chunk.chunk_type.value})\n"
        f"// Language: {chunk.language.value}\n"
    )
    return header + chunk.text

logger = logging.getLogger(__name__)

# Directories to always skip, regardless of .gitignore
ALWAYS_SKIP = {
    # Version control
    ".git",
    # Package managers / dependencies
    "node_modules",
    "site-packages",
    ".venv",
    "venv",
    # Build artifacts
    "dist",
    "build",
    ".next",
    ".nuxt",
    "coverage",
    # Caches
    "__pycache__",
    ".mypy_cache",
    ".pytest_cache",
    ".ruff_cache",
    ".cache",
    # AI tool configs (not source code)
    ".claude",
    ".cursor",
    ".serena",
    # Generated / migration files
    ".supabase",
    "migrations",
    # IDE
    ".idea",
    ".vscode",
}


class IndexingPipeline:
    """Orchestrates the full indexing flow: discover → chunk → embed → store."""

    def __init__(self, config: SeanceConfig) -> None:
        self._config = config
        self._embedder = Embedder(config)
        self._store = VectorStore(config)

    def index_project(self, project_path: Path, project_name: str) -> dict[str, int]:
        """Full index of a project directory.

        Args:
            project_path: Root directory of the project.
            project_name: Name to use for the ChromaDB collection.

        Returns:
            Stats dict with file_count, chunk_count, etc.
        """
        logger.info("Starting full index of %s at %s", project_name, project_path)

        files = self._discover_files(project_path)
        logger.info("Discovered %d indexable files", len(files))

        all_chunks: list[CodeChunk] = []
        for file_path in files:
            try:
                chunks = chunk_file(file_path)
                all_chunks.extend(chunks)
            except Exception as e:
                logger.warning("Failed to chunk %s: %s", file_path, e)
                continue

        # Filter out empty chunks (blank files, empty modules)
        all_chunks = [c for c in all_chunks if c.text.strip()]
        logger.info("Extracted %d chunks from %d files", len(all_chunks), len(files))

        if not all_chunks:
            logger.warning("No chunks extracted — nothing to index")
            return {"file_count": len(files), "chunk_count": 0, "embedded_count": 0}

        # Embed in batches, prepending file/symbol context for stronger retrieval signal
        texts = [_build_embedding_text(c, project_path) for c in all_chunks]
        embeddings = self._embedder.embed_texts(texts)

        # Store
        upserted = self._store.upsert_chunks(project_name, all_chunks, embeddings)
        logger.info("Upserted %d chunks into collection '%s'", upserted, project_name)

        return {
            "file_count": len(files),
            "chunk_count": len(all_chunks),
            "embedded_count": upserted,
        }

    def reindex_changed(self, project_path: Path, project_name: str) -> dict[str, int]:
        """Incremental reindex using git diff to find changed files.

        Only re-embeds files that have changed since the last commit.

        Args:
            project_path: Root directory of the project (must be a git repo).
            project_name: Name of the ChromaDB collection.

        Returns:
            Stats dict with changed_files, chunk_count, etc.
        """
        changed_files = self._get_git_changed_files(project_path)

        if not changed_files:
            logger.info("No changed files detected — index is up to date")
            return {"changed_files": 0, "chunk_count": 0, "embedded_count": 0}

        logger.info("Reindexing %d changed files", len(changed_files))

        all_chunks: list[CodeChunk] = []
        for rel_path in changed_files:
            abs_path = project_path / rel_path
            # Delete old chunks for this file
            self._store.delete_by_file(project_name, str(abs_path))

            if not abs_path.exists():
                # File was deleted — old chunks already removed
                continue

            if detect_language(abs_path) is None:
                continue

            try:
                chunks = chunk_file(abs_path)
                all_chunks.extend(chunks)
            except Exception as e:
                logger.warning("Failed to chunk %s: %s", abs_path, e)
                continue

        # Filter empty chunks (blank files, empty modules)
        all_chunks = [c for c in all_chunks if c.text.strip()]

        if not all_chunks:
            return {"changed_files": len(changed_files), "chunk_count": 0, "embedded_count": 0}

        texts = [_build_embedding_text(c, project_path) for c in all_chunks]
        embeddings = self._embedder.embed_texts(texts)
        upserted = self._store.upsert_chunks(project_name, all_chunks, embeddings)

        return {
            "changed_files": len(changed_files),
            "chunk_count": len(all_chunks),
            "embedded_count": upserted,
        }

    def _discover_files(self, root: Path) -> list[Path]:
        """Walk project directory and find all indexable source files."""
        files: list[Path] = []

        gitignore_patterns = self._load_gitignore(root)

        for item in root.rglob("*"):
            if item.is_dir():
                continue

            # Skip always-ignored directories
            if any(part in ALWAYS_SKIP for part in item.parts):
                continue

            # Skip files not in a supported language
            if detect_language(item) is None:
                continue

            # Skip gitignored files (basic check — not full gitignore spec)
            rel = item.relative_to(root)
            if self._is_gitignored(rel, gitignore_patterns):
                continue

            files.append(item)

        return sorted(files)

    def _load_gitignore(self, root: Path) -> list[str]:
        """Load .gitignore patterns from the project root."""
        gitignore = root / ".gitignore"
        if not gitignore.exists():
            return []

        patterns: list[str] = []
        for line in gitignore.read_text().splitlines():
            line = line.strip()
            if line and not line.startswith("#"):
                patterns.append(line)
        return patterns

    def _is_gitignored(self, rel_path: Path, patterns: list[str]) -> bool:
        """Basic gitignore check. Not a full implementation — covers common patterns."""
        path_str = str(rel_path)
        for pattern in patterns:
            clean = pattern.rstrip("/")
            if clean in path_str or any(part == clean for part in rel_path.parts):
                return True
        return False

    def _get_git_changed_files(self, project_path: Path) -> list[str]:
        """Get list of files changed since last commit using git."""
        try:
            # Files changed in working tree + staged
            result = subprocess.run(
                ["git", "diff", "--name-only", "HEAD"],
                cwd=project_path,
                capture_output=True,
                text=True,
                check=True,
            )
            changed = set(result.stdout.strip().splitlines())

            # Also include untracked files
            result = subprocess.run(
                ["git", "ls-files", "--others", "--exclude-standard"],
                cwd=project_path,
                capture_output=True,
                text=True,
                check=True,
            )
            changed.update(result.stdout.strip().splitlines())

            return [f for f in changed if f]
        except subprocess.CalledProcessError as e:
            logger.warning("git diff failed: %s", e)
            return []
