"""
llm_client.py
-------------
OpenAI-compatible LLM client. Supports any provider with an OpenAI-style API:
  - OpenRouter (default)
  - Groq
  - OpenAI directly
  - Anthropic via proxy

Provider is selected via OPENROUTER_BASE_URL + OPENROUTER_API_KEY in .env.

Groq compatibility:
  Groq rejects response_format + tools together. The client detects Groq
  and falls back to injecting a system message instead.
"""

import os
import time
from openai import OpenAI

from app.core.config import settings
from app.core.logging_config import get_logger, get_current_session_id, set_current_session_id

logger = get_logger(__name__)

_GROQ_BASE_URL = "api.groq.com"

# Module-level singleton — one httpx connection pool for the whole process.
# LLMClient() returns this shared instance; no new pool is created per call.
_instance: "LLMClient | None" = None


class LLMClient:
    """
    Thin wrapper around the OpenAI SDK.

    All LLM calls go through this class. Business logic lives in services.
    """

    def __new__(cls) -> "LLMClient":
        global _instance
        if _instance is None:
            _instance = super().__new__(cls)
            _instance._initialised = False
        return _instance

    def __init__(self) -> None:
        if self._initialised:
            return
        self.client = OpenAI(
            base_url=settings.OPENROUTER_BASE_URL,
            api_key=settings.OPENROUTER_API_KEY,
        )
        self.model = settings.AI_MODEL
        self._is_groq = _GROQ_BASE_URL in (settings.OPENROUTER_BASE_URL or "")
        if self._is_groq:
            logger.info("LLMClient: Groq backend detected — compatibility mode enabled")
        self._initialised = True

    def chat(
        self,
        messages:        list,
        tools:           list | None = None,
        response_format: dict | None = None,
        max_tokens:      int         = 400,
        temperature:     float       = 0.2,
        session_id:      str         = "",
    ):
        """
        Make a chat completion request.

        Args:
            messages:        Conversation history + system prompt
            tools:           OpenAI tool definitions (optional)
            response_format: e.g. {"type": "json_object"} (optional)
            max_tokens:      Response token limit
            temperature:     Sampling temperature
            session_id:      For logging correlation

        Returns:
            OpenAI ChatCompletion response object
        """
        if session_id:
            set_current_session_id(session_id)

        params: dict = {
            "model":       self.model,
            "messages":    self._clean_messages(messages),
            "max_tokens":  max_tokens,
            "temperature": temperature,
        }

        if tools:
            params["tools"] = tools
            if response_format and not self._is_groq:
                params["response_format"] = response_format
            elif response_format and self._is_groq:
                # Groq workaround: inject JSON instruction instead of response_format
                params["messages"] = self._inject_json_instruction(params["messages"])
        elif response_format:
            params["response_format"] = response_format

        t0 = time.perf_counter()
        response = self.client.chat.completions.create(**params)
        latency_ms = round((time.perf_counter() - t0) * 1000)

        logger.info(
            "LLM call | model=%s | session=%s | messages=%d | tools=%s | temp=%.1f | max_tokens=%d | %dms | tokens=%s",
            self.model,
            get_current_session_id(),
            len(params["messages"]),
            bool(tools),
            params.get("temperature", 0.0),
            params.get("max_tokens", 0),
            latency_ms,
            getattr(getattr(response, "usage", None), "total_tokens", "n/a"),
        )
        return response

    def _inject_json_instruction(self, messages: list) -> list:
        """Groq workaround: insert a system message that enforces JSON output."""
        instruction = {
            "role": "system",
            "content": (
                "IMPORTANT: Your final response (after any tool calls) "
                "MUST be valid JSON only. No prose, no markdown, no explanation. "
                "Return only the raw JSON object as instructed."
            ),
        }
        msgs = list(messages)
        for i in reversed(range(len(msgs))):
            if isinstance(msgs[i], dict) and msgs[i].get("role") == "user":
                msgs.insert(i, instruction)
                break
        else:
            msgs.append(instruction)
        return msgs

    def _clean_messages(self, messages: list) -> list:
        """
        Normalise messages to plain dicts.

        - Converts OpenAI SDK response objects to dicts
        - Strips fields Groq rejects (metadata, None values)
        """
        UNSUPPORTED = {"metadata"}
        cleaned = []
        for m in messages:
            if hasattr(m, "model_dump"):
                m = m.model_dump(exclude_none=True)
            elif hasattr(m, "__dict__"):
                m = {k: v for k, v in m.__dict__.items() if v is not None}
            if isinstance(m, dict):
                m = {k: v for k, v in m.items() if k not in UNSUPPORTED and v is not None}
            cleaned.append(m)
        return cleaned
