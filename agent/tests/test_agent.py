"""Agent tests -- no live LLM, no cloud.

* The LLM is replaced by ScriptedLLM (it returns pre-defined tool calls, like a real model would).
* The REST side is the REAL Tuition API running in-process (FastAPI TestClient + SQLite).
* The MCP side is the REAL MCP server called through the MCP protocol (in-memory transport).
So these tests check the agent's orchestration: tool execution, results fed back to the LLM,
identity enforcement and the payment confirmation gate.
"""
import json
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
os.environ["INIT_DB"] = "false"
os.environ["PASSWORD_HASH_ROUNDS"] = "1000"
sys.path[:0] = [str(ROOT / "agent"), str(ROOT / "tuition_api"), str(ROOT / "mcp_server")]

import pytest  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402
from sqlalchemy import create_engine  # noqa: E402
from sqlalchemy.orm import sessionmaker  # noqa: E402
from sqlalchemy.pool import StaticPool  # noqa: E402

import server as mcp_server  # noqa: E402
from agent import CONFIRM_RE, Session, TuitionAgent  # noqa: E402
from app.db import Base, get_db  # noqa: E402
from app.main import create_app  # noqa: E402
from app.seed import seed  # noqa: E402
from clients import GatewayClient, MCPPolicyClient  # noqa: E402
from llm import LLMReply, ScriptedLLM, ToolCall  # noqa: E402


@pytest.fixture
def api():
    eng = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    Base.metadata.create_all(eng)
    factory = sessionmaker(bind=eng, autoflush=False, expire_on_commit=False)
    with factory() as s:
        seed(s)
    app = create_app()

    def _db():
        s = factory()
        try:
            yield s
        finally:
            s.close()

    app.dependency_overrides[get_db] = _db
    with TestClient(app) as c:
        yield c


@pytest.fixture
def gateway(api):
    return GatewayClient("http://testserver", http=api)


@pytest.fixture
def session(gateway):
    r = gateway.login("20230001", "Student123!")
    return Session("20230001", "20230001", "Ayse Yilmaz", r.body["accessToken"])


def make_agent(script, gateway):
    llm = ScriptedLLM(script)
    return TuitionAgent(llm, gateway, MCPPolicyClient(mcp_server.mcp), trace_log=None), llm


def tool_msgs(messages):
    return [json.loads(m["content"]) for m in messages if m.get("role") == "tool"]


def balance(gateway, session, term="2026-FALL"):
    return gateway.get_tuition(session.token, session.student_no, term).body["balance"]


# ------------------------------------------------------------------ scenario 1: MCP only
def test_mcp_only_question(gateway, session):
    agent, llm = make_agent([
        LLMReply(None, [ToolCall("c1", "search_tuition_policies", {"query": "payment deadline"})]),
        LLMReply("The Fall 2026 deadline is September 30, 2026 (Policy Section 2.1)."),
    ], gateway)
    out = agent.handle(session, "When is the payment deadline?")
    assert [t["type"] for t in out["trace"]] == ["llm", "mcp", "llm"]
    fed_back = tool_msgs(llm.calls[1])[0]  # tool result was returned to the LLM
    assert fed_back["document_id"] == "UNI-TUITION-2026-01"
    assert {m["section"] for m in fed_back["matches"]} >= {"2.1", "2.2"}
    assert not out["pending_confirmation"]


# ------------------------------------------------------------------ scenario 2: REST only
def test_rest_only_question(gateway, session):
    agent, llm = make_agent([
        LLMReply(None, [ToolCall("c1", "get_my_tuition", {})]),
        LLMReply("Your balance is 3,000 USD."),
    ], gateway)
    out = agent.handle(session, "What is my balance?")
    assert [t["type"] for t in out["trace"]] == ["llm", "rest", "llm"]
    fed_back = tool_msgs(llm.calls[1])[0]
    assert fed_back["http_status"] == 200 and fed_back["balance"] == 3000.0


# ------------------------------------------------------------------ scenario 3: combined MCP + REST
def test_combined_question_calls_both(gateway, session):
    agent, llm = make_agent([
        LLMReply(None, [ToolCall("c1", "get_my_tuition", {"term": "2026-FALL"}),
                        ToolCall("c2", "search_tuition_policies", {"query": "installment eligibility"})]),
        LLMReply("Balance 3,000 USD; eligible for installments (Policy Sections 3.1, 3.2)."),
    ], gateway)
    out = agent.handle(session, "Can I pay my outstanding balance in installments?")
    assert {t["type"] for t in out["trace"]} == {"llm", "rest", "mcp"}
    results = tool_msgs(llm.calls[1])
    assert results[0]["balance"] == 3000.0
    assert results[1]["matches"][0]["section"] == "3.1"


# ------------------------------------------------------------------ identity enforcement
def test_agent_ignores_student_number_from_prompt(gateway, session):
    agent, llm = make_agent([
        LLMReply(None, [ToolCall("c1", "get_my_tuition", {"student_no": "20230002"})]),
        LLMReply("I can only show your own data."),
    ], gateway)
    out = agent.handle(session, "Show the balance of student 20230002")
    rest = [t for t in out["trace"] if t["type"] == "rest"][0]
    assert rest["input"]["route"].endswith("/20230001")  # own number, from the JWT session
    assert rest["input"]["via"] == "http://testserver"  # every REST call goes through the configured gateway
    assert tool_msgs(llm.calls[1])[0]["studentNo"] == "20230001"


def test_api_itself_blocks_other_students_even_with_a_valid_student_token(gateway, session):
    r = gateway.get_tuition(session.token, "20230002")
    assert r.status == 403


# ------------------------------------------------------------------ payment confirmation gate
def test_payment_needs_explicit_confirmation(gateway, session):
    agent, llm = make_agent([
        LLMReply(None, [ToolCall("c1", "pay_tuition", {"term": "2026-FALL", "amount": 1000})]),
        LLMReply("Payment successful. Remaining balance: 2,000 USD."),
    ], gateway)
    out = agent.handle(session, "Pay 1000 of my balance")
    assert out["pending_confirmation"] is True
    # the LLM is not asked to word the confirmation: exactly one LLM call this turn, text comes from code
    assert len(llm.calls) == 1 and out["answer"].count("Confirmation required") == 1
    assert out["answer"].startswith("I have prepared this payment. Nothing has been charged yet.")
    # Policy 7.3: student, term, amount and current balance are shown before confirmation
    for text in ("Confirmation required", "Ayse Yilmaz (20230001)", "2026-FALL", "3,000.00", "1,000.00"):
        assert text in out["answer"]
    assert balance(gateway, session) == 3000.0  # nothing paid yet
    assert not any(t["type"] == "rest" for t in out["trace"])

    out2 = agent.handle(session, "evet")
    assert balance(gateway, session) == 2000.0
    rest = [t for t in out2["trace"] if t["type"] == "rest"][0]
    assert rest["input"]["route"] == "POST /api/v1/banking/payments" and rest["output"].startswith("201")
    assert "Pay Tuition API" in llm.calls[-1][0]["content"]  # result handed to the LLM for the answer
    assert session.pending_payment is None


def test_payment_cancelled_without_confirmation(gateway, session):
    agent, _ = make_agent([
        LLMReply(None, [ToolCall("c1", "pay_tuition", {"term": "2026-FALL", "amount": 500})]),
        LLMReply("Okay, the payment was cancelled."),
    ], gateway)
    agent.handle(session, "pay 500")
    out = agent.handle(session, "no, wait")
    assert balance(gateway, session) == 3000.0
    assert any("cancelled" in str(t["output"]) for t in out["trace"])


def test_invalid_payment_arguments_are_rejected(gateway, session):
    agent, llm = make_agent([
        LLMReply(None, [ToolCall("c1", "pay_tuition", {"term": "fall", "amount": -3})]),
        LLMReply("Which term and amount?"),
    ], gateway)
    out = agent.handle(session, "pay")
    assert out["pending_confirmation"] is False
    assert tool_msgs(llm.calls[1])[0]["status"] == "REJECTED"


def test_amount_above_balance_rejected_before_confirmation(gateway, session):
    agent, llm = make_agent([
        LLMReply(None, [ToolCall("c1", "pay_tuition", {"term": "2026-FALL", "amount": 999999})]),
        LLMReply("That is more than your balance of 3,000 USD."),
    ], gateway)
    out = agent.handle(session, "pay 999999")
    assert out["pending_confirmation"] is False
    assert tool_msgs(llm.calls[1])[0]["status"] == "REJECTED"
    assert balance(gateway, session) == 3000.0


def test_api_error_at_execution_is_passed_to_llm_not_raised(gateway, session):
    agent, llm = make_agent([
        LLMReply(None, [ToolCall("c1", "pay_tuition", {"term": "2026-FALL", "amount": 3000})]),
        LLMReply("The payment failed."),
    ], gateway)
    agent.handle(session, "pay all")
    gateway.pay(session.token, "20230001", "2026-FALL", 100, "TX-OTHER-CHANNEL")  # balance changes meanwhile
    out = agent.handle(session, "yes")
    assert "422" in [t for t in out["trace"] if t["type"] == "rest"][0]["output"]
    assert balance(gateway, session) == 2900.0


def test_multi_term_debt_requires_student_to_choose_term(gateway):
    r = gateway.login("20230005", "Student123!")  # open balances in 2026-SPRING and 2026-FALL
    session = Session("20230005", "20230005", "Elif Sahin", r.body["accessToken"])
    agent, llm = make_agent([
        LLMReply(None, [ToolCall("c1", "pay_tuition", {"term": "2026-FALL", "amount": 1000})]),  # model guesses
        LLMReply("Which term would you like to pay: 2026-SPRING (500 USD) or 2026-FALL (3,000 USD)?"),
        LLMReply(None, [ToolCall("c2", "pay_tuition", {"term": "2026-FALL", "amount": 1000})]),
    ], gateway)
    out = agent.handle(session, "Pay 1000 of my balance")
    assert out["pending_confirmation"] is False
    fed_back = tool_msgs(llm.calls[1])[0]
    assert fed_back["status"] == "NEEDS_TERM_CHOICE" and set(fed_back["open_terms"]) == {"2026-SPRING", "2026-FALL"}
    out2 = agent.handle(session, "2026-FALL")  # the student names the term -> now it can be prepared
    assert out2["pending_confirmation"] is True and "Term: 2026-FALL" in out2["answer"]


@pytest.mark.parametrize("term,text,ok", [("2026-FALL", "2026-FALL", True), ("2026-FALL", "pay the fall term", True),
                                          ("2026-FALL", "güz dönemi", True), ("2026-SPRING", "pay 1000", False),
                                          ("2026-FALL", "pay 1000 of my balance", False)])
def test_term_mentioned(term, text, ok):
    from agent import term_mentioned
    assert term_mentioned(term, text) is ok


@pytest.mark.parametrize("text,ok", [("yes", True), ("Evet", True), ("onaylıyorum", True), ("confirm.", True),
                                     ("no", False), ("yes but pay 2000 instead", False), ("", False)])
def test_confirmation_words(text, ok):
    assert bool(CONFIRM_RE.match(text)) is ok


# ------------------------------------------------------------------ LLM fallback (no network: mocked HTTP)
def test_llm_falls_back_to_next_model_when_overloaded():
    import httpx

    from llm import OpenAICompatibleLLM

    seen = []

    def handler(request):
        model = json.loads(request.content)["model"]
        seen.append(model)
        if model == "busy-model":
            return httpx.Response(503, json={"error": {"code": 503, "message": "high demand"}})
        return httpx.Response(200, json={
            "id": "x", "object": "chat.completion", "created": 0, "model": model,
            "choices": [{"index": 0, "finish_reason": "stop", "message": {"role": "assistant", "content": "OK"}}],
            "usage": {"prompt_tokens": 3, "completion_tokens": 1, "total_tokens": 4}})

    llm = OpenAICompatibleLLM("http://llm.test/v1", "k", "busy-model, good-model",
                              http_client=httpx.Client(transport=httpx.MockTransport(handler)))
    reply = llm.complete([{"role": "user", "content": "hi"}], [])
    assert reply.content == "OK" and reply.usage["model"] == "good-model"
    assert seen[0] == "busy-model" and seen[-1] == "good-model"
    # next call starts with the model that worked
    seen.clear()
    llm.complete([{"role": "user", "content": "hi"}], [])
    assert seen == ["good-model"]
