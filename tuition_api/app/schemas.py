"""Request/response contracts (they appear in Swagger UI / openapi.json)."""
from decimal import Decimal
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field
from pydantic.alias_generators import to_camel

TERM_PATTERN = r"^\d{4}-(FALL|SPRING|SUMMER)$"
STUDENT_NO_PATTERN = r"^\d{6,12}$"
CURRENCIES = ("USD", "EUR", "TRY")


class ApiModel(BaseModel):
    """camelCase on the wire, snake_case in Python."""

    model_config = ConfigDict(alias_generator=to_camel, populate_by_name=True)


# ---------- auth ----------
class LoginRequest(ApiModel):
    username: str = Field(examples=["20230001"])
    password: str = Field(examples=["Student123!"])


class TokenResponse(ApiModel):
    access_token: str
    token_type: str = "Bearer"
    expires_in: int
    role: str
    student_no: str | None = None


# ---------- query tuition ----------
class TermTuition(ApiModel):
    term: str
    tuition_total: float
    paid_amount: float
    balance: float
    status: Literal["PAID", "PARTIALLY_PAID", "UNPAID"]


class TuitionQueryResponse(ApiModel):
    """`tuitionTotal` and `balance` are OVERALL figures across all terms (or for `term` if the
    `term` query parameter is given). `terms` always contains the per-term breakdown."""

    student_no: str
    full_name: str
    currency: str
    tuition_total: float
    balance: float
    scope: Literal["OVERALL", "TERM"]
    terms: list[TermTuition]


# ---------- pay tuition ----------
class PaymentRequest(ApiModel):
    student_no: str = Field(pattern=STUDENT_NO_PATTERN, examples=["20230001"])
    term: str = Field(pattern=TERM_PATTERN, examples=["2026-FALL"])
    amount: Decimal = Field(gt=0, max_digits=12, decimal_places=2, examples=[1000])
    transaction_id: str = Field(min_length=4, max_length=64, examples=["TX-20261121-0001"])


class PaymentResponse(ApiModel):
    status: Literal["SUCCESS"]
    transaction_id: str
    student_no: str
    term: str
    paid_amount: float
    remaining_balance: float
    currency: str


# ---------- add tuition ----------
class AddTuitionRequest(ApiModel):
    student_no: str = Field(pattern=STUDENT_NO_PATTERN, examples=["20230001"])
    term: str = Field(pattern=TERM_PATTERN, examples=["2026-FALL"])
    amount: Decimal = Field(ge=0, max_digits=12, decimal_places=2, examples=[3000])
    currency: Literal["USD", "EUR", "TRY"] = Field(examples=["USD"])


class AddTuitionResponse(ApiModel):
    status: Literal["CREATED", "UPDATED"]
    student_no: str
    term: str
    tuition_total: float
    balance: float
    currency: str


class BatchRowResult(ApiModel):
    line: int
    student_no: str | None
    term: str | None
    status: Literal["CREATED", "UPDATED", "FAILED"]
    error: str | None = None


class BatchResponse(ApiModel):
    status: Literal["COMPLETED", "COMPLETED_WITH_ERRORS", "FAILED"]
    total_rows: int
    success_count: int
    failure_count: int
    results: list[BatchRowResult]


# ---------- unpaid status ----------
class UnpaidItem(ApiModel):
    student_no: str
    full_name: str
    tuition_total: float
    paid_amount: float
    balance: float
    currency: str


class UnpaidPage(ApiModel):
    term: str
    page: int
    page_size: int
    total_items: int
    total_pages: int
    items: list[UnpaidItem]
