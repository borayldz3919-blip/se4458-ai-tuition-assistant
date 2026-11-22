"""At least one success and one failure/edge case for every REST operation."""
import time

import jwt

from app.config import settings


# ============================================================ Auth
class TestLogin:
    def test_login_success_returns_jwt_with_role_and_student_no(self, client):
        r = client.post("/api/v1/auth/login", json={"username": "20230001", "password": "Student123!"})
        assert r.status_code == 200
        body = r.json()
        claims = jwt.decode(body["accessToken"], settings.jwt_secret, algorithms=["HS256"], issuer=settings.jwt_issuer)
        assert claims["role"] == "student" and claims["student_no"] == "20230001"
        assert body["studentNo"] == "20230001"

    def test_login_wrong_password_401(self, client):
        r = client.post("/api/v1/auth/login", json={"username": "20230001", "password": "nope"})
        assert r.status_code == 401
        assert r.json()["error"]["code"] == "INVALID_CREDENTIALS"


# ============================================================ Mobile: Query Tuition (no auth, 3/day)
class TestMobileQueryTuition:
    def test_overall_balance_and_term_breakdown(self, client):
        r = client.get("/api/v1/mobile/tuition/20230001")
        assert r.status_code == 200
        b = r.json()
        assert b["scope"] == "OVERALL"
        assert b["tuitionTotal"] == 5800.0 and b["balance"] == 3000.0
        assert {t["term"]: t["status"] for t in b["terms"]} == {"2026-SPRING": "PAID", "2026-FALL": "UNPAID"}
        assert r.headers["X-RateLimit-Remaining"] == "2"

    def test_term_filter(self, client):
        b = client.get("/api/v1/mobile/tuition/20230002?term=2026-FALL").json()
        assert b["scope"] == "TERM" and b["balance"] == 2000.0 and b["terms"][0]["status"] == "PARTIALLY_PAID"

    def test_fourth_call_same_student_is_rate_limited(self, client):
        for _ in range(3):
            assert client.get("/api/v1/mobile/tuition/20230001").status_code == 200
        r = client.get("/api/v1/mobile/tuition/20230001")
        assert r.status_code == 429
        assert r.json()["error"]["code"] == "RATE_LIMIT_EXCEEDED"
        # the limit is keyed by student identity: another student is unaffected
        assert client.get("/api/v1/mobile/tuition/20230002").status_code == 200

    def test_unknown_student_404(self, client):
        r = client.get("/api/v1/mobile/tuition/99999999")
        assert r.status_code == 404 and r.json()["error"]["code"] == "NOT_FOUND"

    def test_invalid_student_no_format_422(self, client):
        assert client.get("/api/v1/mobile/tuition/abc").status_code == 422


# ============================================================ Banking: Query Tuition (auth)
class TestBankingQueryTuition:
    def test_requires_token(self, client):
        r = client.get("/api/v1/banking/tuition/20230001")
        assert r.status_code == 401 and r.json()["error"]["code"] == "UNAUTHORIZED"

    def test_student_reads_own_record(self, client, student1):
        r = client.get("/api/v1/banking/tuition/20230001", headers=student1)
        assert r.status_code == 200 and r.json()["balance"] == 3000.0

    def test_student_cannot_read_other_student(self, client, student1):
        r = client.get("/api/v1/banking/tuition/20230002", headers=student1)
        assert r.status_code == 403 and r.json()["error"]["code"] == "FORBIDDEN"

    def test_bank_can_read_any_student_and_is_not_rate_limited(self, client, bank):
        for _ in range(5):
            assert client.get("/api/v1/banking/tuition/20230002", headers=bank).status_code == 200

    def test_expired_token_rejected(self, client):
        now = int(time.time())
        tok = jwt.encode({"iss": settings.jwt_issuer, "sub": "bankapp", "role": "bank", "iat": now - 7200,
                          "exp": now - 3600}, settings.jwt_secret, algorithm="HS256")
        r = client.get("/api/v1/banking/tuition/20230001", headers={"Authorization": f"Bearer {tok}"})
        assert r.status_code == 401 and "expired" in r.json()["error"]["message"].lower()

    def test_forged_token_rejected(self, client):
        tok = jwt.encode({"iss": settings.jwt_issuer, "sub": "x", "role": "admin", "exp": int(time.time()) + 60},
                         "wrong-secret-wrong-secret-wrong-secret!!", algorithm="HS256")
        r = client.get("/api/v1/banking/tuition/20230001", headers={"Authorization": f"Bearer {tok}"})
        assert r.status_code == 401


# ============================================================ Banking: Pay Tuition
def _pay(client, headers, amount, tx, student="20230001", term="2026-FALL"):
    return client.post("/api/v1/banking/payments", headers=headers,
                       json={"studentNo": student, "term": term, "amount": amount, "transactionId": tx})


class TestPayTuition:
    def test_partial_then_full_payment(self, client, student1):
        r = _pay(client, student1, 1000, "TX-1")
        assert r.status_code == 201
        assert r.json() == {"status": "SUCCESS", "transactionId": "TX-1", "studentNo": "20230001",
                            "term": "2026-FALL", "paidAmount": 1000.0, "remainingBalance": 2000.0, "currency": "USD"}
        r = _pay(client, student1, 2000, "TX-2")
        assert r.status_code == 201 and r.json()["remainingBalance"] == 0.0
        assert client.get("/api/v1/banking/tuition/20230001?term=2026-FALL",
                          headers=student1).json()["terms"][0]["status"] == "PAID"

    def test_duplicate_transaction_id_rejected_and_balance_unchanged(self, client, student1):
        assert _pay(client, student1, 500, "TX-DUP").status_code == 201
        r = _pay(client, student1, 500, "TX-DUP")
        assert r.status_code == 409 and r.json()["error"]["code"] == "DUPLICATE_TRANSACTION"
        assert client.get("/api/v1/banking/tuition/20230001?term=2026-FALL", headers=student1).json()["balance"] == 2500.0

    def test_amount_exceeding_balance_rejected(self, client, bank):
        r = _pay(client, bank, 3000.01, "TX-BIG")
        assert r.status_code == 422 and r.json()["error"]["code"] == "AMOUNT_EXCEEDS_BALANCE"

    def test_already_paid_term_rejected(self, client, bank):
        r = _pay(client, bank, 10, "TX-PAID", student="20230003")
        assert r.status_code == 409 and r.json()["error"]["code"] == "ALREADY_PAID"

    def test_student_cannot_pay_for_someone_else(self, client, student1):
        r = _pay(client, student1, 100, "TX-OTHER", student="20230002")
        assert r.status_code == 403

    def test_zero_or_negative_amount_validation_error(self, client, bank):
        assert _pay(client, bank, 0, "TX-0").status_code == 422
        assert _pay(client, bank, -5, "TX-NEG").status_code == 422

    def test_unknown_term_404(self, client, bank):
        assert _pay(client, bank, 10, "TX-T", term="2020-FALL").status_code == 404

    def test_requires_auth(self, client):
        assert _pay(client, {}, 10, "TX-NOAUTH").status_code == 401


# ============================================================ Admin: Add Tuition
class TestAddTuition:
    def test_create_new_student_term(self, client, admin):
        r = client.post("/api/v1/admin/tuition", headers=admin,
                        json={"studentNo": "20239999", "term": "2026-FALL", "amount": 3200, "currency": "USD"})
        assert r.status_code == 201 and r.json()["status"] == "CREATED" and r.json()["balance"] == 3200.0

    def test_same_student_term_updates_amount(self, client, admin):
        r = client.post("/api/v1/admin/tuition", headers=admin,
                        json={"studentNo": "20230002", "term": "2026-FALL", "amount": 3500, "currency": "USD"})
        assert r.status_code == 200 and r.json()["status"] == "UPDATED"
        assert r.json()["balance"] == 2500.0  # 1000 already paid is preserved

    def test_amount_below_paid_rejected(self, client, admin):
        r = client.post("/api/v1/admin/tuition", headers=admin,
                        json={"studentNo": "20230003", "term": "2026-FALL", "amount": 100, "currency": "USD"})
        assert r.status_code == 409 and r.json()["error"]["code"] == "AMOUNT_BELOW_PAID"

    def test_currency_mismatch_rejected(self, client, admin):
        r = client.post("/api/v1/admin/tuition", headers=admin,
                        json={"studentNo": "20230001", "term": "2027-SPRING", "amount": 100, "currency": "TRY"})
        assert r.status_code == 409 and r.json()["error"]["code"] == "CURRENCY_MISMATCH"

    def test_non_admin_forbidden(self, client, bank):
        r = client.post("/api/v1/admin/tuition", headers=bank,
                        json={"studentNo": "20230001", "term": "2027-SPRING", "amount": 100, "currency": "USD"})
        assert r.status_code == 403

    def test_bad_term_format_422(self, client, admin):
        r = client.post("/api/v1/admin/tuition", headers=admin,
                        json={"studentNo": "20230001", "term": "fall", "amount": 100, "currency": "USD"})
        assert r.status_code == 422 and r.json()["error"]["code"] == "VALIDATION_ERROR"


# ============================================================ Admin: Add Tuition - Batch
def _upload(client, headers, text):
    return client.post("/api/v1/admin/tuition/batch", headers=headers,
                       files={"file": ("tuition.csv", text.encode(), "text/csv")})


class TestBatch:
    def test_mixed_file_reports_counts_and_keeps_good_rows(self, client, admin):
        csv_text = ("studentNo,term,amount,currency\n"
                    "20240001,2026-FALL,3000,USD\n"      # created
                    "20230002,2026-FALL,3100,USD\n"      # updated
                    "20240002,FALL,3000,USD\n"           # bad term
                    "20240003,2026-FALL,abc,USD\n"       # bad amount
                    "20230001,2026-FALL,3000,TRY\n"      # currency mismatch
                    "20240004,2026-FALL,2000,usd\n")     # created (currency case-insensitive)
        r = _upload(client, admin, csv_text)
        assert r.status_code == 200
        b = r.json()
        assert (b["status"], b["totalRows"], b["successCount"], b["failureCount"]) == ("COMPLETED_WITH_ERRORS", 6, 3, 3)
        assert [x["status"] for x in b["results"]] == ["CREATED", "UPDATED", "FAILED", "FAILED", "FAILED", "CREATED"]
        assert b["results"][2]["line"] == 4
        assert _upload(client, admin, "studentNo,term,amount,currency\n20240001,2026-FALL,3300,USD\n").json()["results"][0]["status"] == "UPDATED"

    def test_wrong_header_400(self, client, admin):
        r = _upload(client, admin, "id,semester,fee\n1,2,3\n")
        assert r.status_code == 400 and r.json()["error"]["code"] == "INVALID_CSV_HEADER"

    def test_requires_admin(self, client, student1):
        assert _upload(client, student1, "studentNo,term,amount,currency\n").status_code == 403


# ============================================================ Admin: Unpaid Tuition Status (paging)
class TestUnpaid:
    def test_paging(self, client, admin):
        r = client.get("/api/v1/admin/tuition/unpaid?term=2026-FALL&page=1&pageSize=5", headers=admin)
        assert r.status_code == 200
        b = r.json()
        # 25 demo students; 20230003 is fully paid -> 24 unpaid
        assert (b["totalItems"], b["totalPages"], len(b["items"])) == (24, 5, 5)
        assert b["items"][0]["studentNo"] == "20230001"
        last = client.get("/api/v1/admin/tuition/unpaid?term=2026-FALL&page=5&pageSize=5", headers=admin).json()
        assert len(last["items"]) == 4
        assert all(i["balance"] > 0 for i in last["items"])

    def test_page_beyond_end_is_empty(self, client, admin):
        b = client.get("/api/v1/admin/tuition/unpaid?term=2026-FALL&page=99&pageSize=10", headers=admin).json()
        assert b["items"] == [] and b["totalItems"] == 24

    def test_invalid_page_size_422(self, client, admin):
        assert client.get("/api/v1/admin/tuition/unpaid?term=2026-FALL&pageSize=1000", headers=admin).status_code == 422

    def test_requires_admin(self, client, student1):
        assert client.get("/api/v1/admin/tuition/unpaid?term=2026-FALL", headers=student1).status_code == 403
