"""Version 1 of the public API. A future breaking change goes into routers/v2.py under /api/v2."""
from fastapi import APIRouter, Depends, File, Path, Query, Response, UploadFile
from sqlalchemy.orm import Session

from .. import service
from ..db import get_db
from ..errors import AppError, bad_request
from ..models import AppUser
from ..schemas import (
    STUDENT_NO_PATTERN,
    TERM_PATTERN,
    AddTuitionRequest,
    AddTuitionResponse,
    BatchResponse,
    LoginRequest,
    PaymentRequest,
    PaymentResponse,
    TokenResponse,
    TuitionQueryResponse,
    UnpaidPage,
)
from ..security import CurrentUser, create_token, ensure_student_access, require_roles, verify_password

ERR = {"model": None, "description": "Error: {\"error\": {\"code\", \"message\"}}"}

StudentNo = Path(pattern=STUDENT_NO_PATTERN, examples=["20230001"])
TermFilter = Query(None, pattern=TERM_PATTERN, description="Optional. Omit for the OVERALL balance across all terms.")

# ----------------------------------------------------------------------------- auth
auth = APIRouter(prefix="/api/v1/auth", tags=["Auth"])


@auth.post("/login", response_model=TokenResponse, responses={401: ERR},
           summary="Get a JWT (students log in with their student number)")
def login(body: LoginRequest, db: Session = Depends(get_db)):
    user = db.get(AppUser, body.username)
    if user is None or not verify_password(body.password, user.password_hash):
        raise AppError(401, "INVALID_CREDENTIALS", "Wrong username or password")
    token, ttl = create_token(user.username, user.role, user.student_no)
    return TokenResponse(access_token=token, expires_in=ttl, role=user.role, student_no=user.student_no)


# ----------------------------------------------------------------------------- University Mobile App
mobile = APIRouter(prefix="/api/v1/mobile", tags=["University Mobile App"])


@mobile.get("/tuition/{student_no}", response_model=TuitionQueryResponse,
            responses={404: ERR, 429: ERR},
            summary="Query Tuition (no auth, max 3 calls per student per day)")
def mobile_query_tuition(response: Response, student_no: str = StudentNo, term: str | None = TermFilter,
                         db: Session = Depends(get_db)):
    remaining = service.consume_mobile_quota(db, student_no)
    response.headers["X-RateLimit-Limit"] = str(service.settings.mobile_daily_limit)
    response.headers["X-RateLimit-Remaining"] = str(remaining)
    return service.query_tuition(db, student_no, term)


# ----------------------------------------------------------------------------- Banking App
banking = APIRouter(prefix="/api/v1/banking", tags=["Banking App"])
bank_or_student = require_roles("bank", "student")


@banking.get("/tuition/{student_no}", response_model=TuitionQueryResponse, responses={401: ERR, 403: ERR, 404: ERR},
             summary="Query Tuition (auth required)")
def banking_query_tuition(student_no: str = StudentNo, term: str | None = TermFilter,
                          user: CurrentUser = Depends(bank_or_student), db: Session = Depends(get_db)):
    ensure_student_access(user, student_no)
    return service.query_tuition(db, student_no, term)


@banking.post("/payments", response_model=PaymentResponse, status_code=201,
              responses={401: ERR, 403: ERR, 404: ERR, 409: ERR, 422: ERR},
              summary="Pay Tuition (simulated partial or full payment)")
def pay_tuition(body: PaymentRequest, user: CurrentUser = Depends(bank_or_student), db: Session = Depends(get_db)):
    ensure_student_access(user, body.student_no)
    return service.pay_tuition(db, body.student_no, body.term, body.amount, body.transaction_id, user.username)


# ----------------------------------------------------------------------------- University Web Site - Admin
admin = APIRouter(prefix="/api/v1/admin", tags=["University Web Site - Admin"])
admin_only = require_roles("admin")


@admin.post("/tuition", response_model=AddTuitionResponse, responses={401: ERR, 403: ERR, 409: ERR},
            summary="Add Tuition (create, or update if the student/term already exists)")
def add_tuition(body: AddTuitionRequest, response: Response, _: CurrentUser = Depends(admin_only),
                db: Session = Depends(get_db)):
    result = service.add_tuition(db, body.student_no, body.term, body.amount, body.currency)
    response.status_code = 201 if result["status"] == "CREATED" else 200
    return result


@admin.post("/tuition/batch", response_model=BatchResponse, responses={400: ERR, 401: ERR, 403: ERR},
            summary="Add Tuition - Batch (CSV: studentNo,term,amount,currency)")
async def add_tuition_batch(file: UploadFile = File(...), _: CurrentUser = Depends(admin_only),
                            db: Session = Depends(get_db)):
    raw = await file.read()
    if len(raw) > 5 * 1024 * 1024:
        raise bad_request("FILE_TOO_LARGE", "CSV must be smaller than 5 MB")
    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError:
        raise bad_request("INVALID_ENCODING", "CSV must be UTF-8")
    return service.add_tuition_batch(db, text)


@admin.get("/tuition/unpaid", response_model=UnpaidPage, responses={401: ERR, 403: ERR},
           summary="Unpaid Tuition Status (paged)")
def unpaid(term: str = Query(pattern=TERM_PATTERN, examples=["2026-FALL"]),
           page: int = Query(1, ge=1), page_size: int = Query(10, ge=1, le=100, alias="pageSize"),
           _: CurrentUser = Depends(admin_only), db: Session = Depends(get_db)):
    return service.unpaid_status(db, term, page, page_size)


routers = [auth, mobile, banking, admin]
