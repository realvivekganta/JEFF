"""Reservation lookup and recovery operations over the session's mock database."""

from copy import deepcopy
from datetime import datetime
from uuid import uuid4
from zoneinfo import ZoneInfo
import re
from .guardrails import BookingError, eligible_seats, require_recoverable, timestamp, validate_commit
from .session import (
    active_records,
    flight_view,
    get_record,
    invalidate_proposal,
    record_event,
    session_view,
)


def normalized_code(text):
    return re.sub(r"[^A-Z0-9]", "", text.upper())


def lookup_reservation(session, confirmation):
    code = normalized_code(confirmation)
    # The model may only look up a code actually supplied in the current utterance.
    utterance = session["transcript"][-1]["text"] if session["transcript"] else ""
    if not code or code not in normalized_code(utterance):
        raise BookingError("Ask the caller for their confirmation code")
    reservation = next(
        (r for r in session["data"]["reservations"] if r["confirmation"].upper() == code), None
    )
    previous_reservation_id = session["reservation_id"]
    invalidate_proposal(session)
    session["reservation_id"] = None
    session["options"] = []
    session["preferences"] = {"arrival_before": None, "seat_type": "any"}
    session["revision"] += 1
    # Do not carry another passenger's conversation or tool results into new context.
    if previous_reservation_id and (
        not reservation or reservation["id"] != previous_reservation_id
    ):
        session["transcript"] = session["transcript"][-1:]
    session["events"] = []
    session["handoff"] = None
    if not reservation:
        session["stage"] = "identify"
        record_event(session, "lookup_reservation", "Confirmation not found")
        return {
            "found": False,
            "message": "That confirmation was not found. Please check the code.",
        }
    session["reservation_id"] = reservation["id"]
    session["stage"] = "start"
    record_event(
        session, "lookup_reservation", f"Loaded fictional reservation {reservation['confirmation']}"
    )
    return {"found": True, **get_reservation(session)}


def get_reservation(session):
    if session["reservation_id"] is None:
        raise BookingError("Find the reservation first")
    view = session_view(session)
    return {key: view[key] for key in ("customer", "reservation", "trip", "disruption")}


def update_preferences(session, seat_type, arrival_before):
    """Validate and store the caller's preferences interpreted by the model."""
    _, flight, _ = active_records(session)
    if not flight:
        raise BookingError("Find the reservation first")
    if seat_type not in ("unchanged", "any", "window", "aisle"):
        raise BookingError("Unsupported seat preference")
    preferences = deepcopy(session["preferences"])
    if seat_type != "unchanged":
        preferences["seat_type"] = seat_type
    if arrival_before == "none":
        preferences["arrival_before"] = None
    elif arrival_before != "unchanged":
        if not re.fullmatch(r"(?:[01]\d|2[0-3]):[0-5]\d", arrival_before):
            raise BookingError("An arrival deadline must be an unambiguous local time")
        hour, minute = map(int, arrival_before.split(":"))
        zone = ZoneInfo(session["data"]["airports"][flight["destination"]]["timezone"])
        day = timestamp(flight["arrival_at"]).astimezone(zone).date()
        preferences["arrival_before"] = datetime(
            day.year, day.month, day.day, hour, minute, tzinfo=zone
        ).isoformat()
    changed = preferences != session["preferences"]
    if changed:
        session["preferences"] = preferences
        session["revision"] += 1
        invalidate_proposal(session)
        session["options"] = []
        session["stage"] = "start"
        record_event(
            session,
            "preferences",
            "Updated explicit arrival/seat preferences; previous proposal cleared",
        )
    return {"changed": changed, **deepcopy(preferences)}


def search_flights(session):
    require_recoverable(session)
    if session["scenario"] == "api-error":
        raise BookingError("Reservation service unavailable")
    invalidate_proposal(session)
    choices = []
    if session["scenario"] != "no-seats":
        for flight in session["data"]["flights"]:
            seats = eligible_seats(session, flight)
            if seats:
                choices.append({**flight_view(session, flight), "seats": deepcopy(seats)})
    choices.sort(key=lambda f: timestamp(f["departure_at"]))
    session["options"] = choices
    session["stage"] = "options"
    record_event(
        session,
        "search_flights",
        f"{len(choices)} eligible alternatives for this reservation and its preferences",
    )
    return deepcopy(choices)


def propose_rebooking(session, flight_id, seat):
    if session["stage"] not in ("options", "confirm"):
        raise BookingError("Search required before selection")
    offered = next((f for f in session["options"] if f["flight_id"] == flight_id), None)
    if not offered or not any(s["number"] == seat for s in offered["seats"]):
        raise BookingError("Flight or seat was not offered")
    flight = get_record(session["data"], "flights", flight_id)
    if not any(s["number"] == seat for s in eligible_seats(session, flight)):
        raise BookingError("Selected seat is no longer available")
    pending = {
        **flight_view(session, flight, seat),
        "reservation_id": session["reservation_id"],
        "revision": session["revision"],
        "proposal_id": str(uuid4()),
        "additional_fare": session["data"]["policy"]["additional_fare"],
    }
    session["pending"] = pending
    session["approval_id"] = pending["proposal_id"]
    session["stage"] = "confirm"
    record_event(session, "proposal", f"{pending['flight']} seat {seat}; awaiting approval")
    fare = session["data"]["policy"]["additional_fare"]
    return (
        f"{pending['flight']} leaves {pending['origin_name']} on {pending['departure_at'][:10]} at {pending['departure']} "
        f"and arrives in {pending['destination_name']} at {pending['arrival']}, seat {seat}. "
        f"Times are local to each airport. The additional fare is {fare} dollars in this demo. Shall I confirm this change?"
    )


def rebook_flight(session, proposal_id):
    """Commit exactly one current proposal; never exposed as an LLM tool."""
    candidate = validate_commit(session, proposal_id)
    pending = session["pending"]
    reservation, _, _ = active_records(session)
    candidate["seats"] = [seat for seat in candidate["seats"] if seat["number"] != pending["seat"]]
    reservation.update(flight_id=candidate["id"], seat=pending["seat"], status="CONFIRMED")
    session["revision"] += 1
    session["stage"] = "resolved"
    invalidate_proposal(session)
    session["options"] = []
    record_event(
        session, "rebook_flight", f"{candidate['number']} confirmed; seat {reservation['seat']}"
    )
    return session_view(session)["reservation"]


def request_human(session, reason):
    invalidate_proposal(session)
    session["stage"] = "escalated"
    session["handoff"] = {
        "reason": reason,
        "reservation_id": session["reservation_id"],
        "preferences": deepcopy(session["preferences"]),
        "recent_conversation": deepcopy(session["transcript"][-8:]),
    }
    record_event(session, "human_review", reason)
    return f"{reason}. Your reservation has not changed. I have recorded a simulated human handoff; this demo does not transfer the call."
