"""
chat_service.py
---------------
Chat orchestration for a profile.

Responsibilities:
  - Build context from RAG (per profile)
  - Call LLM with system prompt + context + history
  - Handle tool calls (record_user_details, record_unknown_question)
  - Generate followup questions
  - Accumulate and return token usage for every turn
  - Structured logging per session

One ChatService instance is used for all profiles. It is stateless.
Per-profile state (engine, prompts) is looked up on each request.
"""

import json
import re
import time
from datetime import datetime, timezone
from typing import Optional

from fastapi import BackgroundTasks

from app.core.logging_config import (
    get_logger, get_chat_logger, get_profile_logger,
    get_session_logger, new_session_id, set_current_session_id,
)
from app.services.token_service import token_service
from app.models.api_models import ChatMessage, ChatResponse, TokenUsage
from app.services.index_service import index_service, is_warming_up, trigger_on_demand
from app.services.prompt_service import prompt_service
from app.rag.llm_client import LLMClient
from app.storage.file_storage import ProfileFileStorage
from app.services.notification_service import notification_service

logger   = get_logger(__name__)
chat_log = get_chat_logger()

# ── Tool definitions ──────────────────────────────────────────────────────────

_TOOLS = [
    {
        "type": "function",
        "function": {
            "name": "record_user_details",
            "description": "Record a visitor's email when they want to connect.",
            "parameters": {
                "type": "object",
                "properties": {
                    "email": {"type": "string"},
                    "name":  {"type": "string"},
                    "notes": {"type": "string"},
                },
                "required": ["email"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "record_unknown_question",
            "description": "Record a question that cannot be answered from the profile context.",
            "parameters": {
                "type": "object",
                "properties": {
                    "question": {"type": "string"},
                },
                "required": ["question"],
            },
        },
    },
]


# ── Token accumulator ─────────────────────────────────────────────────────────

class _TokenBudget:
    """
    Mutable accumulator for all LLM token usage within a single chat turn.

    Pass one instance through every LLM call in the turn so the final
    ChatResponse carries a full picture of cost.
    """
    __slots__ = ("prompt", "completion", "calls")

    def __init__(self) -> None:
        self.prompt = self.completion = self.calls = 0

    def add(self, usage) -> None:
        """Add token counts from an OpenAI response.usage object (may be None)."""
        if usage is None:
            return
        self.prompt     += getattr(usage, "prompt_tokens",     0) or 0
        self.completion += getattr(usage, "completion_tokens", 0) or 0
        self.calls      += 1

    @property
    def total(self) -> int:
        return self.prompt + self.completion

    def to_model(self) -> TokenUsage:
        return TokenUsage(
            prompt_tokens     = self.prompt,
            completion_tokens = self.completion,
            total_tokens      = self.total,
            call_count        = self.calls,
        )


# ── Service ───────────────────────────────────────────────────────────────────

class ChatService:
    """
    Stateless chat orchestrator.

    All per-profile config (prompts, RAG engine) is loaded at call time.
    Session state lives in the client (history list).
    """

    def __init__(self) -> None:
        self.llm = LLMClient()

    # ── Public API ────────────────────────────────────────────────────────────

    def chat(
        self,
        slug:             str,
        message:          str,
        history:          list[ChatMessage],
        session_id:       str = "",
        background_tasks: Optional[BackgroundTasks] = None,
    ) -> ChatResponse:
        """
        Process one chat turn for a profile.

        background_tasks: when provided, non-critical I/O (chat event persistence,
        notifications) is deferred so the HTTP response is not blocked.

        Returns:
            ChatResponse with answer, followup questions, and full token usage.
        """
        t0    = time.perf_counter()
        sid   = session_id or new_session_id()
        set_current_session_id(sid)
        slog  = get_session_logger(logger, sid)
        plog  = get_profile_logger(slug)
        budget = _TokenBudget()

        slog.info("── Chat turn | slug=%s | query=%s", slug, message[:80])

        # Load per-profile chat history limit from preferences
        from app.services.preferences_service import preferences_service
        from app.core.constants import CHAT_HISTORY_LIMIT_DEFAULT
        prefs         = preferences_service.get(slug)
        history_limit = int(prefs.get("chat_history_limit", CHAT_HISTORY_LIMIT_DEFAULT))

        # Trim history to owner-configured window
        trimmed_history = history[-history_limit:]
        history_trimmed = len(history) > history_limit

        # If an on-demand index is already in flight for this profile, tell the visitor
        if is_warming_up(slug):
            slog.info("Chat blocked | slug=%s | reason=on-demand index in flight", slug)
            return ChatResponse(
                answer     = "I'm getting ready for you — loading my knowledge base. Please try again in 2 minutes.",
                followups  = [],
                session_id = sid,
                warming_up = True,
            )

        # Get RAG engine — get_engine() always returns a non-None engine if the profile
        # folder exists, even when ChromaDB is empty (0 chunks). So we must check both
        # engine is None (folder missing) AND chunk_count == 0 (folder exists, not indexed).
        engine = index_service.get_engine(slug)
        if engine is None or engine.chunk_count() == 0:
            # trigger_on_demand is idempotent and skips silently if already in flight or no docs
            trigger_on_demand(slug)
            if is_warming_up(slug):
                # on-demand was just triggered (or was already running)
                slog.info("Chat blocked | slug=%s | reason=index building on-demand", slug)
                return ChatResponse(
                    answer     = "I'm getting ready for you — loading my knowledge base. Please try again in 2 minutes.",
                    followups  = [],
                    session_id = sid,
                    warming_up = True,
                )
            else:
                # trigger_on_demand skipped — no documents uploaded
                slog.warning("Chat blocked | slug=%s | reason=no documents uploaded", slug)
                if background_tasks:
                    background_tasks.add_task(
                        notification_service.notify_incomplete_profile, slug, sid,
                    )
                else:
                    notification_service.notify_incomplete_profile(slug, sid)
                return ChatResponse(
                    answer     = (
                        "This profile hasn't been set up fully yet. "
                        "I've notified the profile owner and the platform administrator. "
                        "Please check back again shortly."
                    ),
                    followups  = [],
                    session_id = sid,
                )

        # RAG retrieval (intent classification tokens tracked inside engine)
        context_chunks = engine.retrieve(message, k=4)
        context_block  = "\n\n".join(context_chunks)
        slog.info("RAG: %d chunks | %d chars", len(context_chunks), len(context_block))

        # Build system prompt
        snapshot   = engine.build_snapshot()
        sys_prompt = prompt_service.system_prompt(slug).format(
            name      = slug,
            followups = prompt_service.fallback_followups(),
        )

        messages = [
            {"role": "system", "content": sys_prompt},
            {"role": "system", "content": f"[PROFILE CONTEXT]\n{context_block}"},
            *[{"role": m.role, "content": m.content} for m in trimmed_history],
            {"role": "user",   "content": message},
        ]

        # Main LLM call (tool loop)
        answer = self._llm_loop(messages, slug, sid, slog, budget)

        # Followup generation
        _was_answered = not any(p in answer.lower() for p in prompt_service.unknown_phrases())
        turn_followups = self._generate_turn_followups(
            slug         = slug,
            question     = message,
            answer       = answer,
            snapshot     = snapshot,
            was_answered = _was_answered,
            budget       = budget,
        )
        final_followups = turn_followups or prompt_service.fallback_followups()

        # Persist token usage for admin dashboard (synchronous — fast local write)
        token_service.record(slug, "query", budget.prompt, budget.completion, budget.total)

        latency_ms = round((time.perf_counter() - t0) * 1000)

        # Structured chat event — defer to background so HTTP response is not blocked
        event = {
            "ts":           datetime.now(timezone.utc).isoformat(),
            "session_id":   sid,
            "question":     message,
            "answer":       answer,
            "followups":    final_followups,
            "tokens":       budget.total,
            "latency_ms":   latency_ms,
            "was_answered": _was_answered,
        }
        if background_tasks is not None:
            background_tasks.add_task(ProfileFileStorage(slug).append_chat_event, event)
        else:
            ProfileFileStorage(slug).append_chat_event(event)

        # Structured logging
        slog.info(
            "Turn complete | tokens=%d (prompt=%d compl=%d calls=%d) | %dms | history_trimmed=%s",
            budget.total, budget.prompt, budget.completion, budget.calls, latency_ms, history_trimmed,
        )
        chat_log.info(
            "slug=%s | tokens=%d | Q=%s | A=%s",
            slug, budget.total, message[:80], answer[:120],
        )
        plog.info(
            "chat | tokens=%d | Q=%s | A=%s",
            budget.total, message[:80], answer[:80],
        )

        return ChatResponse(
            answer          = answer,
            followups       = final_followups,
            session_id      = sid,
            tokens_used     = budget.to_model(),
            history_trimmed = history_trimmed,
            latency_ms      = latency_ms,
        )

    def get_initial_followups(self, slug: str) -> list[str]:
        """Generate opening followup questions shown when the chat UI loads."""
        try:
            engine = index_service.get_engine(slug)
            if not engine or engine.chunk_count() == 0:
                # Kick off on-demand indexing at page load so it's ready before the first message.
                # Return empty — generic fallbacks are irrelevant to this profile's content.
                trigger_on_demand(slug)
                logger.info("get_initial_followups: no index for '%s' — returning empty", slug)
                return []

            snapshot = engine.build_snapshot()
            if not snapshot.strip():
                return prompt_service.fallback_followups()
        except Exception as e:
            logger.warning("get_initial_followups: engine error for '%s': %s — returning empty", slug, e)
            return []

        from app.services.profile_service import profile_service
        name   = profile_service.get_display_name(slug)
        prompt = prompt_service.initial_followups_prompt(slug).format(
            name            = name,
            profile_context = snapshot[:4000],
        )

        budget    = _TokenBudget()
        questions = self._call_llm_for_followups(prompt, budget)
        logger.info("Initial followups | slug=%s | tokens=%d", slug, budget.total)
        return questions if len(questions) == 3 else prompt_service.fallback_followups()

    def get_welcome_message(self, slug: str) -> str:
        """Return the welcome message for a profile, formatted with the profile name."""
        from app.services.profile_service import profile_service
        name       = profile_service.get_display_name(slug)
        short_name = name.split()[0] if name else slug
        tmpl       = prompt_service.welcome_message(slug)
        try:
            return tmpl.format(name=short_name)
        except KeyError:
            logger.warning("welcome_message template for '%s' has unexpected placeholders", slug)
            return tmpl

    # ── LLM loop ──────────────────────────────────────────────────────────────

    def _llm_loop(
        self,
        messages: list,
        slug:     str,
        sid:      str,
        slog,
        budget:   _TokenBudget,
    ) -> str:
        """
        Run the main LLM call, handling tool calls until a text response arrives.
        Accumulates token usage into `budget`.
        Returns the answer text.
        """
        tool_round = 0
        while True:
            try:
                response = self.llm.chat(
                    messages        = messages,
                    tools           = _TOOLS,
                    response_format = {"type": "json_object"},
                    max_tokens      = 500,
                    session_id      = sid,
                )
            except Exception as e:
                slog.error("LLM call failed: %s", e, exc_info=True)
                notification_service.notify_llm_error("LLM call failed", str(e), sid)
                return self._error_message(str(e))

            budget.add(getattr(response, "usage", None))
            choice = response.choices[0]

            if choice.message.tool_calls:
                tool_round += 1
                tool_names  = [tc.function.name for tc in choice.message.tool_calls]
                slog.info("Tool round #%d: %s", tool_round, tool_names)
                messages.append(choice.message)
                messages.extend(self._handle_tool_calls(choice.message.tool_calls, slug, sid))
                continue

            # Text response — parse JSON envelope
            raw    = choice.message.content or ""
            answer = self._parse_answer(raw, slog)
            slog.info("LLM answered | tokens_this_call=%d",
                      getattr(getattr(response, "usage", None), "total_tokens", 0) or 0)
            return answer

    def _parse_answer(self, raw: str, slog) -> str:
        """Extract answer text from the LLM JSON envelope. Returns raw on failure."""
        try:
            data = json.loads(raw)
            return data.get("answer", raw)
        except Exception:
            slog.warning("LLM reply is not valid JSON — returning raw text (len=%d)", len(raw))
            return raw

    # ── Tool handling ─────────────────────────────────────────────────────────

    def _handle_tool_calls(self, tool_calls, slug: str, sid: str) -> list[dict]:
        results = []
        for call in tool_calls:
            try:
                args = json.loads(call.function.arguments)
            except json.JSONDecodeError as e:
                logger.warning("Malformed tool arguments for '%s': %s", call.function.name, e)
                args = {}
            args["session_id"] = sid
            result = self._dispatch_tool(call.function.name, args, slug)
            results.append({
                "role":        "tool",
                "content":     json.dumps(result),
                "tool_call_id": call.id,
            })
        return results

    def _dispatch_tool(self, name: str, args: dict, slug: str) -> dict:
        plog = get_profile_logger(slug)
        # Log args before dispatch (mask nothing — these are non-sensitive tool inputs)
        safe_args = {k: v for k, v in args.items() if k != "session_id"}
        plog.info("Tool dispatch | name=%s | args=%s", name, json.dumps(safe_args, default=str))

        if name == "record_user_details":
            email      = args.get("email", "")
            name_val   = args.get("name", "")
            session_id = args.get("session_id", "")
            plog.info("Lead captured | email=%s", email)
            chat_log.info("LEAD | slug=%s | email=%s", slug, email)
            notification_service.notify_lead(name=name_val, email=email, session_id=session_id, slug=slug)
            result = {"status": "lead recorded"}
            plog.info("Tool result | name=%s | result=%s", name, result)
            return result

        if name == "record_unknown_question":
            question   = args.get("question", "")
            session_id = args.get("session_id", "")
            plog.info("Unknown question logged | question=%s", question)
            chat_log.info("UNKNOWN | slug=%s | question=%s", slug, question)
            # Pushover push + conditional owner email (NotificationService handles both)
            notification_service.notify_unknown_question(
                question=question, session_id=session_id, slug=slug
            )
            result = {"status": "unknown recorded"}
            plog.info("Tool result | name=%s | result=%s", name, result)
            return result

        logger.warning("Unrecognised tool called: '%s' — ignoring", name)
        return {"status": "unknown tool"}

    # ── Followup generation ───────────────────────────────────────────────────

    def _generate_turn_followups(
        self,
        slug:         str,
        question:     str,
        answer:       str,
        snapshot:     str,
        was_answered: bool,
        budget:       _TokenBudget,
    ) -> list[str]:
        from app.services.profile_service import profile_service
        name   = profile_service.get_display_name(slug)
        prompt = prompt_service.turn_followups_prompt(slug).format(
            name            = name,
            question        = question,
            answer          = answer[:300],
            was_answered    = "true" if was_answered else "false",
            profile_context = snapshot[:3000],
        )
        return self._call_llm_for_followups(prompt, budget)

    def _call_llm_for_followups(self, prompt: str, budget: _TokenBudget) -> list[str]:
        """
        Call the LLM to produce a JSON list of 3 followup questions.
        Accumulates usage into `budget`. Returns [] on any failure.
        """
        try:
            response = self.llm.chat(
                [{"role": "user", "content": prompt}],
                max_tokens  = 200,
                temperature = 0.6,
            )
            budget.add(getattr(response, "usage", None))
            content = response.choices[0].message.content or "[]"
            content = re.sub(r"^```(?:json)?\s*", "", content.strip())
            content = re.sub(r"\s*```$",          "", content).strip()
            parsed  = json.loads(content)
            if isinstance(parsed, list):
                return [q for q in parsed if isinstance(q, str) and q.strip()][:3]
        except json.JSONDecodeError as e:
            logger.warning("Followup JSON parse failed: %s", e)
        except Exception as e:
            logger.warning("Followup LLM call failed: %s", e, exc_info=True)
        return []

    # ── Helpers ───────────────────────────────────────────────────────────────

    @staticmethod
    def _error_message(error: str) -> str:
        if "quota" in error.lower() or "402" in error:
            return "I'm currently experiencing high demand. Please try again shortly."
        return "I'm experiencing a technical issue. Please try again shortly."


# Singleton
chat_service = ChatService()
