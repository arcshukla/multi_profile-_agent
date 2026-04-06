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

import logging

import chromadb
from chromadb.config import Settings as ChromaSettings
from chromadb.utils.embedding_functions import ONNXMiniLM_L6_V2 as _ONNXEmbedFn

from app.core.logging_config import get_logger
from app.utils.file_utils import read_document
from app.rag.llm_client import LLMClient

_module_logger = get_logger(__name__)

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
        logger:          Optional[logging.Logger] = None,
    ) -> None:
        if not topic_labels:
            raise ValueError("topic_labels must not be empty")

        self.topic_labels  = topic_labels
        self.split_prompt  = split_prompt
        self.intent_prompt = intent_prompt
        self.on_tokens     = on_tokens
        self.llm           = LLMClient()
        self._log          = logger or _module_logger

        self._client = chromadb.PersistentClient(
            path=db_path,
            settings=ChromaSettings(anonymized_telemetry=False),
        )
        self.collection = self._get_or_create_collection(collection_name)
        self._log.info(
            "SemanticRAGEngine ready | path='%s' | collection='%s' | %d chunks | topics=%s",
            db_path, collection_name, self.collection.count(), topic_labels,
        )

    def _get_or_create_collection(self, collection_name: str):
        """
        Get or create the ChromaDB collection.

        If the persisted collection was created with a different embedding function
        (e.g. upgraded ChromaDB defaulted to 'default' on an older collection),
        the collection is wiped and recreated so re-ingestion starts clean.
        """
        try:
            return self._client.get_or_create_collection(
                name=collection_name,
                metadata={"hnsw:space": "cosine"},
                embedding_function=_EMBEDDING_FN,
            )
        except ValueError as exc:
            msg = str(exc).lower()
            if "embedding function" in msg and ("conflict" in msg or "already exists" in msg):
                self._log.warning(
                    "Embedding function conflict on collection '%s' — wiping and recreating. "
                    "Re-indexing required. Detail: %s",
                    collection_name, exc,
                )
                self._client.delete_collection(collection_name)
                return self._client.get_or_create_collection(
                    name=collection_name,
                    metadata={"hnsw:space": "cosine"},
                    embedding_function=_EMBEDDING_FN,
                )
            raise

    def close(self) -> None:
        """Release the ChromaDB connection. Call before wiping the DB directory."""
        try:
            if getattr(self._client._system, "_running", False):
                self._client._system.stop()
        except Exception as e:
            self._log.debug("ChromaDB client stop failed (harmless): %s", e)
        try:
            self._client.clear_system_cache()
        except Exception as e:
            self._log.debug("ChromaDB cache clear failed (harmless): %s", e)

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
            self._log.warning("Could not read %s: %s", path, e, exc_info=True)
            return 0

        self._log.info("Ingesting: %s | %d chars extracted", path.name, len(raw_text))
        if not raw_text.strip():
            self._log.warning("Empty text extracted from %s — skipping", path.name)
            return 0
        sections = self._split_into_sections(raw_text, source_name=str(path))
        if not sections:
            self._log.warning("No sections extracted from %s", path)
            return 0

        self._log.info("Parsed %d sections from %s", len(sections), path.name)
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
                self.collection.upsert(
                    ids=[chunk_id],
                    documents=[text],
                    metadatas=[{"topic": topic, "source": path.name}],
                )
                added += 1
            except Exception as e:
                self._log.warning("collection.upsert failed | id=%s | source=%s | %s: %s",
                                  chunk_id, path.name, type(e).__name__, e)
                skipped += 1

        self._log.info(
            "Ingested %s → %d chunks written, %d skipped (total: %d)",
            path.name, added, skipped, self.collection.count()
        )
        return added

    def ingest_all(self, docs_dir: str | Path) -> int:
        """Ingest all supported documents in a directory. Returns total new chunks."""
        from app.core.constants import ALLOWED_DOC_EXTENSIONS
        docs_path = Path(docs_dir)
        if not docs_path.exists():
            self._log.warning("docs_dir does not exist: %s", docs_path)
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
        self._log.info("Cleared collection '%s' (%d docs removed)", self.collection.name, len(ids))

    # ── Retrieval ─────────────────────────────────────────────────────────────

    def retrieve(self, query: str, k: int = 4) -> list[str]:
        """
        Classify query intent → fetch matching topic sections.
        Falls back to a broader fetch if classification returns no results.
        """
        if self.collection.count() == 0:
            self._log.warning("retrieve | collection is empty — no results | query=%r", query[:120])
            return []

        # Step 1: classify the query into topic labels
        topics = self._classify_intent(query)
        self._log.info("retrieve | query=%r | classified_topics=%s", query[:120], topics)

        # Step 2: fetch by topic metadata filter
        chunks = []
        used_fallback = False
        if topics:
            for topic in topics:
                try:
                    result = self.collection.get(
                        where={"topic": topic},
                        include=["documents"],
                    )
                    fetched = result["documents"]
                    self._log.info("retrieve | topic='%s' → %d chunk(s) fetched", topic, len(fetched))
                    chunks.extend(fetched)
                except Exception as e:
                    self._log.warning("ChromaDB fetch failed | topic='%s' | query=%r | error=%s", topic, query[:80], e)

        # Step 3: fallback — grab top-k by position if topic fetch is empty
        if not chunks:
            used_fallback = True
            self._log.warning(
                "retrieve | topic fetch returned 0 chunks | query=%r | topics=%s — falling back to first %d docs",
                query[:120], topics, k,
            )
            result = self.collection.get(include=["documents"])
            chunks = result["documents"][:k]

        # Deduplicate while preserving order
        seen: set[str] = set()
        deduped = []
        for c in chunks:
            if c not in seen:
                seen.add(c)
                deduped.append(c)

        final = deduped[:k]
        self._log.info(
            "retrieve | final=%d chunk(s) returned (from %d pre-dedup) | fallback=%s | snippet='%s...'",
            len(final), len(chunks), used_fallback,
            final[0][:80].replace("\n", " ") if final else "",
        )
        return final

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
            self._log.warning("chunk_count failed (stale/corrupt ChromaDB?): %s", e)
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
            self._log.info(
                "Indexing LLM call | source=%s | prompt_tokens=%d | completion_tokens=%d | total=%d",
                source_name, prompt_tok, completion_tok, total_tok,
            )
            if self.on_tokens:
                self.on_tokens("indexing", prompt_tok, completion_tok, total_tok)
            sections = self._parse_llm_json(response.choices[0].message.content, fallback=[])
            if isinstance(sections, list):
                return sections
            self._log.warning("LLM split for %s returned non-list: %r", source_name, sections)
        except json.JSONDecodeError as e:
            self._log.warning("LLM split JSON parse failed for %s: %s", source_name, e)
        except Exception as e:
            try:
                from openai import APIStatusError
                if isinstance(e, APIStatusError) and e.status_code in (401, 402, 403, 429):
                    self._log.error(
                        "Fatal API error during LLM split for %s (HTTP %d) — aborting indexing: %s",
                        source_name, e.status_code, e,
                    )
                    raise
            except ImportError:
                pass
            self._log.warning("LLM split failed for %s: %s", source_name, e, exc_info=True)
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
            raw_content = response.choices[0].message.content
            parsed = self._parse_llm_json(raw_content, fallback=[])
            if isinstance(parsed, list):
                valid = [t for t in parsed if t in self.topic_labels]
                invalid = [t for t in parsed if t not in self.topic_labels]
                if not valid:
                    self._log.warning(
                        "Intent classification returned no valid topics | query=%r | raw=%r | invalid=%s",
                        query[:80], raw_content[:120], invalid,
                    )
                elif invalid:
                    self._log.warning(
                        "Intent classification | valid=%s | unrecognised labels dropped=%s",
                        valid, invalid,
                    )
                return valid
            self._log.warning("Intent classification returned non-list | query=%r | raw=%r", query[:80], parsed)
        except json.JSONDecodeError as e:
            self._log.warning("Intent classification JSON parse failed | query=%r | error=%s", query[:80], e)
        except Exception as e:
            self._log.warning("Intent classification failed | query=%r | error=%s", query[:80], e, exc_info=True)
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
