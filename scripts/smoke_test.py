"""End-to-end check THROUGH THE API GATEWAY (cross-platform, needs only `httpx`).

    python scripts/smoke_test.py http://localhost:8000 [DIRECT_API_URL]
or, with nothing installed locally (uses the agent image):
    docker compose run --rm -v ./scripts:/scripts -v ./samples:/samples agent python /scripts/smoke_test.py http://gateway:8000

Evidence produced: routed versioned endpoints, a protected route (401 without / 200 with JWT), error
responses, the per-student daily limit (429), authorization (403), a simulated payment, duplicate
transaction rejection, paging and the batch upload.
"""
import json
import sys
import time
from pathlib import Path

import httpx

GW = (sys.argv[1] if len(sys.argv) > 1 else "http://localhost:8000").rstrip("/")
DIRECT_API = sys.argv[2].rstrip("/") if len(sys.argv) > 2 else None  # optional: prove the gateway can't be bypassed
STUDENT = "20230002"
c = httpx.Client(base_url=GW, timeout=20)
CSV = next((p for p in [Path(__file__).resolve().parents[1] / "samples" / "tuition_batch.csv",
                        Path("/samples/tuition_batch.csv")] if p.exists()), None)


def show(title, r):
    print(f"\n==== {title}\n{r.request.method} {r.request.url}\n-> HTTP {r.status_code}")
    try:
        print(json.dumps(r.json(), indent=2, ensure_ascii=False)[:1500])
    except ValueError:
        print(r.text[:500])
    return r


def login(user, pw):
    return c.post("/api/v1/auth/login", json={"username": user, "password": pw}).json()["accessToken"]


tok = login(STUDENT, "Student123!")
H = {"Authorization": f"Bearer {tok}"}
print(f"Gateway: {GW}\nLogged in as {STUDENT} (POST /api/v1/auth/login, public route)")

if DIRECT_API:
    show("Direct call to the Tuition API, bypassing the gateway (expect 403 GATEWAY_REQUIRED)",
         httpx.get(f"{DIRECT_API}/api/v1/mobile/tuition/{STUDENT}", timeout=20))

show("Protected route WITHOUT token (expect 401 from gateway)", c.get(f"/api/v1/banking/tuition/{STUDENT}"))
show("Protected route WITH token (expect 200)", c.get(f"/api/v1/banking/tuition/{STUDENT}", headers=H))
show("Another student's record with this token (expect 403)", c.get("/api/v1/banking/tuition/20230001", headers=H))

sno = f"202300{int(time.time()) % 15 + 10}"
print(f"\n==== Mobile Query Tuition, no auth, 4 calls for student {sno} (expect 200,200,200,429)")
for i in range(1, 5):
    r = c.get(f"/api/v1/mobile/tuition/{sno}")
    print(f"call {i}: HTTP {r.status_code}  X-RateLimit-Remaining-Day={r.headers.get('x-ratelimit-remaining-day')}"
          f"  body={r.text[:120] if r.status_code != 200 else 'ok'}")

tx = f"SMOKE-{int(time.time())}"
pay = {"studentNo": STUDENT, "term": "2026-FALL", "amount": 100, "transactionId": tx}
show("Simulated partial payment (expect 201)", c.post("/api/v1/banking/payments", headers=H, json=pay))
show("Same transaction id again (expect 409 DUPLICATE_TRANSACTION)", c.post("/api/v1/banking/payments", headers=H, json=pay))
show("Admin route with a student token (expect 403)", c.get("/api/v1/admin/tuition/unpaid?term=2026-FALL", headers=H))

A = {"Authorization": f"Bearer {login('admin', 'Admin123!')}"}
show("Add Tuition (admin)", c.post("/api/v1/admin/tuition", headers=A,
                                   json={"studentNo": "20249999", "term": "2026-FALL", "amount": 3000, "currency": "USD"}))
show("Unpaid Tuition Status page 1, size 5 (admin)",
     c.get("/api/v1/admin/tuition/unpaid", params={"term": "2026-FALL", "page": 1, "pageSize": 5}, headers=A))
if CSV:
    show("Add Tuition - Batch CSV (admin)", c.post("/api/v1/admin/tuition/batch", headers=A,
                                                  files={"file": (CSV.name, CSV.read_bytes(), "text/csv")}))
