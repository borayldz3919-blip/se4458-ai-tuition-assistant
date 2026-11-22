"""Business logic. Routers stay thin; everything testable lives here."""
import csv
import io
import math
import re
from datetime import date, datetime
from decimal import ROUND_HALF_UP, Decimal, InvalidOperation
from zoneinfo import ZoneInfo

from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from .config import settings
from .errors import AppError, bad_request, conflict, not_found
from .models import ApiUsage, Payment, Student, Tuition
from .schemas import CURRENCIES, STUDENT_NO_PATTERN, TERM_PATTERN

CENT = Decimal("0.01")
MOBILE_ENDPOINT = "mobile.query_tuition"


def money(v) -> Decimal:
    return Decimal(v).quantize(CENT, rounding=ROUND_HALF_UP)


def _f(v) -> float:
    return float(money(v))


def _status(t: Tuition) -> str:
    if t.balance <= 0:
        return "PAID"
    return "PARTIALLY_PAID" if t.paid_amount > 0 else "UNPAID"


def today_local() -> date:
    return datetime.now(ZoneInfo(settings.timezone)).date()


# --------------------------------------------------------------------------- Query Tuition
def query_tuition(db: Session, student_no: str, term: str | None = None) -> dict:
    student = db.get(Student, student_no)
    if student is None:
        raise not_found(f"Student {student_no} not found")
    stmt = select(Tuition).where(Tuition.student_no == student_no).order_by(Tuition.term)
    if term:
        stmt = stmt.where(Tuition.term == term)
    rows = list(db.scalars(stmt))
    if term and not rows:
        raise not_found(f"No tuition record for student {student_no} in term {term}")
    return {
        "student_no": student.student_no,
        "full_name": student.full_name,
        "currency": student.currency,
        "tuition_total": _f(sum((t.amount for t in rows), Decimal(0))),
        "balance": _f(sum((t.balance for t in rows), Decimal(0))),
        "scope": "TERM" if term else "OVERALL",
        "terms": [
            {
                "term": t.term,
                "tuition_total": _f(t.amount),
                "paid_amount": _f(t.paid_amount),
                "balance": _f(t.balance),
                "status": _status(t),
            }
            for t in rows
        ],
    }


def consume_mobile_quota(db: Session, student_no: str, limit: int | None = None) -> int:
    """Increment the per-student daily counter; raise 429 when the limit is exceeded.

    Keyed by student number (identity), NOT by client IP, as required. The gateway enforces the same
    rule; this is the authoritative, persistent counter. Returns remaining calls for today.
    """
    limit = settings.mobile_daily_limit if limit is None else limit
    day = today_local()
    key = (student_no, day, MOBILE_ENDPOINT)
    for _ in range(2):  # retry once on a concurrent first insert
        row = db.get(ApiUsage, key, with_for_update=True)
        if row is None:
            row = ApiUsage(student_no=student_no, usage_date=day, endpoint=MOBILE_ENDPOINT, call_count=0)
            db.add(row)
            try:
                db.flush()
            except IntegrityError:
                db.rollback()
                continue
        if row.call_count >= limit:
            db.rollback()
            raise AppError(
                429,
                "RATE_LIMIT_EXCEEDED",
                f"Query Tuition is limited to {limit} calls per student per day",
                headers={"Retry-After": "86400", "X-RateLimit-Limit": str(limit), "X-RateLimit-Remaining": "0"},
            )
        row.call_count += 1
        db.commit()
        return limit - row.call_count
    raise AppError(503, "TRY_AGAIN", "Please retry")


# --------------------------------------------------------------------------- Pay Tuition
def pay_tuition(db: Session, student_no: str, term: str, amount: Decimal, transaction_id: str, paid_by: str) -> dict:
    amount = money(amount)
    if amount <= 0:
        raise bad_request("INVALID_AMOUNT", "Amount must be greater than zero")
    if db.scalar(select(Payment.id).where(Payment.transaction_id == transaction_id)) is not None:
        raise conflict("DUPLICATE_TRANSACTION", f"Transaction {transaction_id} was already processed")

    tuition = db.scalar(
        select(Tuition).where(Tuition.student_no == student_no, Tuition.term == term).with_for_update()
    )
    if tuition is None:
        raise not_found(f"No tuition record for student {student_no} in term {term}")
    balance = tuition.balance
    if balance <= 0:
        raise conflict("ALREADY_PAID", f"Tuition for {term} is already fully paid")
    if amount > balance:
        raise AppError(
            422, "AMOUNT_EXCEEDS_BALANCE", f"Amount {amount} exceeds remaining balance {money(balance)}"
        )

    tuition.paid_amount = money(tuition.paid_amount + amount)
    db.add(Payment(transaction_id=transaction_id, tuition_id=tuition.id, student_no=student_no,
                   term=term, amount=amount, paid_by=paid_by))
    try:
        db.commit()
    except IntegrityError:  # concurrent duplicate transaction id
        db.rollback()
        raise conflict("DUPLICATE_TRANSACTION", f"Transaction {transaction_id} was already processed")
    return {
        "status": "SUCCESS",
        "transaction_id": transaction_id,
        "student_no": student_no,
        "term": term,
        "paid_amount": _f(amount),
        "remaining_balance": _f(tuition.balance),
        "currency": tuition.currency,
    }


# --------------------------------------------------------------------------- Add Tuition
def _upsert_tuition(db: Session, student_no: str, term: str, amount: Decimal, currency: str) -> tuple[str, Tuition]:
    """Duplicate-handling rules (documented in README):
    * (studentNo, term) is unique. A second Add Tuition for the same pair UPDATES the amount.
    * The new amount may not be lower than what was already paid (409 AMOUNT_BELOW_PAID).
    * A student has a single billing currency; a different currency is rejected (409 CURRENCY_MISMATCH).
    * Unknown students are created automatically.
    """
    amount = money(amount)
    if amount < 0:
        raise bad_request("INVALID_AMOUNT", "Amount cannot be negative")
    student = db.get(Student, student_no)
    if student is None:
        student = Student(student_no=student_no, full_name="", currency=currency)
        db.add(student)
        db.flush()
    elif student.currency != currency:
        raise conflict("CURRENCY_MISMATCH", f"Student {student_no} is billed in {student.currency}, not {currency}")

    tuition = db.scalar(select(Tuition).where(Tuition.student_no == student_no, Tuition.term == term))
    if tuition is None:
        tuition = Tuition(student_no=student_no, term=term, amount=amount, paid_amount=Decimal(0), currency=currency)
        db.add(tuition)
        db.flush()
        return "CREATED", tuition
    if amount < tuition.paid_amount:
        raise conflict("AMOUNT_BELOW_PAID",
                       f"New amount {amount} is lower than already paid {money(tuition.paid_amount)}")
    tuition.amount = amount
    db.flush()
    return "UPDATED", tuition


def add_tuition(db: Session, student_no: str, term: str, amount: Decimal, currency: str) -> dict:
    try:
        status, t = _upsert_tuition(db, student_no, term, amount, currency)
        db.commit()
    except AppError:
        db.rollback()
        raise
    return {"status": status, "student_no": student_no, "term": term,
            "tuition_total": _f(t.amount), "balance": _f(t.balance), "currency": t.currency}


BATCH_HEADER = ["studentNo", "term", "amount", "currency"]
MAX_BATCH_ROWS = 10_000


def add_tuition_batch(db: Session, csv_text: str) -> dict:
    """Each row is processed in its own savepoint: one bad row never rolls back the good ones.
    Rows are applied in file order, so a repeated (studentNo, term) later in the file updates the earlier one."""
    reader = csv.reader(io.StringIO(csv_text.lstrip("﻿")))
    header = next(reader, None)
    if header is None or [h.strip() for h in header] != BATCH_HEADER:
        raise bad_request("INVALID_CSV_HEADER", f"CSV header must be: {','.join(BATCH_HEADER)}")

    results, ok = [], 0
    for line_no, row in enumerate(reader, start=2):
        if not row or all(not c.strip() for c in row):
            continue
        if len(results) >= MAX_BATCH_ROWS:
            raise bad_request("BATCH_TOO_LARGE", f"At most {MAX_BATCH_ROWS} rows per file")
        cells = [c.strip() for c in row]
        sno = cells[0] if cells else None
        term = cells[1] if len(cells) > 1 else None
        res = {"line": line_no, "student_no": sno, "term": term}
        try:
            if len(cells) != 4:
                raise bad_request("INVALID_ROW", "Expected 4 columns")
            amount_s, currency = cells[2], cells[3].upper()
            if not re.match(STUDENT_NO_PATTERN, sno):
                raise bad_request("INVALID_STUDENT_NO", "studentNo must be 6-12 digits")
            if not re.match(TERM_PATTERN, term):
                raise bad_request("INVALID_TERM", "term must look like 2026-FALL")
            if currency not in CURRENCIES:
                raise bad_request("INVALID_CURRENCY", f"currency must be one of {', '.join(CURRENCIES)}")
            try:
                amount = Decimal(amount_s)
            except InvalidOperation:
                raise bad_request("INVALID_AMOUNT", "amount is not a number")
            if not amount.is_finite():
                raise bad_request("INVALID_AMOUNT", "amount is not a number")
            sp = db.begin_nested()
            try:
                status, _ = _upsert_tuition(db, sno, term, amount, currency)
                sp.commit()
            except Exception:
                sp.rollback()
                raise
            res.update(status=status, error=None)
            ok += 1
        except AppError as e:
            res.update(status="FAILED", error=f"{e.code}: {e.message}")
        results.append(res)
    db.commit()

    failed = len(results) - ok
    status = "COMPLETED" if failed == 0 else ("FAILED" if ok == 0 else "COMPLETED_WITH_ERRORS")
    return {"status": status, "total_rows": len(results), "success_count": ok,
            "failure_count": failed, "results": results}


# --------------------------------------------------------------------------- Unpaid Tuition Status
def unpaid_status(db: Session, term: str, page: int, page_size: int) -> dict:
    cond = (Tuition.term == term, Tuition.amount > Tuition.paid_amount)
    total = db.scalar(select(func.count()).select_from(Tuition).where(*cond)) or 0
    rows = db.execute(
        select(Tuition, Student.full_name)
        .join(Student, Student.student_no == Tuition.student_no)
        .where(*cond)
        .order_by(Tuition.student_no)
        .offset((page - 1) * page_size)
        .limit(page_size)
    ).all()
    return {
        "term": term,
        "page": page,
        "page_size": page_size,
        "total_items": total,
        "total_pages": math.ceil(total / page_size) if total else 0,
        "items": [
            {"student_no": t.student_no, "full_name": name, "tuition_total": _f(t.amount),
             "paid_amount": _f(t.paid_amount), "balance": _f(t.balance), "currency": t.currency}
            for t, name in rows
        ],
    }
