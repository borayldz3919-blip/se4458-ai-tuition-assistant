"""Backends the agent executes tools against.

* GatewayClient -> API Gateway (Kong) -> Tuition REST API   (REST path)
* MCPPolicyClient -> MCP Server (Streamable HTTP)            (MCP path, separate from the gateway)
"""
from __future__ import annotations

import asyncio
import json
from dataclasses import dataclass

import httpx


@dataclass
class ApiResult:
    status: int
    body: dict

    @property
    def ok(self) -> bool:
        return 200 <= self.status < 300


class GatewayClient:
    def __init__(self, base_url: str, http: httpx.Client | None = None):
        self.base_url = base_url.rstrip("/")
        # `http` can be injected in tests (e.g. a Starlette TestClient wrapping the API in-process)
        self.http = http or httpx.Client(base_url=self.base_url, timeout=20)

    def _call(self, method: str, path: str, token: str | None = None, **kw) -> ApiResult:
        headers = {"Authorization": f"Bearer {token}"} if token else {}
        try:
            r = self.http.request(method, path, headers=headers, **kw)
        except httpx.HTTPError as e:
            return ApiResult(503, {"error": {"code": "GATEWAY_UNREACHABLE", "message": str(e)}})
        try:
            body = r.json()
        except ValueError:
            body = {"error": {"code": f"HTTP_{r.status_code}", "message": r.text[:300]}}
        if not isinstance(body, dict):
            body = {"data": body}
        return ApiResult(r.status_code, body)

    def login(self, username: str, password: str) -> ApiResult:
        return self._call("POST", "/api/v1/auth/login", json={"username": username, "password": password})

    def get_tuition(self, token: str, student_no: str, term: str | None = None) -> ApiResult:
        params = {"term": term} if term else None
        return self._call("GET", f"/api/v1/banking/tuition/{student_no}", token, params=params)

    def pay(self, token: str, student_no: str, term: str, amount: float, transaction_id: str) -> ApiResult:
        return self._call("POST", "/api/v1/banking/payments", token, json={
            "studentNo": student_no, "term": term, "amount": amount, "transactionId": transaction_id})


class MCPPolicyClient:
    """MCP client: opens a Streamable-HTTP session to the MCP server and calls search_tuition_policies."""

    def __init__(self, url_or_server):
        self.target = url_or_server  # URL in production; an in-process MCPServer object in tests

    async def _search(self, query: str, top_k: int) -> dict:
        from mcp import Client

        async with Client(self.target) as c:
            res = await c.call_tool("search_tuition_policies", {"query": query, "top_k": top_k})
            if res.is_error:
                return {"error": res.content[0].text if res.content else "MCP tool error"}
            return res.structured_content or json.loads(res.content[0].text)

    def search(self, query: str, top_k: int = 3) -> dict:
        try:
            return asyncio.run(self._search(query, top_k))
        except Exception as e:  # noqa: BLE001 - surface as a tool error to the LLM
            return {"error": f"MCP server unavailable: {e}"}
