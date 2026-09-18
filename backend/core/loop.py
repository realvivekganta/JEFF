"""Run one voice/text turn: policy checks, context, model, tool execution, response."""

import asyncio
import json
import re
import httpx
from .context import build_context, build_approval_context, model_tools
from .guardrails import BookingError, require_recoverable
from .session import BusyError, finish_turn, record_event, session_view
from . import tools


async def process_turn(runtime, text, *, from_voice=False):
    # Claim state before awaiting: the phone and dashboard cannot commit concurrently.
    if runtime["busy"] or (runtime["active_call"] and not from_voice):
        raise BusyError("A phone call or conversation turn is already in progress")
    runtime["busy"] = True
    try:
        return await handle_turn(
            runtime["session"], text, runtime["settings"], runtime["model_request"]
        )
    finally:
        runtime["busy"] = False


async def request_model(settings, payload):
    if not settings.openai_api_key or not settings.openai_model:
        raise ValueError("Missing model configuration")
    async with httpx.AsyncClient(timeout=25) as client:
        response = await client.post(
            "https://api.openai.com/v1/responses",
            json=payload,
            headers={"Authorization": f"Bearer {settings.openai_api_key}"},
        )
        if not response.is_success:
            # Do not log provider response bodies or credentials.
            raise ValueError(f"Model request failed ({response.status_code})")
        return response.json()


async def interpret_approval(session, settings, model_request=None):
    payload = build_approval_context(session, settings)
    response = await model_request(payload) if model_request else await request_model(settings, payload)
    if response.get("status") != "completed":
        raise ValueError("Incomplete approval interpretation")
    text = " ".join(
        part["text"]
        for item in response.get("output", []) if item.get("type") == "message"
        for part in item.get("content", []) if part.get("type") == "output_text"
    )
    result = json.loads(text)
    if not isinstance(result, dict) or set(result) != {"decision"} or result["decision"] not in ("approve", "not_approve"):
        raise ValueError("Invalid approval interpretation")
    return result["decision"] == "approve"


async def handle_turn(session, text, settings, model_request=None):
    text = text.strip()
    if not text or len(text) > 1500:
        raise ValueError("Enter 1–1500 characters")
    session["transcript"].append({"role": "user", "text": text})
    try:
        deadline = asyncio.get_running_loop().time() + 25
        approval_id = session["approval_id"]
        approved = False
        if session["stage"] == "confirm" and approval_id:
            async with asyncio.timeout_at(deadline):
                approved = await interpret_approval(session, settings, model_request)
        if approved and session["approval_id"] == approval_id:
            reservation = tools.rebook_flight(session, approval_id)
            return finish_turn(
                session,
                f"Confirmed. You’re on {reservation['flight']} on {reservation['departure_at'][:10]}, "
                f"departing {reservation['origin_name']} at {reservation['departure']} and arriving in "
                f"{reservation['destination_name']} at {reservation['arrival']}, seat {reservation['seat']}. "
                "Your updated demo itinerary is on screen.",
            )
        # Every non-approval utterance invalidates permission to commit the pending proposal.
        session["approval_id"] = None
        async with asyncio.timeout_at(deadline):
            return finish_turn(session, await run_model_loop(session, settings, model_request))
    except asyncio.CancelledError:
        session["approval_id"] = None
        raise
    except BookingError as error:
        return finish_turn(session, tools.request_human(session, str(error)))
    except Exception as error:
        record_event(session, "failure", type(error).__name__)
        return finish_turn(
            session,
            tools.request_human(session, "The recovery service could not complete this step"),
        )


async def run_model_loop(session, settings, model_request=None):
    turn_items = []
    for _ in range(6):
        payload = build_context(session, settings, turn_items)
        response = (
            await model_request(payload)
            if model_request
            else await request_model(settings, payload)
        )
        if response.get("status") in ("incomplete", "failed"):
            raise ValueError("Incomplete model response")
        output = response.get("output", [])
        calls = [item for item in output if item.get("type") == "function_call"]
        if not calls:
            reply = " ".join(
                part["text"]
                for item in output
                if item.get("type") == "message"
                for part in item.get("content", [])
                if part.get("type") == "output_text"
            ).strip()
            if not reply:
                raise ValueError("Empty model response")
            if session["stage"] != "resolved" and re.search(r"\b(confirmed|rebooked|booked|updated|all set)\b", reply, re.I):
                return "I haven’t changed your reservation yet. Would you like to go ahead with the flight and seat we discussed?"
            return reply
        if len(calls) != 1:
            raise ValueError("Expected one tool action at a time")
        call = calls[0]
        spec = next((t for t in model_tools(session) if t["name"] == call["name"]), None)
        args = json.loads(call["arguments"])
        if (
            spec is None
            or not isinstance(args, dict)
            or set(args) != set(spec["parameters"]["properties"])
            or any(not isinstance(v, str) for v in args.values())
        ):
            raise ValueError("Invalid tool action")
        if call["name"] == "request_human":
            return tools.request_human(session, "This request needs human review")
        if call["name"] == "propose_rebooking":
            return tools.propose_rebooking(session, **args)
        action = {
            "lookup_reservation": tools.lookup_reservation,
            "get_reservation": tools.get_reservation,
            "update_preferences": tools.update_preferences,
            "search_flights": tools.search_flights,
        }[call["name"]]
        result = action(session, **args)
        if call["name"] == "lookup_reservation":
            turn_items = []
            if result["found"]:
                require_recoverable(session)
        if call["name"] == "search_flights" and not result:
            return tools.request_human(
                session, "No eligible alternatives meet the current recovery rules and preferences"
            )
        turn_items.extend(output)
        turn_items.append(
            {
                "type": "function_call_output",
                "call_id": call["call_id"],
                "output": json.dumps(result),
            }
        )
    raise ValueError("Tool step limit reached")
