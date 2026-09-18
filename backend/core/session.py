"""Plain dictionaries for one call, its working data, and its display state."""

from copy import deepcopy
from datetime import datetime, timezone
from pathlib import Path
from uuid import uuid4
import json

SCENARIOS = ("normal", "no-seats", "sold-out", "api-error")


def load_data():
    data = json.loads((Path(__file__).resolve().parent.parent / "data/demo.json").read_text())
    # Fail early on broken references instead of selecting the wrong passenger/flight.
    for collection in ("passengers", "reservations", "flights"):
        ids = [row["id"] for row in data[collection]]
        if len(ids) != len(set(ids)):
            raise ValueError(f"Duplicate IDs in {collection}")
    codes = [r["confirmation"].upper() for r in data["reservations"]]
    if len(codes) != len(set(codes)):
        raise ValueError("Duplicate confirmation codes")
    for r in data["reservations"]:
        get_record(data, "passengers", r["passenger_id"])
        get_record(data, "flights", r["flight_id"])
    return data


def get_record(data, collection, record_id):
    return next(row for row in data[collection] if row["id"] == record_id)


def create_session(scenario="normal", demo_data=None):
    if scenario not in SCENARIOS:
        raise ValueError("Invalid scenario")
    return {
        "id": str(uuid4()),
        "scenario": scenario,
        "data": deepcopy(demo_data if demo_data is not None else load_data()),
        "stage": "identify",
        "reservation_id": None,
        "revision": 0,
        "options": [],
        "pending": None,
        "approval_id": None,
        "preferences": {"arrival_before": None, "seat_type": "any"},
        "events": [],
        "transcript": [],
        "handoff": None,
    }


def record_event(session, name, detail):
    session["events"].append(
        {"name": name, "detail": detail, "time": datetime.now(timezone.utc).isoformat()}
    )


def invalidate_proposal(session):
    session["pending"] = None
    session["approval_id"] = None


def finish_turn(session, reply):
    session["transcript"].append({"role": "assistant", "text": reply})
    return reply


def interrupt_session(session, heard=""):
    session["approval_id"] = None
    if session["transcript"] and session["transcript"][-1]["role"] == "assistant":
        session["transcript"][-1]["text"] = (
            f"[Interrupted; passenger heard only: {heard}]"
            if heard
            else "[Response interrupted before completion]"
        )
    record_event(session, "interrupt", "Approval cleared; a fresh readback is required")


def active_records(session):
    if session["reservation_id"] is None:
        return None, None, None
    data = session["data"]
    reservation = get_record(data, "reservations", session["reservation_id"])
    return (
        reservation,
        get_record(data, "flights", reservation["flight_id"]),
        get_record(data, "passengers", reservation["passenger_id"]),
    )


def flight_view(session, flight, seat=None):
    airports = session["data"]["airports"]
    departure = datetime.fromisoformat(flight["departure_at"])
    arrival = datetime.fromisoformat(flight["arrival_at"])
    return {
        "flight_id": flight["id"],
        "flight": flight["number"],
        "origin": flight["origin"],
        "destination": flight["destination"],
        "departure_at": flight["departure_at"],
        "arrival_at": flight["arrival_at"],
        "departure": departure.strftime("%I:%M %p").lstrip("0"),
        "arrival": arrival.strftime("%I:%M %p").lstrip("0"),
        "seat": seat,
        "origin_name": airports[flight["origin"]]["name"],
        "destination_name": airports[flight["destination"]]["name"],
    }


def session_view(session):
    reservation, flight, passenger = active_records(session)
    view = {
        "id": session["id"],
        "stage": session["stage"],
        "scenario": session["scenario"],
        "customer": None,
        "reservation": None,
        "trip": None,
        "disruption": None,
        "options": session["options"],
        "pending": session["pending"],
        "awaitingApproval": bool(session["approval_id"]),
        "preferences": session["preferences"],
        "events": session["events"],
        "transcript": session["transcript"],
        "handoff": session["handoff"],
    }
    if reservation:
        view["customer"] = {**passenger, "confirmation": reservation["confirmation"]}
        view["reservation"] = {
            **flight_view(session, flight, reservation["seat"]),
            "id": reservation["id"],
            "status": "CONFIRMED" if reservation["status"] == "CONFIRMED" else flight["status"],
        }
        airports = session["data"]["airports"]
        view["trip"] = {
            "origin_name": airports[flight["origin"]]["name"],
            "destination_name": airports[flight["destination"]]["name"],
            "travel_date": flight["departure_at"][:10],
            "timezone": "airport local time",
        }
        view["disruption"] = next(
            (d for d in session["data"]["disruptions"] if d["flight_id"] == flight["id"]), None
        )
    return deepcopy(view)


class BusyError(ValueError):
    """An active call or turn owns the shared demo state."""


def create_runtime(settings, model_request=None):
    return {
        "settings": settings,
        "model_request": model_request,
        "session": create_session(),
        "busy": False,
        "active_call": False,
    }


def get_state(runtime):
    session = runtime["session"]
    return {
        **session_view(session),
        "busy": runtime["busy"],
        "activeCall": runtime["active_call"],
        "mode": "LLM · " + runtime["settings"].openai_model,
        "voiceConfigured": runtime["settings"].voice_ready,
        "demoReservations": [
            {
                "confirmation": r["confirmation"],
                "label": f"{r['confirmation']} · {get_record(session['data'], 'flights', r['flight_id'])['number']} · {get_record(session['data'], 'flights', r['flight_id'])['origin']} → {get_record(session['data'], 'flights', r['flight_id'])['destination']}",
            }
            for r in session["data"]["reservations"]
        ],
    }


def reset_session(runtime, scenario="normal"):
    if runtime["busy"] or runtime["active_call"]:
        raise BusyError("Finish the active call or turn before resetting")
    runtime["session"] = create_session(scenario)
