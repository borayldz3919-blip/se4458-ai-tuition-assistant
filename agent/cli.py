"""Command-line chat. Prints the full tool trace after every answer (useful for README logs).

    python cli.py --user 20230001 --password Student123!
"""
import argparse
import getpass
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from agent import Session, TuitionAgent  # noqa: E402
from clients import GatewayClient, MCPPolicyClient  # noqa: E402
from config import settings  # noqa: E402
from llm import OpenAICompatibleLLM  # noqa: E402


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--user", default=None)
    ap.add_argument("--password", default=None)
    ap.add_argument("-q", "--question", action="append", help="non-interactive: ask these questions in order")
    a = ap.parse_args()

    gw = GatewayClient(settings.gateway_url)
    user = a.user or input("Student number: ")
    res = gw.login(user, a.password or getpass.getpass("Password: "))
    if not res.ok:
        sys.exit(f"Login failed: {res.body}")
    token, sno = res.body["accessToken"], res.body["studentNo"]
    session = Session(user, sno, "", token)
    agent = TuitionAgent(OpenAICompatibleLLM(settings.llm_base_url, settings.llm_api_key, settings.llm_model),
                         gw, MCPPolicyClient(settings.mcp_url), settings.max_tool_rounds, settings.trace_log,
                         settings.timezone)
    print(f"Logged in as {sno} via gateway {settings.gateway_url}. Type 'exit' to quit.\n")

    questions = iter(a.question) if a.question else None
    while True:
        try:
            q = next(questions) if questions else input("You> ")
        except (StopIteration, EOFError):
            break
        if q.strip().lower() in ("exit", "quit"):
            break
        if questions:
            print(f"You> {q}")
        out = agent.handle(session, q)
        print("\n--- tool trace ---")
        for i, t in enumerate(out["trace"], 1):
            print(f"{i}. [{t['type'].upper()}] {t['tool']}  input={json.dumps(t['input'], ensure_ascii=False)}")
            print(f"     -> {t['output'] if isinstance(t['output'], str) else json.dumps(t['output'], ensure_ascii=False)}")
        print(f"--- answer ---\n{out['answer']}\n")


if __name__ == "__main__":
    main()
