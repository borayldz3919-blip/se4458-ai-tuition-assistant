"""Diagnose the LLM connection from inside the agent container.

    docker compose run --rm -v ./scripts:/scripts agent python /scripts/check_llm.py
"""
import os
import time

import httpx

key = os.environ.get("LLM_API_KEY", "").strip()
base = os.environ.get("LLM_BASE_URL", "https://generativelanguage.googleapis.com/v1beta/openai/").rstrip("/")
models = [m.strip() for m in os.environ.get("LLM_MODEL", "gemini-2.5-flash").split(",") if m.strip()]
H = {"Authorization": f"Bearer {key}"}

print(f"key set: {bool(key)} (length {len(key)})   base: {base}   models: {models}")


def timed(label, fn):
    t = time.time()
    try:
        r = fn()
        print(f"[{label}] HTTP {r.status_code} in {time.time() - t:.1f}s")
        return r
    except Exception as e:  # noqa: BLE001
        print(f"[{label}] FAILED after {time.time() - t:.1f}s: {type(e).__name__}: {e}")
        return None


timed("internet: google.com", lambda: httpx.get("https://www.google.com", timeout=15))

r = timed("list models", lambda: httpx.get(f"{base}/models", headers=H, timeout=20))
if r is not None:
    if r.status_code == 200:
        ids = [m["id"].removeprefix("models/") for m in r.json().get("data", [])]
        print("  flash models available:", [i for i in ids if "flash" in i][:20])
        for m in models:
            print(f"  configured model {m} available:", m in ids)
    else:
        print("  ", r.text[:400])

for model in models:
    r = timed(f"chat completion {model}", lambda: httpx.post(
        f"{base}/chat/completions", headers=H, timeout=90,
        json={"model": model, "messages": [{"role": "user", "content": "Reply with just: OK"}]}))
    if r is not None:
        print("  ", r.text[:300].replace("\n", " "))
