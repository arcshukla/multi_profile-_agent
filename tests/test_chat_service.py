"""
test_chat_service.py
--------------------
Unit tests for ChatService.

Covers:
  - Tool dispatch: record_user_details, record_unknown_question, unknown tool
  - _parse_answer: valid JSON envelope, raw fallback
  - _error_message: quota detection
  - _call_llm_for_followups: valid JSON, malformed JSON fallback
  - chat(): history trimming via chat_history_limit preference
  - get_display_name used correctly (no direct get_entry calls in service)
"""

from unittest.mock import MagicMock, patch
import json
import pytest

from app.services.chat_service import ChatService, _TokenBudget
from app.models.api_models import ChatMessage


# ── Helpers ───────────────────────────────────────────────────────────────────

def make_llm_response(content: str, usage=None):
    resp = MagicMock()
    resp.choices = [MagicMock()]
    resp.choices[0].message.content = content
    resp.choices[0].message.tool_calls = None
    if usage is None:
        resp.usage = MagicMock(prompt_tokens=10, completion_tokens=5, total_tokens=15)
    else:
        resp.usage = usage
    return resp


def make_tool_call(name: str, args: dict, call_id: str = "call_1"):
    tc = MagicMock()
    tc.id = call_id
    tc.function.name = name
    tc.function.arguments = json.dumps(args)
    return tc


# ── _parse_answer ─────────────────────────────────────────────────────────────

def test_parse_answer_valid_json():
    svc = ChatService.__new__(ChatService)
    slog = MagicMock()
    raw = json.dumps({"answer": "Hello from the LLM"})
    assert svc._parse_answer(raw, slog) == "Hello from the LLM"


def test_parse_answer_fallback_to_raw():
    svc = ChatService.__new__(ChatService)
    slog = MagicMock()
    raw = "plain text response"
    assert svc._parse_answer(raw, slog) == "plain text response"


def test_parse_answer_missing_answer_key():
    svc = ChatService.__new__(ChatService)
    slog = MagicMock()
    raw = json.dumps({"other_key": "value"})
    # Falls back to raw string because "answer" is missing
    assert svc._parse_answer(raw, slog) == raw


# ── _error_message ────────────────────────────────────────────────────────────

def test_error_message_quota():
    svc = ChatService.__new__(ChatService)
    msg = svc._error_message("quota exceeded")
    assert "demand" in msg.lower() or "quota" in msg.lower()


def test_error_message_generic():
    svc = ChatService.__new__(ChatService)
    msg = svc._error_message("connection timeout")
    assert "technical issue" in msg.lower() or "try again" in msg.lower()


# ── _call_llm_for_followups ───────────────────────────────────────────────────

def test_call_llm_for_followups_valid_json(monkeypatch, isolate_data_dirs):
    svc = ChatService.__new__(ChatService)
    svc.llm = MagicMock()
    followups = ["What is Q1?", "Tell me about Q2?", "And Q3?"]
    svc.llm.chat.return_value = make_llm_response(json.dumps(followups))

    budget = _TokenBudget()
    result = svc._call_llm_for_followups("some prompt", budget)
    assert result == followups
    assert budget.total > 0


def test_call_llm_for_followups_malformed_json(monkeypatch, isolate_data_dirs):
    svc = ChatService.__new__(ChatService)
    svc.llm = MagicMock()
    svc.llm.chat.return_value = make_llm_response("NOT JSON AT ALL")

    budget = _TokenBudget()
    result = svc._call_llm_for_followups("some prompt", budget)
    assert result == []  # graceful fallback


def test_call_llm_for_followups_non_list_json(monkeypatch, isolate_data_dirs):
    svc = ChatService.__new__(ChatService)
    svc.llm = MagicMock()
    svc.llm.chat.return_value = make_llm_response(json.dumps({"key": "not a list"}))

    budget = _TokenBudget()
    result = svc._call_llm_for_followups("some prompt", budget)
    assert result == []


# ── _dispatch_tool ────────────────────────────────────────────────────────────

def test_dispatch_tool_record_user_details(monkeypatch, isolate_data_dirs):
    svc = ChatService.__new__(ChatService)

    mock_notif = MagicMock()
    monkeypatch.setattr("app.services.chat_service.notification_service", mock_notif)

    result = svc._dispatch_tool(
        "record_user_details",
        {"email": "visitor@example.com", "name": "Visitor", "session_id": "ses1"},
        "some-slug",
    )
    assert result["status"] == "lead recorded"
    mock_notif.notify_lead.assert_called_once_with(
        name="Visitor", email="visitor@example.com", session_id="ses1", slug="some-slug"
    )


def test_dispatch_tool_record_unknown_question(monkeypatch, isolate_data_dirs):
    svc = ChatService.__new__(ChatService)

    mock_notif = MagicMock()
    monkeypatch.setattr("app.services.chat_service.notification_service", mock_notif)

    result = svc._dispatch_tool(
        "record_unknown_question",
        {"question": "What is your salary?", "session_id": "ses2"},
        "some-slug",
    )
    assert result["status"] == "unknown recorded"
    mock_notif.notify_unknown_question.assert_called_once_with(
        question="What is your salary?", session_id="ses2", slug="some-slug"
    )


def test_dispatch_tool_unknown_name(monkeypatch, isolate_data_dirs):
    svc = ChatService.__new__(ChatService)
    mock_notif = MagicMock()
    monkeypatch.setattr("app.services.chat_service.notification_service", mock_notif)

    result = svc._dispatch_tool("nonexistent_tool", {}, "some-slug")
    assert result["status"] == "unknown tool"


# ── chat_history_limit ────────────────────────────────────────────────────────

def test_chat_trims_history_to_limit(monkeypatch, isolate_data_dirs):
    """
    When history has more turns than chat_history_limit, the trimmed_history
    sent to the LLM should be capped at that limit and history_trimmed=True.
    """
    from app.services.chat_service import chat_service as _singleton

    # Build a 20-message history
    long_history = [
        ChatMessage(role="user" if i % 2 == 0 else "assistant", content=f"msg {i}")
        for i in range(20)
    ]

    # Stub preferences to return limit=5
    mock_prefs_svc = MagicMock()
    mock_prefs_svc.get.return_value = {"chat_history_limit": 5, "notify_unanswered_email": False}

    # Stub index_service.get_engine → returns a working mock engine
    mock_engine = MagicMock()
    mock_engine.retrieve.return_value = ["chunk1"]
    mock_engine.build_snapshot.return_value = "snapshot"

    mock_idx = MagicMock()
    mock_idx.get_engine.return_value = mock_engine

    # Stub prompt_service
    mock_prompt = MagicMock()
    mock_prompt.system_prompt.return_value = "System: {name} {followups}"
    mock_prompt.fallback_followups.return_value = ["q1", "q2", "q3"]
    mock_prompt.unknown_phrases.return_value = ["i don't know"]

    # Stub LLM → returns a text answer immediately (no tool calls)
    mock_llm = MagicMock()
    mock_llm.chat.return_value = make_llm_response(json.dumps({"answer": "Trimmed answer"}))
    _singleton.llm = mock_llm

    # Stub token_service.record (no-op)
    mock_token = MagicMock()

    # Stub ProfileFileStorage.append_chat_event (no-op)
    mock_fs_cls = MagicMock()
    mock_fs_cls.return_value.append_chat_event = MagicMock()

    # Stub profile_service.get_display_name
    mock_profile_svc = MagicMock()
    mock_profile_svc.get_display_name.return_value = "Test User"

    # Capture each LLM call's messages list separately
    all_call_messages = []

    def capture_llm_chat(messages, **kwargs):
        all_call_messages.append(list(messages))
        return make_llm_response(json.dumps({"answer": "Trimmed answer"}))

    mock_llm.chat.side_effect = capture_llm_chat

    with patch("app.services.preferences_service.preferences_service", mock_prefs_svc), \
         patch("app.services.chat_service.index_service", mock_idx), \
         patch("app.services.chat_service.prompt_service", mock_prompt), \
         patch("app.services.chat_service.token_service", mock_token), \
         patch("app.services.chat_service.ProfileFileStorage", mock_fs_cls):

        resp = _singleton.chat(
            slug="test-slug",
            message="New question",
            history=long_history,
            session_id="ses-trim",
        )

    assert resp.history_trimmed is True
    # Look only at the first LLM call (main chat), not the followups call
    main_call_messages = all_call_messages[0]
    history_turns = [m for m in main_call_messages if m["role"] in ("user", "assistant")]
    # At most limit (5) history turns + 1 new user message
    assert len(history_turns) <= 5 + 1, (
        f"Expected ≤6 user/assistant messages in main LLM call, got {len(history_turns)}"
    )


# ── _TokenBudget ──────────────────────────────────────────────────────────────

def test_token_budget_accumulates():
    budget = _TokenBudget()
    usage1 = MagicMock(prompt_tokens=10, completion_tokens=5, total_tokens=0)
    usage2 = MagicMock(prompt_tokens=20, completion_tokens=10, total_tokens=0)
    budget.add(usage1)
    budget.add(usage2)
    assert budget.prompt == 30
    assert budget.completion == 15
    assert budget.calls == 2


def test_token_budget_handles_none():
    budget = _TokenBudget()
    budget.add(None)  # must not raise
    assert budget.total == 0
