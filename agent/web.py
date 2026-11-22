"""Minimal web chat for the AI Tuition Assistant (UI appearance is not graded).

    uvicorn web:app --port 8080     ->  http://localhost:8080
"""
from __future__ import annotations

import secrets
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from fastapi import FastAPI, HTTPException  # noqa: E402
from fastapi.responses import FileResponse  # noqa: E402
from pydantic import BaseModel  # noqa: E402

from agent import Session, TuitionAgent  # noqa: E402
from clients import GatewayClient, MCPPolicyClient  # noqa: E402
from config import settings  # noqa: E402
from llm import OpenAICompatibleLLM  # noqa: E402

STATIC = Path(__file__).resolve().parent / "static"

app = FastAPI(title="AI Tuition Assistant - chat", docs_url=None, redoc_url=None)
gateway = GatewayClient(settings.gateway_url)
_agent: TuitionAgent | None = None
SESSIONS: dict[str, Session] = {}  # in-memory, single replica (documented in README)


def get_agent() -> TuitionAgent:
    global _agent
    if _agent is None:
        llm = OpenAICompatibleLLM(settings.llm_base_url, settings.llm_api_key, settings.llm_model)
        _agent = TuitionAgent(llm, gateway, MCPPolicyClient(settings.mcp_url),
                              settings.max_tool_rounds, settings.trace_log, settings.timezone)
    return _agent


class LoginBody(BaseModel):
    username: str
    password: str


class ChatBody(BaseModel):
    session_id: str
    message: str


@app.get("/")
def index():
    return FileResponse(STATIC / "index.html")


@app.get("/health")
def health():
    return {"status": "ok", "gateway": settings.gateway_url, "mcp": settings.mcp_url, "model": settings.llm_model}


@app.post("/chat/login")
def login(body: LoginBody):
    res = gateway.login(body.username, body.password)
    if not res.ok:
        raise HTTPException(res.status, res.body.get("error", {}).get("message", "Login failed"))
    if res.body.get("role") != "student":
        raise HTTPException(403, "The assistant is for students only")
    token, sno = res.body["accessToken"], res.body["studentNo"]
    profile = gateway.get_tuition(token, sno)
    name = profile.body.get("fullName", "") if profile.ok else ""
    sid = secrets.token_urlsafe(24)
    SESSIONS[sid] = Session(username=body.username, student_no=sno, full_name=name, token=token)
    return {"session_id": sid, "student_no": sno, "full_name": name}


@app.post("/chat/message")
def chat(body: ChatBody):
    session = SESSIONS.get(body.session_id)
    if session is None:
        raise HTTPException(401, "Session expired, please log in again")
    try:
        return get_agent().handle(session, body.message)
    except RuntimeError as e:  # e.g. missing LLM_API_KEY
        raise HTTPException(500, str(e))
    except Exception as e:  # noqa: BLE001 - LLM provider errors (quota, auth, network)
        raise HTTPException(502, f"LLM provider error: {type(e).__name__}: {str(e)[:300]}")


@app.post("/chat/logout")
def logout(body: dict):
    SESSIONS.pop(body.get("session_id", ""), None)
    return {"ok": True}
