"""Database model (see docs/er-diagram.md)."""
from datetime import date, datetime, timezone
from decimal import Decimal

from sqlalchemy import (
    CheckConstraint,
    Date,
    DateTime,
    ForeignKey,
    Integer,
    Numeric,
    String,
    UniqueConstraint,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from .db import Base


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


class Student(Base):
    __tablename__ = "students"

    student_no: Mapped[str] = mapped_column(String(20), primary_key=True)
    full_name: Mapped[str] = mapped_column(String(120), default="")
    currency: Mapped[str] = mapped_column(String(3), default="USD")

    tuitions: Mapped[list["Tuition"]] = relationship(back_populates="student")


class Tuition(Base):
    """Tuition fee of one student for one term. Balance = amount - paid_amount."""

    __tablename__ = "tuitions"
    __table_args__ = (
        UniqueConstraint("student_no", "term", name="uq_tuition_student_term"),
        CheckConstraint("amount >= 0", name="ck_tuition_amount_nonneg"),
        CheckConstraint("paid_amount >= 0", name="ck_tuition_paid_nonneg"),
        CheckConstraint("paid_amount <= amount", name="ck_tuition_paid_le_amount"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    student_no: Mapped[str] = mapped_column(ForeignKey("students.student_no"), index=True)
    term: Mapped[str] = mapped_column(String(20), index=True)
    amount: Mapped[Decimal] = mapped_column(Numeric(12, 2))
    paid_amount: Mapped[Decimal] = mapped_column(Numeric(12, 2), default=Decimal("0"))
    currency: Mapped[str] = mapped_column(String(3))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, onupdate=utcnow)

    student: Mapped[Student] = relationship(back_populates="tuitions")
    payments: Mapped[list["Payment"]] = relationship(back_populates="tuition")

    @property
    def balance(self) -> Decimal:
        return Decimal(self.amount) - Decimal(self.paid_amount)


class Payment(Base):
    """Simulated payment. transaction_id is globally unique -> duplicates are rejected."""

    __tablename__ = "payments"
    __table_args__ = (CheckConstraint("amount > 0", name="ck_payment_amount_pos"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    transaction_id: Mapped[str] = mapped_column(String(64), unique=True, index=True)
    tuition_id: Mapped[int] = mapped_column(ForeignKey("tuitions.id"), index=True)
    student_no: Mapped[str] = mapped_column(String(20), index=True)
    term: Mapped[str] = mapped_column(String(20))
    amount: Mapped[Decimal] = mapped_column(Numeric(12, 2))
    paid_by: Mapped[str] = mapped_column(String(64))  # username from the JWT
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)

    tuition: Mapped[Tuition] = relationship(back_populates="payments")


class AppUser(Base):
    """Login accounts. role: student | bank | admin. Students are linked to their student_no."""

    __tablename__ = "app_users"

    username: Mapped[str] = mapped_column(String(64), primary_key=True)
    password_hash: Mapped[str] = mapped_column(String(256))
    role: Mapped[str] = mapped_column(String(16))
    student_no: Mapped[str | None] = mapped_column(ForeignKey("students.student_no"), nullable=True)


class ApiUsage(Base):
    """Per-student daily call counter for the rate-limited mobile Query Tuition endpoint."""

    __tablename__ = "api_usage"

    student_no: Mapped[str] = mapped_column(String(20), primary_key=True)
    usage_date: Mapped[date] = mapped_column(Date, primary_key=True)
    endpoint: Mapped[str] = mapped_column(String(64), primary_key=True)
    call_count: Mapped[int] = mapped_column(Integer, default=0)
