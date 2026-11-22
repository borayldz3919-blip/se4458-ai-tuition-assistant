"""Three (+) distinct retrieval queries against the RUNNING MCP server, through a real MCP client
(Streamable HTTP). This is the evidence for "Demonstrate three distinct retrieval queries".

    docker compose run --rm -v ./scripts:/scripts agent python /scripts/mcp_retrieval_demo.py http://mcp:8001/mcp
    python scripts/mcp_retrieval_demo.py https://<mcp-fqdn>/mcp        # against the cloud deployment
"""
import asyncio
import json
import sys

from mcp import Client

URL = sys.argv[1] if len(sys.argv) > 1 else "http://localhost:8001/mcp"
QUERIES = [
    "When is the Fall tuition deadline?",
    "installment eligibility",
    "late fee",
    "refund after early withdrawal",
]


async def main() -> None:
    async with Client(URL) as c:
        tools = (await c.list_tools()).tools
        print(f"MCP server: {URL}")
        print("Tools exposed:", [t.name for t in tools])
        print("Input schema of search_tuition_policies:", json.dumps(tools[0].input_schema))
        for i, q in enumerate(QUERIES, 1):
            res = await c.call_tool("search_tuition_policies", {"query": q})
            data = res.structured_content or json.loads(res.content[0].text)
            print(f"\n==== Retrieval test {i}: search_tuition_policies(query={q!r})")
            print(f"document: {data['document']} ({data['document_id']})")
            for m in data["matches"]:
                text = " ".join(m["text"].split())
                print(f"  [{m['score']:5.2f}] Section {m['section']} - {m['heading']}")
                print(f"          {text[:160]}{'...' if len(text) > 160 else ''}")


if __name__ == "__main__":
    asyncio.run(main())
