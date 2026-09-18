"""Tool and action guardrails: eligibility, availability, and exact-proposal consent."""

from datetime import datetime, timedelta
from zoneinfo import ZoneInfo
from .session import active_records, get_record


class BookingError(ValueError):
    """An unsupported request or stale booking proposal."""


def timestamp(value):
    result = datetime.fromisoformat(value)
    if result.tzinfo is None:
        raise BookingError("A timezone is required for dates and times")
    return result


def require_recoverable(session):
    reservation, flight, _ = active_records(session)
    if not reservation:
        raise BookingError("Find the reservation first")
    if reservation["status"] != "DISRUPTED" or flight["status"] != "CANCELLED":
        raise BookingError(
            "This demo automates canceled flights only; this reservation needs human review"
        )
    if reservation.get("segments"):
        raise BookingError("Multi-leg reservations need human review")
    disruption = next(
        (d for d in session["data"]["disruptions"] if d["flight_id"] == flight["id"]), None
    )
    if not disruption or disruption["type"] != "cancellation":
        raise BookingError("The cancellation could not be verified")
    return reservation, flight


def eligible_seats(session, candidate):
    _, original = require_recoverable(session)
    data = session["data"]
    if (
        candidate["id"] == original["id"]
        or candidate["status"] != "SCHEDULED"
        or (candidate["origin"], candidate["destination"])
        != (original["origin"], original["destination"])
    ):
        return []
    departure, arrival = timestamp(candidate["departure_at"]), timestamp(candidate["arrival_at"])
    zone = ZoneInfo(data["airports"][original["origin"]]["timezone"])
    if (
        data["policy"]["same_day_only"]
        and departure.astimezone(zone).date()
        != timestamp(original["departure_at"]).astimezone(zone).date()
    ):
        return []
    if (
        departure
        < timestamp(data["demo_now"]) + timedelta(minutes=data["policy"]["minimum_lead_minutes"])
        or arrival <= departure
    ):
        return []
    deadline = session["preferences"]["arrival_before"]
    if deadline and arrival > timestamp(deadline):
        return []
    preference = session["preferences"]["seat_type"]
    return [
        seat for seat in candidate["seats"] if preference == "any" or seat["type"] == preference
    ]


def validate_commit(session, proposal_id):
    pending = session["pending"]
    if (
        session["stage"] != "confirm"
        or not pending
        or not proposal_id
        or session["approval_id"] != proposal_id
        or pending["proposal_id"] != proposal_id
        or pending["reservation_id"] != session["reservation_id"]
        or pending["revision"] != session["revision"]
    ):
        raise BookingError("Explicit approval of the current proposal is required")
    candidate = get_record(session["data"], "flights", pending["flight_id"])
    seats = eligible_seats(session, candidate)
    if session["scenario"] == "sold-out" or not any(s["number"] == pending["seat"] for s in seats):
        raise BookingError("Selected seat is no longer available")
    if (
        candidate["number"] != pending["flight"]
        or session["data"]["policy"]["additional_fare"] != pending["additional_fare"]
    ):
        raise BookingError("The flight details or fare changed before I could book this option")
    if any(
        candidate[key] != pending[key]
        for key in ("departure_at", "arrival_at", "origin", "destination")
    ):
        raise BookingError("The flight changed; a new proposal is required")
    return candidate
