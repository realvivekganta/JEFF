"""Context isolation, saved preferences, tool validation, and model loop behavior."""

import json
from copy import deepcopy
import pytest
from main import Settings
from core.context import build_context, model_tools
from core.loop import handle_turn
from core.session import create_session, session_view
from core import tools


def call(name, **args):
    return {
        "type": "function_call",
        "call_id": "test-" + name,
        "name": name,
        "arguments": json.dumps(args),
    }


def say(text):
    return {"type": "message", "content": [{"type": "output_text", "text": text}]}


def decision(approved=True):
    return say(json.dumps({"decision": "approve" if approved else "not_approve"}))


def fake_model(*outputs):
    remaining = list(outputs)
    requests = []

    async def request(payload):
        requests.append(deepcopy(payload))
        assert remaining, "Unexpected model request"
        return {"status": "completed", "output": remaining.pop(0)}

    return request, requests


async def identified():
    s = create_session()
    s["transcript"].append({"role": "user", "text": "XK72LP"})
    tools.lookup_reservation(s, "XK72LP")
    return s


def test_unidentified_context_contains_no_passengers_or_reservations():
    payload = build_context(create_session(), Settings())
    text = json.dumps(payload)
    assert "Sam Taylor" not in text and "XK72LP" not in text and "BOS842" not in text
    assert [t["name"] for t in payload["tools"]] == ["lookup_reservation", "request_human"]


async def test_context_scopes_records_and_preserves_preferences_beyond_40_messages():
    s = await identified()
    s["transcript"].append({"role": "user", "text": "window seat before 9 PM"})
    tools.update_preferences(s, seat_type="window", arrival_before="21:00")
    s["transcript"] += [{"role": "user", "text": "follow-up"}] * 50
    payload = build_context(s, Settings())
    assert len(payload["input"]) == 40
    text = payload["instructions"]
    assert "window" in text and "21:00" in text
    assert "Alex Morgan" not in text and "BOS842" not in text
    assert "rebook_flight" not in [t["name"] for t in payload["tools"]]


async def test_model_search_tool_results_and_reasoning_then_proposal():
    s = await identified()
    reasoning = {"type": "reasoning", "encrypted_content": "fixture", "summary": []}
    request, requests = fake_model(
        [reasoning, call("search_flights")], [call("propose_rebooking", flight_id="F2", seat="14A")], [decision()]
    )
    reply = await handle_turn(s, "Find the earlier option", Settings(), request)
    assert "Shall I confirm" in reply
    assert reasoning in requests[1]["input"]
    assert any(x.get("type") == "function_call_output" for x in requests[1]["input"])
    assert "DL2219" in requests[1]["instructions"]
    assert session_view(s)["reservation"]["status"] == "CANCELLED"
    await handle_turn(s, "Yes", Settings(), request)
    assert len(requests) == 3 and s["stage"] == "resolved"


@pytest.mark.parametrize(
    "action",
    [
        call("rebook_flight", proposal_id="fake"),
        call("propose_rebooking", flight_id="F5", seat="12C"),
        call("search_flights", invent=True),
        call("lookup_reservation", confirmation="BOS842"),
    ],
)
async def test_invalid_or_unrequested_model_tools_cannot_commit_or_lookup_other_customer(action):
    s = await identified()
    tools.search_flights(s)
    request, _ = fake_model([action])
    await handle_turn(s, "What can you do?", Settings(), request)
    assert s["stage"] == "escalated"
    assert s["reservation_id"] == "R1"
    assert session_view(s)["reservation"]["status"] == "CANCELLED"


async def test_false_model_success_is_not_spoken():
    s = await identified()
    request, _ = fake_model([say("Your flight is confirmed!")])
    reply = await handle_turn(s, "Tell me about my booking", Settings(), request)
    assert "haven’t changed" in reply


async def test_provider_error_is_redacted():
    async def fail(payload):
        raise RuntimeError("secret provider detail")

    s = await identified()
    await handle_turn(s, "What are the options?", Settings(), fail)
    assert s["stage"] == "escalated"
    assert "secret provider" not in json.dumps(session_view(s))


async def test_loop_step_limit_stops_repeated_tool_calls():
    s = await identified()
    request, requests = fake_model(*[[call("get_reservation")]] * 6)
    await handle_turn(s, "Tell me more", Settings(), request)
    assert len(requests) == 6 and s["stage"] == "escalated"


async def test_every_natural_utterance_reaches_model_including_lookup():
    s = create_session()
    request, requests = fake_model(
        [say('Tell me what happened.')],
        [call('lookup_reservation', confirmation='XK72LP')],
        [say('I found your canceled flight. Let’s find a way home.')],
    )
    assert await handle_turn(s, 'Hello', Settings(), request) == 'Tell me what happened.'
    reply = await handle_turn(s, 'My code is XK72LP', Settings(), request)
    assert reply == 'I found your canceled flight. Let’s find a way home.'
    assert len(requests) == 3
    assert requests[1]['input'][-1]['content'] == 'My code is XK72LP'
    assert session_view(s)['customer']['confirmation'] == 'XK72LP'
    assert requests[2]['input'][0]['content'] == 'Hello'


async def test_delegation_is_passed_to_model_without_regex_selection():
    s = await identified()
    tools.search_flights(s)
    request, requests = fake_model([call('propose_rebooking', flight_id='F2', seat='14A')])
    await handle_turn(s, 'Surprise me, just get me home quickly', Settings(), request)
    assert requests[0]['input'][-1]['content'] == 'Surprise me, just get me home quickly'
    assert s['stage'] == 'confirm'
    assert session_view(s)['reservation']['status'] == 'CANCELLED'
    assert 'lookup_reservation' not in [t['name'] for t in requests[0]['tools']]


async def test_unsupported_intent_is_interpreted_by_model_and_handoff_cannot_commit():
    s = await identified()
    request, requests = fake_model([call('request_human')])
    await handle_turn(s, 'Could a person sort out a refund for me?', Settings(), request)
    assert len(requests) == 1
    assert s['stage'] == 'escalated'
    assert session_view(s)['reservation']['status'] == 'CANCELLED'
