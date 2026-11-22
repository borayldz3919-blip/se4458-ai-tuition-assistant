"""LLM access through the OpenAI-compatible Chat Completions API with real tool/function calling.

Works with Google Gemini (default, free tier), OpenAI, GitHub Models, Groq, Ollama... by changing
LLM_BASE_URL / LLM_MODEL / LLM_API_KEY. Tests use ScriptedLLM, so no live LLM is needed.
"""
from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any, Protocol


@dataclass
class ToolCall:
    id: str
    name: str
    arguments: dict[str, Any]


@dataclass
class LLMReply:
    content: str | None
    tool_calls: list[ToolCall] = field(default_factory=list)
    raw_message: dict[str, Any] = field(default_factory=dict)  # appended back to the conversation verbatim
    usage: dict[str, int] | None = None


class LLM(Protocol):
    def complete(self, messages: list[dict], tools: list[dict]) -> LLMReply: ...


class OpenAICompatibleLLM:
    """`model` may be a comma-separated list: if a model is unavailable (404 retired/not enabled for this
    key, 429 quota, 5xx overloaded) or times out, the next one is tried. Free tiers are often temporarily
    overloaded, so this keeps the assistant usable."""

    RETRYABLE_STATUS = {404, 429, 500, 502, 503, 504}

    def __init__(self, base_url: str, api_key: str, model: str, timeout: float = 30, http_client=None):
        from openai import OpenAI

        if not api_key:
            raise RuntimeError("LLM_API_KEY is not set (get a free Gemini key at https://aistudio.google.com/apikey)")
        self.client = OpenAI(base_url=base_url, api_key=api_key, timeout=timeout, max_retries=1,
                             http_client=http_client)
        self.models = [m.strip() for m in model.split(",") if m.strip()]
        self.model = self.models[0]

    def _create(self, messages: list[dict], tools: list[dict]):
        import openai

        errors = []
        # start with the model that worked last time, then the others in configured order
        order = [self.model] + [m for m in self.models if m != self.model]
        for m in order:
            try:
                resp = self.client.chat.completions.create(
                    model=m, messages=messages, tools=tools or None, temperature=0.1)
                self.model = m
                return resp
            except openai.APIStatusError as e:
                if e.status_code not in self.RETRYABLE_STATUS:
                    raise
                errors.append(f"{m}: HTTP {e.status_code}")
            except (openai.APITimeoutError, openai.APIConnectionError) as e:
                errors.append(f"{m}: {type(e).__name__}")
        raise RuntimeError("All configured LLM models are unavailable right now (" + "; ".join(errors) +
                           "). Please try again in a minute.")

    def complete(self, messages: list[dict], tools: list[dict]) -> LLMReply:
        resp = self._create(messages, tools)
        msg = resp.choices[0].message
        calls = []
        for tc in msg.tool_calls or []:
            try:
                args = json.loads(tc.function.arguments or "{}")
            except json.JSONDecodeError:
                args = {"_invalid_json": tc.function.arguments}
            calls.append(ToolCall(tc.id, tc.function.name, args))
        # model_dump keeps provider-specific extras (e.g. Gemini thought signatures) needed on the next turn
        raw = msg.model_dump(exclude_none=True)
        raw.pop("refusal", None)
        raw.pop("annotations", None)
        usage = None
        if resp.usage:
            usage = {"model": self.model, "prompt_tokens": resp.usage.prompt_tokens,
                     "completion_tokens": resp.usage.completion_tokens}
        return LLMReply(msg.content, calls, raw, usage)


class ScriptedLLM:
    """Deterministic test double. `script` is a list of LLMReply objects or callables(messages)->LLMReply."""

    def __init__(self, script: list):
        self.script = list(script)
        self.calls: list[list[dict]] = []

    def complete(self, messages: list[dict], tools: list[dict]) -> LLMReply:
        self.calls.append([dict(m) for m in messages])
        if not self.script:
            raise AssertionError("ScriptedLLM ran out of replies")
        step = self.script.pop(0)
        reply = step(messages) if callable(step) else step
        if not reply.raw_message:
            reply.raw_message = {"role": "assistant", "content": reply.content}
            if reply.tool_calls:
                reply.raw_message["tool_calls"] = [
                    {"id": c.id, "type": "function", "function": {"name": c.name, "arguments": json.dumps(c.arguments)}}
                    for c in reply.tool_calls
                ]
        return reply
