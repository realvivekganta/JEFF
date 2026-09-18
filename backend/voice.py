"""Twilio adapter: signed HTTP webhook + ConversationRelay WebSocket messages."""

import asyncio
import json
from contextlib import suppress
from urllib.parse import parse_qsl
from fastapi import APIRouter, Request, WebSocket, WebSocketDisconnect
from fastapi.responses import Response
from starlette.datastructures import FormData
from twilio.request_validator import RequestValidator
from twilio.twiml.voice_response import VoiceResponse, Connect
from core.session import create_session, interrupt_session, record_event
from core.loop import process_turn

MAX_MESSAGE_BYTES = 16000
WELCOME_GREETING = "Hi, I’m Jeff, your airline recovery assistant. How can I help you today?"


def create_voice_router(runtime: dict) -> APIRouter:
    router = APIRouter()
    settings = runtime["settings"]

    def valid(signature: str, url: str, params=None) -> bool:
        return bool(
            settings.twilio_auth_token
            and RequestValidator(settings.twilio_auth_token).validate(url, params or {}, signature)
        )

    @router.post("/voice")
    async def incoming_call(request: Request):
        if not settings.voice_ready:
            return Response("Configure the voice settings in .env", status_code=503)
        body = bytearray()
        async for chunk in request.stream():
            body.extend(chunk)
            if len(body) > MAX_MESSAGE_BYTES:
                return Response(status_code=413)
        try:
            params = FormData(parse_qsl(body.decode("utf-8"), keep_blank_values=True))
        except UnicodeDecodeError:
            return Response(status_code=400)
        if not valid(
            request.headers.get("x-twilio-signature", ""),
            settings.public_base_url + "/voice",
            params,
        ):
            return Response(status_code=403)
        response = VoiceResponse()
        caller = params.get("From", "")
        if caller != settings.demo_caller_number or runtime["active_call"] or runtime["busy"]:
            response.say(
                "This voice demo is unavailable for this caller or another call is active."
            )
            response.hangup()
        else:
            connect = Connect()
            connect.conversation_relay(
                url=settings.public_base_url.replace("https:", "wss:", 1) + "/relay",
                welcome_greeting=WELCOME_GREETING,
            )
            response.append(connect)
            response.say("The demo session has ended. No live agent transfer is configured.")
        return Response(str(response), media_type="text/xml")

    @router.websocket("/relay")
    async def conversation_relay(ws: WebSocket):
        signature_url = settings.public_base_url.replace("https:", "wss:", 1) + "/relay"
        if (
            not settings.voice_ready
            or ws.url.query
            or runtime["active_call"]
            or runtime["busy"]
            or (not valid(ws.headers.get("x-twilio-signature", ""), signature_url))
        ):
            await ws.close(code=1008)
            return
        # Claim the single call slot synchronously, before accepting the socket.
        runtime["active_call"] = True
        initialized = False
        worker = None
        current_turn = None
        generation = 0
        queue = asyncio.Queue(maxsize=8)
        partial = ""

        async def process_turns():
            nonlocal current_turn
            while True:
                revision, text = await queue.get()
                try:
                    if revision != generation:
                        continue
                    current_turn = asyncio.create_task(process_turn(runtime, text, from_voice=True))
                    try:
                        reply = await current_turn
                    except asyncio.CancelledError:
                        if asyncio.current_task().cancelling():
                            raise
                        continue
                    except Exception:
                        reply = "I could not complete this step. Please restart the demo."
                    if revision == generation:
                        await ws.send_json({"type": "text", "token": reply, "last": True})
                finally:
                    current_turn = None
                    queue.task_done()

        try:
            await ws.accept()
            worker = asyncio.create_task(process_turns())
            while True:
                raw = await asyncio.wait_for(ws.receive_text(), timeout=300 if initialized else 10)
                if len(raw.encode()) > MAX_MESSAGE_BYTES:
                    await ws.close(code=1009)
                    break
                message = json.loads(raw)
                if not isinstance(message, dict):
                    raise ValueError("Invalid message")
                kind = message.get("type")
                if kind == "setup":
                    if initialized or message.get("from") != settings.demo_caller_number:
                        await ws.close(code=1008)
                        break
                    runtime["session"] = create_session(scenario=runtime["session"]["scenario"])
                    runtime["session"]["transcript"].append(
                        {"role": "assistant", "text": WELCOME_GREETING}
                    )
                    record_event(
                        runtime["session"],
                        "voice",
                        "ConversationRelay connected; caller allowlist is not identity authentication",
                    )
                    initialized = True
                    continue
                if not initialized:
                    await ws.close(code=1008)
                    break
                if kind == "interrupt":
                    generation += 1
                    partial = ""
                    if current_turn:
                        current_turn.cancel()
                    interrupt_session(
                        runtime["session"], str(message.get("utteranceUntilInterrupt", ""))
                    )
                elif kind == "prompt":
                    fragment = message.get("voicePrompt", "")
                    if not isinstance(fragment, str):
                        raise ValueError("Invalid prompt")
                    partial += fragment + " "
                    if len(partial.strip()) > 1500:
                        await ws.close(code=1009)
                        break
                    if message.get("last") is True:
                        queue.put_nowait((generation, partial.strip()))
                        partial = ""
                elif kind == "error":
                    record_event(
                        runtime["session"],
                        "voice_error",
                        "ConversationRelay reported a speech error",
                    )
        except WebSocketDisconnect:
            pass
        except (ValueError, asyncio.TimeoutError, asyncio.QueueFull):
            with suppress(RuntimeError, WebSocketDisconnect):
                await ws.close(code=1008)
        finally:
            generation += 1
            if current_turn:
                current_turn.cancel()
            if worker:
                worker.cancel()
                with suppress(asyncio.CancelledError, RuntimeError, WebSocketDisconnect):
                    await worker
            runtime["session"]["approval_id"] = None
            runtime["active_call"] = False
            if initialized:
                record_event(runtime["session"], "voice", "Call disconnected")

    return router
