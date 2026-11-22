"""AI Agent orchestrator.

    user question -> LLM picks tools -> agent executes MCP and/or REST (REST via API Gateway)
    -> tool results go back to the LLM -> grounded answer (+ policy section references)

Safety rules enforced in CODE (not only in the prompt):
1. Identity: REST tools never take a student number from the LLM. The agent always uses the student
   number and JWT of the logged-in session, so "show me 20230002's balance" cannot leak data (and the
   API would answer 403 anyway).
2. Payments: the `pay_tuition` tool only creates a *pending* payment. The Pay Tuition API is called only
   after the student's NEXT message is an explicit confirmation ("yes", "evet", "confirm", ...).
"""
from __future__ import annotations

import json
import re
import time
import uuid
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

from clients import GatewayClient, MCPPolicyClient
from llm import LLM

TERM_RE = re.compile(r"^\d{4}-(FALL|SPRING|SUMMER)$")
CONFIRM_RE = re.compile(
    r"^\s*(yes|y|confirm|confirmed|i confirm|approve|evet|onayl[ıi]yorum|onayla|onay|tamam|ok|okay)\b[\s.!]*$",
    re.IGNORECASE,
)

SEASON_WORDS = {
    "FALL": ("fall", "autumn", "güz", "guz"),
    "SPRING": ("spring", "bahar"),
    "SUMMER": ("summer", "yaz"),
}


def term_mentioned(term: str, text: str) -> bool:
    """True if the student's own message names this term (e.g. '2026-FALL', 'fall', 'güz dönemi')."""
    text_l = (text or "").lower()
    if term.lower() in text_l:
        return True
    season = term.split("-")[-1]
    return any(re.search(rf"\b{w}\b", text_l) for w in SEASON_WORDS.get(season, ()))


TOOLS: list[dict] = [
    {
        "type": "function",
        "function": {
            "name": "search_tuition_policies",
            "description": (
                "Search the official University Tuition Policies document (deadlines, installment plans and "
                "eligibility, late fees, scholarships, refunds, payment methods, bank transfer instructions). "
                "Returns excerpts with section references. Contains NO personal balances."
            ),
            "parameters": {
                "type": "object",
                "properties": {"query": {"type": "string", "description": "Short search text in English, e.g. 'installment eligibility'"}},
                "required": ["query"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "get_my_tuition",
            "description": (
                "Get the logged-in student's live tuition records from the Tuition API: total tuition, "
                "paid amount and remaining balance per term, plus the overall balance. Only the logged-in "
                "student's own data can be read."
            ),
            "parameters": {
                "type": "object",
                "properties": {"term": {"type": "string", "description": "Optional term like 2026-FALL. Omit for all terms."}},
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "pay_tuition",
            "description": (
                "Prepare a (simulated) tuition payment for the logged-in student. This does NOT pay yet: the "
                "system will ask the student to explicitly confirm. Call it only when the student asked to pay "
                "and both term and amount are known (use get_my_tuition first to find the term/balance)."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "term": {"type": "string", "description": "Term like 2026-FALL"},
                    "amount": {"type": "number", "description": "Amount in the tuition currency, > 0"},
                },
                "required": ["term", "amount"],
            },
        },
    },
]

SYSTEM_PROMPT = """You are the University Tuition Assistant. You help ONE logged-in student: {name} (student number {student_no}).
Today is {today} (Europe/Istanbul).

LANGUAGE: Answer in English. Switch to Turkish only if the student's LATEST message itself is written in Turkish. The student's name, the university's location or earlier turns never decide the language.

How to work:
- Policy/rule questions (deadlines, installments, late fees, scholarships, refunds, how to pay, bank details): call search_tuition_policies.
- ANY question that involves this student's own amounts, balance, debt or payments: call get_my_tuition in THIS turn. Never reuse amounts from earlier messages; balances change.
- Questions that need both (e.g. "Can I pay my outstanding balance in installments?", "Will I get a late fee?"): call BOTH get_my_tuition and search_tuition_policies, then apply the policy to the live numbers.
- Payment requests: if the amount is unclear, ask. If the student has open balances in more than one term and did not name the term, ASK which term; never pick one yourself. Then call pay_tuition. The system itself then shows the confirmation summary (student, term, balance, amount) and handles the reply. Never say a payment is done unless you received a SUCCESS result.
- Ground every statement in tool results. Never invent amounts, dates or rules. Cite policy sections like "(Policy Section 3.2)".
- Installments: the policy says an arrangement is SUBJECT TO APPROVAL (Section 3.4). Explain eligibility and the possible split using the live balance, but never promise or claim approval. A partial payment is not an approved installment plan.
- Late fees and scholarships: explain the rule from the policy, but only report fees/discounts that appear in the live tuition record.
- You can only see this student's data. If asked about another student, explain that you can't share other students' information.
- Be concise."""


@dataclass
class PendingPayment:
    term: str
    amount: float
    transaction_id: str
    created_at: float
    current_balance: float = 0.0
    currency: str = ""


@dataclass
class Session:
    username: str
    student_no: str
    full_name: str
    token: str
    history: list[dict] = field(default_factory=list)  # user/assistant turns only
    pending_payment: PendingPayment | None = None
    current_user_text: str = ""  # message of the turn being processed (used by the term-choice rule)


class TuitionAgent:
    def __init__(self, llm: LLM, gateway: GatewayClient, policies: MCPPolicyClient,
                 max_tool_rounds: int = 5, trace_log: str | None = None, timezone: str = "Europe/Istanbul"):
        self.llm, self.gateway, self.policies = llm, gateway, policies
        self.max_tool_rounds = max_tool_rounds
        self.trace_log = Path(trace_log) if trace_log else None
        self.tz = ZoneInfo(timezone)

    # ------------------------------------------------------------------ public
    def handle(self, session: Session, user_text: str) -> dict[str, Any]:
        trace: list[dict] = []
        user_text = (user_text or "").strip()
        session.current_user_text = user_text
        extra_system: list[str] = []

        if session.pending_payment:
            pending, session.pending_payment = session.pending_payment, None
            if CONFIRM_RE.match(user_text):
                result = self._execute_payment(session, pending, trace)
                extra_system.append(
                    "The student explicitly confirmed the pending payment and the agent called the Pay Tuition API. "
                    f"API result (status {result['http_status']}): {json.dumps(result['body'])}. "
                    "Report the outcome (paid amount and remaining balance, or the error) to the student."
                )
            else:
                trace.append({"type": "confirmation", "tool": "pay_tuition", "input": user_text,
                              "output": "NOT CONFIRMED -> payment cancelled, Pay API not called"})
                extra_system.append(
                    f"The student did NOT confirm the pending payment of {pending.amount} for {pending.term}; it was "
                    "cancelled and no money was moved. Mention this briefly, then answer their new message if it has one."
                )

        answer = self._run_llm_loop(session, user_text, extra_system, trace)

        if session.pending_payment:  # created during this turn -> code-enforced confirmation prompt
            p = session.pending_payment
            answer = (
                "I have prepared this payment. Nothing has been charged yet.\n\n"
                "**Confirmation required (Policy Section 7.3)**\n"
                f"Student: {session.full_name} ({session.student_no})\n"
                f"Term: {p.term}\n"
                f"Current balance: {p.current_balance:,.2f} {p.currency}\n"
                f"Amount to pay: **{p.amount:,.2f} {p.currency}** (remaining after payment: "
                f"{p.current_balance - p.amount:,.2f})\n"
                "Reply **yes / evet** to confirm, or anything else to cancel."
            )

        session.history += [{"role": "user", "content": user_text}, {"role": "assistant", "content": answer}]
        session.history = session.history[-12:]
        result = {"answer": answer, "trace": trace,
                  "pending_confirmation": bool(session.pending_payment)}
        self._log(session, user_text, result)
        return result

    # ------------------------------------------------------------------ LLM loop
    def _run_llm_loop(self, session: Session, user_text: str, extra_system: list[str], trace: list[dict]) -> str:
        system = SYSTEM_PROMPT.format(name=session.full_name or session.username, student_no=session.student_no,
                                      today=datetime.now(self.tz).strftime("%A %d %B %Y"))
        if extra_system:  # one system message only (some OpenAI-compatible providers ignore later ones)
            system += "\n\nCONTEXT FOR THIS TURN:\n" + "\n".join(f"- {n}" for n in extra_system)
        messages: list[dict] = [{"role": "system", "content": system}, *session.history]
        messages.append({"role": "user", "content": user_text or "(empty message)"})

        for _ in range(self.max_tool_rounds):
            t0 = time.perf_counter()
            reply = self.llm.complete(messages, TOOLS)
            trace.append({
                "type": "llm",
                "tool": "LLM",
                "input": f"{len(messages)} messages",
                "output": ("tool selection: " + ", ".join(f"{c.name}({json.dumps(c.arguments)})" for c in reply.tool_calls))
                if reply.tool_calls else "final answer",
                "ms": round((time.perf_counter() - t0) * 1000),
                "usage": reply.usage,
            })
            messages.append(reply.raw_message)
            if not reply.tool_calls:
                return reply.content or ""
            for call in reply.tool_calls:
                output = self._execute_tool(session, call.name, call.arguments, trace)
                messages.append({"role": "tool", "tool_call_id": call.id,
                                 "content": json.dumps(output, ensure_ascii=False)})
            if session.pending_payment:
                # A payment is waiting for confirmation: stop here. The confirmation text is produced by
                # code (see handle), so the LLM cannot word it differently or claim the payment is done.
                return ""
        return "Sorry, I could not complete this request. Please rephrase your question."

    # ------------------------------------------------------------------ tool execution
    def _execute_tool(self, session: Session, name: str, args: dict, trace: list[dict]) -> dict:
        t0 = time.perf_counter()
        if name == "search_tuition_policies":
            query = str(args.get("query", "")).strip()
            out = self.policies.search(query, 3)
            kind, shown_input = "mcp", {"query": query}
            summary = [m["reference"] for m in out.get("matches", [])] or out
        elif name == "get_my_tuition":
            term = args.get("term") or None
            if term and not TERM_RE.match(str(term)):
                term = None
            # identity comes from the session/JWT, never from the model's arguments
            res = self.gateway.get_tuition(session.token, session.student_no, term)
            out = {"http_status": res.status, **res.body}
            kind = "rest"
            shown_input = {"route": f"GET /api/v1/banking/tuition/{session.student_no}" + (f"?term={term}" if term else ""),
                           "via": self.gateway.base_url}
            summary = (f"{res.status}: balance {res.body.get('balance')} {res.body.get('currency', '')}"
                       if res.ok else f"{res.status}: {res.body.get('error')}")
        elif name == "pay_tuition":
            out, kind, shown_input, summary = self._prepare_payment(session, args)
        else:
            out = {"error": f"Unknown tool {name}"}
            kind, shown_input, summary = "error", args, out
        trace.append({"type": kind, "tool": name, "input": shown_input, "output": summary,
                      "ms": round((time.perf_counter() - t0) * 1000)})
        return out

    def _prepare_payment(self, session: Session, args: dict):
        """Creates a PENDING payment only. Per Policy Section 7.3 the confirmation shows student, term, amount
        and current balance; per 7.2 the amount must be positive and not exceed the balance."""
        term = str(args.get("term", "")).strip().upper()
        try:
            amount = round(float(args.get("amount")), 2)
        except (TypeError, ValueError):
            amount = 0.0
        if not TERM_RE.match(term) or amount <= 0:
            out = {"status": "REJECTED", "message": "A valid term (e.g. 2026-FALL) and a positive amount are required."}
            return out, "confirmation", args, out["status"]
        # Ambiguity rule (enforced in code): with open balances in several terms, the STUDENT must name the
        # term. The model may not pick one on its own.
        overall = self.gateway.get_tuition(session.token, session.student_no)
        open_terms = [t["term"] for t in overall.body.get("terms", []) if t.get("balance", 0) > 0] if overall.ok else []
        if len(open_terms) > 1 and not term_mentioned(term, session.current_user_text):
            out = {"status": "NEEDS_TERM_CHOICE", "open_terms": open_terms,
                   "message": "The student has open balances in several terms and did not say which one. "
                              "Ask the student which term to pay (list the terms with their balances). "
                              "Do not choose a term yourself."}
            return out, "confirmation", {"term": term, "amount": amount}, f"NEEDS_TERM_CHOICE: {open_terms}"
        live = self.gateway.get_tuition(session.token, session.student_no, term)
        if not live.ok:
            out = {"status": "REJECTED", "http_status": live.status, "message": "Could not load the term balance",
                   **live.body}
            return out, "confirmation", {"term": term, "amount": amount}, f"REJECTED ({live.status})"
        current_balance = float(live.body.get("balance", 0))
        if amount > current_balance:
            out = {"status": "REJECTED", "current_balance": current_balance,
                   "message": f"Amount exceeds the current outstanding balance for {term} (Policy Section 7.2)."}
            return out, "confirmation", {"term": term, "amount": amount}, "REJECTED: amount exceeds balance"
        pending = PendingPayment(term, amount, f"AGT-{uuid.uuid4().hex[:12].upper()}", time.time(),
                                 current_balance, live.body.get("currency", ""))
        session.pending_payment = pending
        out = {"status": "CONFIRMATION_REQUIRED", "student_no": session.student_no, "term": term, "amount": amount,
               "current_balance": current_balance, "currency": pending.currency,
               "transaction_id": pending.transaction_id,
               "message": "Payment NOT executed yet. The system will ask the student to reply 'yes' to confirm."}
        return out, "confirmation", {"term": term, "amount": amount}, "pending -> waiting for explicit user confirmation"

    def _execute_payment(self, session: Session, p: PendingPayment, trace: list[dict]) -> dict:
        t0 = time.perf_counter()
        res = self.gateway.pay(session.token, session.student_no, p.term, p.amount, p.transaction_id)
        trace.append({"type": "confirmation", "tool": "pay_tuition", "input": "user confirmed",
                      "output": "explicit confirmation received"})
        trace.append({
            "type": "rest", "tool": "pay_tuition",
            "input": {"route": "POST /api/v1/banking/payments", "via": self.gateway.base_url,
                      "studentNo": session.student_no, "term": p.term,
                      "amount": p.amount, "transactionId": p.transaction_id},
            "output": (f"{res.status}: paid {res.body.get('paidAmount')}, remaining {res.body.get('remainingBalance')}"
                       if res.ok else f"{res.status}: {res.body.get('error')}"),
            "ms": round((time.perf_counter() - t0) * 1000),
        })
        return {"http_status": res.status, "body": res.body}

    # ------------------------------------------------------------------ trace log
    def _log(self, session: Session, question: str, result: dict) -> None:
        if not self.trace_log:
            return
        try:
            self.trace_log.parent.mkdir(parents=True, exist_ok=True)
            with self.trace_log.open("a", encoding="utf-8") as f:
                f.write(json.dumps({"ts": datetime.now(self.tz).isoformat(), "student_no": session.student_no,
                                    "question": question, **result}, ensure_ascii=False) + "\n")
        except OSError:
            pass
