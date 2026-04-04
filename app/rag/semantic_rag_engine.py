"""
semantic_rag_engine.py
----------------------
Generic LLM-powered semantic RAG engine — ported from the existing single-profile app.

Core pattern:
  INGEST:   LLM splits each file into named topic sections using a prompt
            and topic list you provide. Each section stored in ChromaDB with
            a `topic` metadata label.

  RETRIEVE: A fast LLM call classifies the user's query using an intent
            prompt you provide, then does a direct metadata filter fetch.
            No ANN, no reranker — pure label lookup.

This class knows nothing about profiles, products, or any other domain.
All domain knowledge (topic labels, prompts) is supplied by the caller.

on_tokens callback (optional):
  Signature: on_tokens(operation: str, prompt: int, completion: int, total: int)
  Called after every LLM call so the caller can record usage without
  coupling the engine to any persistence layer.
  operation values: "indexing" | "intent"
"""

import hashlib
import json
import re
from pathlib import Path
from typing import Callable, Optional

import chromadb
from chromadb.config import Settings as ChromaSettings
from chromadb.utils.embedding_functions import ONNXMiniLM_L6_V2 as _ONNXEmbedFn

from app.core.logging_config import get_logger
from app.utils.file_utils import read_document
from app.rag.llm_client import LLMClient

logger = get_logger(__name__)

# Module-level singleton — loaded once on import, shared across all SemanticRAGEngine instances.
# Avoids repeated ONNX model loads and suppresses the "No ONNX providers" warning.
_EMBEDDING_FN = _ONNXEmbedFn(preferred_providers=["CPUExecutionProvider"])


class SemanticRAGEngine:
    """
    Generic LLM-powered semantic RAG engine.

    Instantiate one per profile with its own db_path and collection_name.
    topic_labels, split_prompt, and intent_prompt are the domain config.
    """

    def __init__(
        self,
        topic_labels:    list[str],
        split_prompt:    str,
        intent_prompt:   str,
        db_path:         str = ".chromadb_semantic",
        collection_name: str = "semantic_docs",
        on_tokens:       Optional[Callable[[str, int, int, int], None]] = None,
    ) -> None:
        if not topic_labels:
            raise ValueError("topic_labels must not be empty")

        self.topic_labels  = topic_labels
        self.split_prompt  = split_prompt
        self.intent_prompt = intent_prompt
        self.on_tokens     = on_tokens
        self.llm           = LLMClient()

        self._client = chromadb.PersistentClient(
            path=db_path,
            settings=ChromaSettings(anonymized_telemetry=False),
        )
        self.collection = self._client.get_or_create_collection(
            name=collection_name,
            metadata={"hnsw:space": "cosine"},
            embedding_function=_EMBEDDING_FN,
        )
        logger.info(
            "SemanticRAGEngine ready | path='%s' | collection='%s' | %d chunks | topics=%s",
            db_path, collection_name, self.collection.count(), topic_labels,
        )

    def close(self) -> None:
        """Release the ChromaDB connection. Call before wiping the DB directory."""
        try:
            self._client._system.stop()
        except Exception as e:
            logger.warning("ChromaDB client close failed: %s", e)

    # ── Ingestion ─────────────────────────────────────────────────────────────

    def ingest(self, source: str | Path) -> int:
        """
        Read a file, split into labelled topic sections via LLM, store in ChromaDB.
        Idempotent — skips chunks already indexed (same content hash).
        Returns number of new chunks added.
        """
        path = Path(source)
        if not path.is_file():
            raise FileNotFoundError(f"File not found: {path}")

        try:
            raw_text = read_document(path)
        except Exception as e:
            logger.warning("Could not read %s: %s", path, e, exc_info=True)
            return 0

        logger.info("Ingesting: %s | %d chars extracted", path.name, len(raw_text))
        if not raw_text.strip():
            logger.warning("Empty text extracted from %s — skipping", path.name)
            return 0
        sections = self._split_into_sections(raw_text, source_name=str(path))
        if not sections:
            logger.warning("No sections extracted from %s", path)
            return 0

        logger.info("Parsed %d sections from %s", len(sections), path.name)
        added = 0
        skipped = 0
        for section in sections:
            topic = section.get("topic", "other")
            text  = section.get("text", "").strip()
            if not text:
                skipped += 1
                continue
            chunk_id = self._chunk_id(text)
            try:
                self.collection.add(
                    ids=[chunk_id],
                    documents=[text],
                    metadatas=[{"topic": topic, "source": path.name}],
                )
                added += 1
            except Exception as e:
                err_name = type(e).__name__
                if "unique" in str(e).lower() or "exists" in str(e).lower():
                    logger.debug("Chunk already indexed (duplicate id=%s)", chunk_id)
                else:
                    logger.warning("collection.add failed | id=%s | %s: %s", chunk_id, err_name, e)
                skipped += 1

        logger.info(
            "Ingested %s → %d new chunks, %d skipped (total: %d)",
            path.name, added, skipped, self.collection.count()
        )
        return added

    def ingest_all(self, docs_dir: str | Path) -> int:
        """Ingest all supported documents in a directory. Returns total new chunks."""
        from app.core.constants import ALLOWED_DOC_EXTENSIONS
        docs_path = Path(docs_dir)
        if not docs_path.exists():
            logger.warning("docs_dir does not exist: %s", docs_path)
            return 0
        total = 0
        for f in sorted(docs_path.iterdir()):
            if f.is_file() and f.suffix.lower() in ALLOWED_DOC_EXTENSIONS:
                total += self.ingest(f)
        return total

    def clear(self) -> None:
        """Delete all documents from the collection (wipe index)."""
        ids = self.collection.get()["ids"]
        if ids:
            self.collection.delete(ids=ids)
        logger.info("Cleared collection '%s' (%d docs removed)", self.collection.name, len(ids))

    # ── Retrieval ─────────────────────────────────────────────────────────────

    def retrieve(self, query: str, k: int = 4) -> list[str]:
        """
        Classify query intent → fetch matching topic sections.
        Falls back to a broader fetch if classification returns no results.
        """
        if self.collection.count() == 0:
            return []

        # Step 1: classify the query into topic labels
        topics = self._classify_intent(query)
        logger.debug("Query intent: topics=%s", topics)

        # Step 2: fetch by topic metadata filter
        chunks = []
        if topics:
            for topic in topics:
                try:
                    result = self.collection.get(
                        where={"topic": topic},
                        include=["documents"],
                    )
                    chunks.extend(result["documents"])
                except Exception as e:
                    logger.warning("ChromaDB fetch failed for topic '%s': %s", topic, e)

        # Step 3: fallback — grab top-k by position if topic fetch is empty
        if not chunks:
            logger.debug("Topic fetch empty — falling back to first %d docs", k)
            result = self.collection.get(include=["documents"])
            chunks = result["documents"][:k]

        # Deduplicate while preserving order
        seen: set[str] = set()
        deduped = []
        for c in chunks:
            if c not in seen:
                seen.add(c)
                deduped.append(c)

        logger.debug("Retrieved %d chunk(s)", len(deduped[:k]))
        return deduped[:k]

    def build_snapshot(self) -> str:
        """
        Return a text snapshot of ALL indexed content.
        Used to generate initial followup questions.
        """
        result = self.collection.get(include=["documents", "metadatas"])
        if not result["documents"]:
            return ""
        parts = []
        for doc, meta in zip(result["documents"], result["metadatas"]):
            topic = (meta or {}).get("topic", "")
            parts.append(f"[{topic.upper()}]\n{doc}")
        return "\n\n".join(parts)

    def get_all_topics(self) -> list[str]:
        """Return distinct topic labels currently in the collection."""
        result = self.collection.get(include=["metadatas"])
        topics = {(m or {}).get("topic", "") for m in result["metadatas"]}
        return sorted(t for t in topics if t)

    def chunk_count(self) -> int:
        try:
            return self.collection.count()
        except Exception as e:
            logger.warning("chunk_count failed (stale/corrupt ChromaDB?): %s", e)
            return 0

    # ── Private helpers ───────────────────────────────────────────────────────

    def _split_into_sections(self, text: str, source_name: str) -> list[dict]:
        """
        Call LLM to split raw document text into [{topic, text}] sections.
        Fires on_tokens("indexing", ...) after the call.
        """
        prompt = self.split_prompt.format(
            topic_labels = ", ".join(self.topic_labels),
            source_name  = source_name,
            text         = text[:12000],  # stay within context window
        )
        try:
            response = self.llm.chat(
                [{"role": "user", "content": prompt}],
                max_tokens  = 4000,
                temperature = 0.1,
            )
            usage = getattr(response, "usage", None)
            prompt_tok     = getattr(usage, "prompt_tokens",     0) or 0
            completion_tok = getattr(usage, "completion_tokens", 0) or 0
            total_tok      = getattr(usage, "total_tokens",      0) or 0
            logger.info(
                "Indexing LLM call | source=%s | prompt_tokens=%d | completion_tokens=%d | total=%d",
                source_name, prompt_tok, completion_tok, total_tok,
            )
            if self.on_tokens:
                self.on_tokens("indexing", prompt_tok, completion_tok, total_tok)
            sections = self._parse_llm_json(response.choices[0].message.content, fallback=[])
            if isinstance(sections, list):
                return sections
            logger.warning("LLM split for %s returned non-list: %r", source_name, sections)
        except json.JSONDecodeError as e:
            logger.warning("LLM split JSON parse failed for %s: %s", source_name, e)
        except Exception as e:
            logger.warning("LLM split failed for %s: %s", source_name, e, exc_info=True)
        return []

    def _classify_intent(self, query: str) -> list[str]:
        """
        Call LLM to classify query into 1-3 topic labels.
        Fast, low-token call — failures gracefully fall back to full index scan.
        Fires on_tokens("intent", ...) after the call.
        """
        prompt = self.intent_prompt.format(
            topic_labels = ", ".join(self.topic_labels),
            query        = query,
        )
        try:
            response = self.llm.chat(
                [{"role": "user", "content": prompt}],
                max_tokens  = 100,
                temperature = 0.0,
            )
            usage = getattr(response, "usage", None)
            if self.on_tokens and usage:
                self.on_tokens(
                    "intent",
                    getattr(usage, "prompt_tokens",     0) or 0,
                    getattr(usage, "completion_tokens", 0) or 0,
                    getattr(usage, "total_tokens",      0) or 0,
                )
            parsed = self._parse_llm_json(response.choices[0].message.content, fallback=[])
            if isinstance(parsed, list):
                valid = [t for t in parsed if t in self.topic_labels]
                if not valid:
                    logger.debug("Intent classification returned no valid topics for query=%r", query[:80])
                return valid
            logger.warning("Intent classification returned non-list: %r", parsed)
        except json.JSONDecodeError as e:
            logger.warning("Intent classification JSON parse failed: %s", e)
        except Exception as e:
            logger.warning("Intent classification failed: %s", e, exc_info=True)
        return []

    @staticmethod
    def _parse_llm_json(content: Optional[str], fallback=None):
        """
        Strip markdown code fences and parse JSON from an LLM response.
        Raises json.JSONDecodeError on invalid JSON (caller decides how to handle).
        """
        if not content:
            return fallback
        content = re.sub(r"^```(?:json)?\s*", "", content.strip())
        content = re.sub(r"\s*```$",           "", content).strip()
        return json.loads(content)

    @staticmethod
    def _chunk_id(text: str) -> str:
        return hashlib.sha256(text.encode()).hexdigest()[:32]
