"""MCP server tests: chunking, three distinct retrieval queries, and a real MCP protocol round-trip
(in-memory transport, so no network or LLM is needed)."""
import asyncio
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import pytest  # noqa: E402
from mcp import Client  # noqa: E402

import server  # noqa: E402
from indexer import build_index, chunk_markdown, tokenize  # noqa: E402


@pytest.fixture(scope="module")
def index():
    return build_index()


def test_document_is_split_into_numbered_sections(index):
    ids = [c.id for c in index.chunks]
    assert len(ids) == len(set(ids)) == 25
    assert {"1", "2.1", "3.1", "3.2", "4.1", "6.1", "7.3", "9"} <= set(ids)
    assert index.document_id == "UNI-TUITION-2026-01"


def test_chunker_handles_section_without_subsections():
    chunks = chunk_markdown("## 1. Intro\n### 1.1 A\ntext a\n## 2. Contact\ncall us\n")
    assert [(c.id, c.title, c.text) for c in chunks] == [("1.1", "A", "text a"), ("2", "Contact", "call us")]


def test_tokenizer_normalises_synonyms_and_plurals():
    assert tokenize("Instalments are DUE") == ["installment", "deadline"]


# ---- three distinct retrieval queries (also shown in README) ----
@pytest.mark.parametrize(
    "query,expected_top",
    [
        ("Fall tuition deadline", "8.1"),
        ("installment eligibility", "3.1"),
        ("late fee", "4.1"),
        ("early withdrawal refund", "6.1"),
        ("duplicate payment submissions", "7.4"),
        ("Spring deadline", "2.2"),
    ],
)
def test_retrieval_returns_expected_section_first(index, query, expected_top):
    hits = index.search(query, 3)
    assert hits and hits[0][0].id == expected_top


def test_unrelated_query_returns_nothing(index):
    assert index.search("pizza delivery", 3) == []


def test_tool_function_formats_results_and_clamps_top_k():
    r = server.search("installment rules", top_k=50)
    assert r["document"] == "University_Tuition_Policies.md" and r["document_id"] == "UNI-TUITION-2026-01"
    assert 1 <= len(r["matches"]) <= 5
    first = r["matches"][0]
    assert first["section"] == "3.2" and first["heading"] == "Number and size of installments"
    assert {"section", "heading", "reference", "parent_section", "score", "text"} <= first.keys()


def test_empty_query():
    assert server.search("   ")["matches"] == []


def test_mcp_protocol_roundtrip_in_memory():
    async def run():
        async with Client(server.mcp) as c:
            tools = await c.list_tools()
            assert [t.name for t in tools.tools] == ["search_tuition_policies"]
            res = await c.call_tool("search_tuition_policies", {"query": "late fee", "top_k": 2})
            assert not res.is_error
            payload = res.structured_content or json.loads(res.content[0].text)
            assert payload["matches"][0]["section"] == "4.1"

    asyncio.run(run())
