"""Load settings, expose FastAPI routes, and run the dashboard and voice listeners."""

import asyncio
import os
import signal
from contextlib import contextmanager
from dataclasses import dataclass, field
from pathlib import Path
from typing import Literal
from urllib.parse import urlparse

import uvicorn
from dotenv import load_dotenv
from fastapi import FastAPI, Request
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from core.session import BusyError, create_runtime, get_state, reset_session
from core.loop import process_turn
from voice import create_voice_router

ROOT = Path(__file__).resolve().parent
FRONTEND_DIST = ROOT.parent / "frontend" / "dist"


@dataclass(frozen=True)
class Settings:
    port: int = 3100
    voice_port: int = 3101
    public_base_url: str = ""
    twilio_auth_token: str = field(default="", repr=False)
    demo_caller_number: str = field(default="", repr=False)
    openai_api_key: str = field(default="", repr=False)
    openai_model: str = ""

    @property
    def voice_ready(self) -> bool:
        url = urlparse(self.public_base_url)
        return bool(
            url.scheme == "https"
            and url.netloc
            and self.twilio_auth_token
            and self.demo_caller_number
        )

    @classmethod
    def from_env(cls):
        load_dotenv(ROOT / ".env")
        return cls(
            port=int(os.getenv("PORT", "3100")),
            voice_port=int(os.getenv("VOICE_PORT", "3101")),
            public_base_url=os.getenv("PUBLIC_BASE_URL", "").rstrip("/"),
            twilio_auth_token=os.getenv("TWILIO_AUTH_TOKEN", ""),
            demo_caller_number=os.getenv("DEMO_CALLER_NUMBER", ""),
            openai_api_key=os.getenv("OPENAI_API_KEY", ""),
            openai_model=os.getenv("OPENAI_MODEL", ""),
        )


class Message(BaseModel):
    text: str = Field(min_length=1, max_length=1500)


class Reset(BaseModel):
    scenario: Literal["normal", "no-seats", "sold-out", "api-error"] = "normal"


def create_apps(runtime: dict, dist: Path | None = None):
    dashboard = FastAPI(
        title="JEFF recovery dashboard", docs_url=None, redoc_url=None, openapi_url=None
    )
    voice = FastAPI(title="Twilio voice adapter", docs_url=None, redoc_url=None, openapi_url=None)
    voice.include_router(create_voice_router(runtime))
    allowed_hosts = {
        f"127.0.0.1:{runtime['settings'].port}",
        f"localhost:{runtime['settings'].port}",
    }
    allowed_origins = {f"http://{host}" for host in allowed_hosts} | {
        "http://127.0.0.1:5173",
        "http://localhost:5173",
    }

    @dashboard.middleware("http")
    async def local_dashboard_only(request: Request, call_next):
        if request.headers.get("host") not in allowed_hosts:
            return JSONResponse({"error": "Local dashboard only"}, status_code=403)
        if request.headers.get("origin") and request.headers["origin"] not in allowed_origins:
            return JSONResponse({"error": "Invalid origin"}, status_code=403)
        response = await call_next(request)
        response.headers["Cache-Control"] = "no-store"
        return response

    @dashboard.exception_handler(BusyError)
    async def busy_error(request, error):
        return JSONResponse({"error": str(error)}, status_code=409)

    @dashboard.exception_handler(ValueError)
    async def input_error(request, error):
        return JSONResponse({"error": str(error)}, status_code=400)

    @dashboard.get("/api/state")
    async def state():
        return get_state(runtime)

    @dashboard.post("/api/turn")
    async def turn(message: Message):
        await process_turn(runtime, message.text)
        return get_state(runtime)

    @dashboard.post("/api/reset")
    async def reset(payload: Reset):
        reset_session(runtime, payload.scenario)
        return get_state(runtime)

    build = dist or FRONTEND_DIST

    @dashboard.get("/")
    async def index():
        if not (build / "index.html").is_file():
            return JSONResponse(
                {"error": "Build the dashboard first: npm --prefix ../frontend run build"},
                status_code=503,
            )
        return FileResponse(build / "index.html")

    if (build / "assets").is_dir():
        dashboard.mount("/assets", StaticFiles(directory=build / "assets"), name="assets")
    return dashboard, voice


class LocalServer(uvicorn.Server):
    # The launcher coordinates shutdown for both listeners.
    @contextmanager
    def capture_signals(self):
        yield


async def main():
    settings = Settings.from_env()
    if not settings.openai_api_key or not settings.openai_model:
        raise SystemExit("Set OPENAI_API_KEY and OPENAI_MODEL in backend/.env")
    if settings.port == settings.voice_port:
        raise SystemExit("PORT and VOICE_PORT must differ")
    if not (FRONTEND_DIST / "index.html").exists():
        raise SystemExit(
            "Build the dashboard first: npm --prefix ../frontend ci && npm --prefix ../frontend run build"
        )
    runtime = create_runtime(settings)
    dashboard, voice = create_apps(runtime)
    servers = [
        LocalServer(
            uvicorn.Config(
                app,
                host="127.0.0.1",
                port=port,
                access_log=False,
                ws_max_size=16_000,
                timeout_graceful_shutdown=5,
            )
        )
        for app, port in [(dashboard, settings.port), (voice, settings.voice_port)]
    ]

    def stop():
        for server in servers:
            server.should_exit = True

    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        loop.add_signal_handler(sig, stop)
    print(f"Dashboard: http://127.0.0.1:{settings.port}", flush=True)
    print(
        f"Voice adapter: http://127.0.0.1:{settings.voice_port} (tunnel this port only)", flush=True
    )
    tasks = [asyncio.create_task(server.serve()) for server in servers]
    try:
        await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
    finally:
        stop()
        await asyncio.gather(*tasks)


if __name__ == "__main__":
    asyncio.run(main())
