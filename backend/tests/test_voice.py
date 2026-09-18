"""Signed Twilio protocol checks against the actual FastAPI routes."""

import pytest
from fastapi.testclient import TestClient
from starlette.websockets import WebSocketDisconnect
from twilio.request_validator import RequestValidator
from main import create_apps
from main import Settings
from core.session import create_runtime
from test_context import call, say, fake_model, decision

BASE = "https://demo.example.test"
TOKEN = "fixture-token"
CALLER = "+12025550123"


@pytest.fixture
def clients():
    runtime = create_runtime(
        Settings(public_base_url=BASE, twilio_auth_token=TOKEN, demo_caller_number=CALLER)
    )
    dashboard, voice = create_apps(runtime)
    with TestClient(dashboard, base_url="http://127.0.0.1:3100") as ui, TestClient(voice) as phone:
        yield (runtime, ui, phone)


def signature(url, params=None):
    return {"x-twilio-signature": RequestValidator(TOKEN).compute_signature(url, params or {})}


def setup(ws, caller=CALLER):
    ws.send_json({"type": "setup", "from": caller, "callSid": "CAfixture"})


def prompt(ws, text):
    ws.send_json({"type": "prompt", "voicePrompt": text, "last": True})
    reply = ws.receive_json()
    assert reply["type"] == "text" and reply["last"] is True
    return reply["token"]


def test_unsigned_webhook_rejected_and_signed_webhook_returns_twiml(clients):
    _, _, phone = clients
    params = {"From": CALLER}
    assert phone.post("/voice", data=params).status_code == 403
    response = phone.post("/voice", data=params, headers=signature(BASE + "/voice", params))
    assert response.status_code == 200
    assert "your airline recovery assistant" in response.text
    assert "Jeff" in response.text
    assert "How can I help you today?" in response.text
    assert "reservation confirmation code?" not in response.text
    assert "<ConversationRelay" in response.text
    assert "wss://demo.example.test/relay" in response.text


def test_other_caller_rejected(clients):
    _, _, phone = clients
    params = {"From": "+12025550999"}
    response = phone.post("/voice", data=params, headers=signature(BASE + "/voice", params))
    assert "<Hangup" in response.text
    assert "<ConversationRelay" not in response.text


@pytest.mark.parametrize("code,flight", [("XK72LP", "DL2219"), ("BOS842", "DL512")])
def test_websocket_recovery_updates_same_dashboard_state(clients, code, flight):
    runtime, ui, phone = clients
    flight_id, seat = ('F2', '14A') if code == 'XK72LP' else ('F5', '12C')
    runtime['model_request'], _ = fake_model(
        [say('What’s happening with your trip?')],
        [say('I’m sorry. What is your confirmation code?')],
        [call('lookup_reservation', confirmation=code)],
        [say('Your flight was canceled.')],
        [call('search_flights')], [say('There are 2 eligible alternatives.')],
        [call('propose_rebooking', flight_id=flight_id, seat=seat)], [decision()],
    )
    with phone.websocket_connect(
        "/relay", headers=signature("wss://demo.example.test/relay")
    ) as ws:
        setup(ws)
        assert "What’s happening with your trip?" in prompt(ws, "Hi")
        reply = prompt(ws, "My flight was canceled and I’m stranded")
        assert "sorry" in reply and "confirmation code" in reply
        assert ui.get("/api/state").json()["reservation"] is None
        assert "canceled" in prompt(ws, code)
        assert "2 eligible" in prompt(ws, "Find alternatives")
        prompt(ws, flight)
        assert ui.get("/api/state").json()["reservation"]["status"] == "CANCELLED"
        assert ui.post("/api/reset", json={}).status_code == 409
        assert ui.post("/api/turn", json={"text": "confirm rebooking"}).status_code == 409
        assert "Confirmed" in prompt(ws, "confirm rebooking")
        state = ui.get("/api/state").json()
        assert state["reservation"]["flight"] == flight
        assert state["reservation"]["status"] == "CONFIRMED"


def test_voice_preserves_selected_failure_scenario(clients):
    runtime, ui, phone = clients
    runtime['model_request'], _ = fake_model(
        [call('lookup_reservation', confirmation='XK72LP')], [say('I found your reservation.')],
        [call('search_flights')],
    )
    assert ui.post("/api/reset", json={"scenario": "no-seats"}).status_code == 200
    with phone.websocket_connect(
        "/relay", headers=signature("wss://demo.example.test/relay")
    ) as ws:
        setup(ws)
        prompt(ws, "XK72LP")
        prompt(ws, "Find alternatives")
        state = ui.get("/api/state").json()
        assert state["stage"] == "escalated"
        assert state["reservation"]["status"] == "CANCELLED"


def test_interrupted_voice_readback_needs_fresh_consent(clients):
    runtime, ui, phone = clients
    runtime['model_request'], _ = fake_model(
        [call('lookup_reservation', confirmation='XK72LP')], [say('I found your reservation.')],
        [call('search_flights')], [say('Here are the alternatives.')],
        [call('propose_rebooking', flight_id='F2', seat='14A')],
        [call('propose_rebooking', flight_id='F2', seat='14A')], [decision()],
    )
    with phone.websocket_connect(
        "/relay", headers=signature("wss://demo.example.test/relay")
    ) as ws:
        setup(ws)
        prompt(ws, "XK72LP")
        prompt(ws, "Find alternatives")
        prompt(ws, "DL2219")
        ws.send_json({"type": "interrupt", "utteranceUntilInterrupt": "DL2219 leaves"})
        assert "Shall I confirm" in prompt(ws, "confirm rebooking")
        assert ui.get("/api/state").json()["reservation"]["status"] == "CANCELLED"
        prompt(ws, "confirm rebooking")
        assert ui.get("/api/state").json()["reservation"]["status"] == "CONFIRMED"


def test_unsigned_websocket_and_wrong_setup_caller_are_rejected(clients):
    _, _, phone = clients
    with pytest.raises(WebSocketDisconnect):
        with phone.websocket_connect("/relay"):
            pass
    with pytest.raises(WebSocketDisconnect):
        with phone.websocket_connect(
            "/relay", headers=signature("wss://demo.example.test/relay")
        ) as ws:
            setup(ws, "+12025550999")
            ws.receive_json()


def test_foreign_origins_and_tunnel_dashboard_access_rejected(clients):
    _, ui, phone = clients
    assert (
        ui.post("/api/reset", json={}, headers={"origin": "https://evil.example"}).status_code
        == 403
    )
    assert ui.get("/api/state", headers={"host": "public.example"}).status_code == 403
    assert phone.get("/api/state").status_code == 404
    assert phone.post("/api/reset", json={}).status_code == 404
    assert phone.get("/").status_code == 404


def test_input_validation_and_secret_redaction(clients):
    _, ui, _ = clients
    assert ui.post("/api/turn", json={"text": 123}).status_code == 422
    assert ui.post("/api/turn", json={"text": "   "}).status_code == 400
    assert ui.post("/api/reset", json={"scenario": "unknown"}).status_code == 422
    assert TOKEN not in ui.get("/api/state").text
    assert CALLER not in ui.get("/api/state").text


def test_dashboard_build_is_served(clients):
    _, ui, _ = clients
    response = ui.get("/")
    assert response.status_code == 200
    assert "A way home, tonight." in response.text
    assert "/assets/" in response.text
