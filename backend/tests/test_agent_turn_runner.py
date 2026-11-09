# Tests for the agent turn runner's delta/commentary publication timing.

import pytest
from sqlmodel import Session, select

from app.db import get_engine, init_db
from app.models.entities import AgentConversation, AgentMessage
from app.services import agent_gateway, agent_turn
from app.services.agent_gateway import AgentEvent
from app.services.agent_stream import AgentStreamBroker


@pytest.fixture
def runner_broker(tmp_app_data, monkeypatch):
    init_db()
    broker = AgentStreamBroker()
    monkeypatch.setattr(agent_turn, "agent_stream", broker)
    return broker


def _seed_conversation(conversation_id: str) -> None:
    with Session(get_engine()) as session:
        session.add(
            AgentConversation(
                id=conversation_id,
                project_id="1",
                title="测试会话",
                dsh_session_id="sess-test",
                status="busy",
            )
        )
        session.add(
            AgentMessage(
                conversation_id=conversation_id,
                role="user",
                content="你好",
            )
        )
        session.commit()


def _monkeypatch_stream(monkeypatch, events):
    def _stream_prompt(session_id, prompt, *, should_stop=None, stats=None, transport=None):
        for event in events:
            yield event

    monkeypatch.setattr(agent_turn.agent_gateway, "stream_prompt", _stream_prompt)


def _events_after(broker, conversation_id):
    poll = broker.poll_after(conversation_id, 0)
    assert poll.kind == "events", poll
    return list(poll.events)


def _assistant_messages(conversation_id):
    with Session(get_engine()) as session:
        return list(
            session.exec(
                select(AgentMessage)
                .where(AgentMessage.conversation_id == conversation_id)
                .where(AgentMessage.role == "assistant")
            ).all()
        )


def test_commentary_before_tool_is_not_published_as_answer(runner_broker, monkeypatch):
    conversation_id = "conv-tool"
    _seed_conversation(conversation_id)
    _monkeypatch_stream(
        monkeypatch,
        [
            AgentEvent(kind="connected", session_id="sess-test"),
            AgentEvent(kind="reasoning", text="think"),
            AgentEvent(kind="delta", text="commentary"),
            AgentEvent(kind="tool_call", text=""),
            AgentEvent(kind="delta", text="final"),
            AgentEvent(kind="completed"),
        ],
    )

    result = agent_turn.run_turn(conversation_id)

    assert result["outcome"] == "completed"
    assert result["text"] == "final"

    events = _events_after(runner_broker, conversation_id)
    deltas = [event for event in events if event.event == "delta"]
    assert [event.payload["delta"] for event in deltas] == ["final"]

    reasoning = [event for event in events if event.event == "reasoning"]
    assert any(event.payload["delta"] == "think" for event in reasoning)
    commentary_events = [
        event for event in reasoning if "commentary" in event.payload.get("delta", "")
    ]
    assert len(commentary_events) == 1

    # The final answer delta is published only after commentary is folded into
    # the reasoning stream, so the live bubble never shows commentary.
    assert deltas[0].sequence > commentary_events[0].sequence

    snapshot = runner_broker.snapshot(conversation_id)
    assert snapshot is not None
    assert snapshot["text"] == "final"
    assert "think" in snapshot["reasoning"]
    assert "commentary" in snapshot["reasoning"]

    messages = _assistant_messages(conversation_id)
    assert len(messages) == 1
    assert messages[0].content == "final"
    assert "think" in messages[0].reasoning
    assert "commentary" in messages[0].reasoning


def test_answer_without_tool_stays_out_of_reasoning(runner_broker, monkeypatch):
    conversation_id = "conv-plain"
    _seed_conversation(conversation_id)
    _monkeypatch_stream(
        monkeypatch,
        [
            AgentEvent(kind="connected", session_id="sess-test"),
            AgentEvent(kind="reasoning", text="think"),
            AgentEvent(kind="delta", text="answer"),
            AgentEvent(kind="completed"),
        ],
    )

    result = agent_turn.run_turn(conversation_id)

    assert result["outcome"] == "completed"
    assert result["text"] == "answer"

    events = _events_after(runner_broker, conversation_id)
    deltas = [event for event in events if event.event == "delta"]
    assert [event.payload["delta"] for event in deltas] == ["answer"]

    snapshot = runner_broker.snapshot(conversation_id)
    assert snapshot is not None
    assert snapshot["text"] == "answer"
    assert snapshot["reasoning"] == "think"

    messages = _assistant_messages(conversation_id)
    assert len(messages) == 1
    assert messages[0].content == "answer"
    assert messages[0].reasoning == "think"
