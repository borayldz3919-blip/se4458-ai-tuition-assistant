"""MCP server exposing `search_tuition_policies` over Streamable HTTP (official MCP Python SDK, v2).

Run:  python server.py            -> http://0.0.0.0:8001/mcp
The index is loaded once at import time (built from the Markdown file only if index.json is missing).
"""
import logging
import os

from mcp.server import MCPServer

from indexer import SOURCE_NAME, load_or_build

log = logging.getLogger("mcp-tuition")
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")

INDEX = load_or_build()
log.info("Policy index ready: %d chunks from %s", len(INDEX.chunks), SOURCE_NAME)

MAX_EXCERPT = 900

mcp = MCPServer(
    "tuition-policies",
    instructions="Search the University Tuition Policies document. Always cite the returned section reference.",
)


def search(query: str, top_k: int = 3) -> dict:
    """Response shape follows Section 9 of the policy document: section ID, heading, document ID, text."""
    query = (query or "").strip()
    base = {"document": SOURCE_NAME, "document_id": INDEX.document_id, "query": query}
    if not query:
        return {**base, "matches": [], "message": "Empty query"}
    top_k = max(1, min(int(top_k), 5))
    hits = INDEX.search(query, top_k)
    return {
        **base,
        "matches": [
            {
                "section": c.id,
                "heading": c.title,
                "reference": c.reference,
                "parent_section": c.parent,
                "score": round(score, 3),
                "text": c.text if len(c.text) <= MAX_EXCERPT else c.text[:MAX_EXCERPT].rsplit(" ", 1)[0] + " ...",
            }
            for c, score in hits
        ],
        "message": None if hits else "No matching policy section found",
    }


@mcp.tool()
def search_tuition_policies(query: str, top_k: int = 3) -> dict:
    """Search the University Tuition Policies (deadlines, installments, late fees, scholarships, refunds,
    payment/banking instructions). Returns the most relevant passages with section ID, heading,
    document ID and text (e.g. section "3.2", heading "Number and size of installments"). Does NOT contain any student's balance.

    Args:
        query: Natural-language search text, e.g. "installment eligibility".
        top_k: Number of excerpts to return (1-5, default 3).
    """
    result = search(query, top_k)
    log.info("search_tuition_policies(%r) -> %s", query, [m["section"] for m in result["matches"]])
    return result


if __name__ == "__main__":
    mcp.run(
        transport="streamable-http",
        host=os.getenv("HOST", "0.0.0.0"),
        port=int(os.getenv("PORT", "8001")),
        stateless_http=True,
        json_response=True,
    )
