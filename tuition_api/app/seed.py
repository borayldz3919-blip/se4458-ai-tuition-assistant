"""Demo data so the instructor can reproduce every scenario. Idempotent."""
from decimal import Decimal

from sqlalchemy.orm import Session

from .models import AppUser, Student, Tuition
from .security import hash_password

CURRENT_TERM = "2026-FALL"

# student_no, name, [(term, amount, paid)]
DEMO_STUDENTS = [
    ("20230001", "Ayse Yilmaz", [("2026-SPRING", 2800, 2800), (CURRENT_TERM, 3000, 0)]),
    ("20230002", "Mehmet Demir", [(CURRENT_TERM, 3000, 1000)]),
    ("20230003", "Zeynep Kaya", [(CURRENT_TERM, 3000, 3000)]),
    ("20230004", "Can Ozturk", [(CURRENT_TERM, 2250, 0)]),  # 25% merit scholarship (Policy 5.1)
    ("20230005", "Elif Sahin", [("2026-SPRING", 2800, 2300), (CURRENT_TERM, 3000, 0)]),
] + [(f"202300{i:02d}", f"Demo Student {i}", [(CURRENT_TERM, 3000, 500 * (i % 3))]) for i in range(6, 26)]

DEMO_PASSWORDS = {"student": "Student123!", "bank": "Bank123!", "admin": "Admin123!"}


def seed(db: Session) -> None:
    if db.get(AppUser, "admin") is not None:
        return
    for sno, name, terms in DEMO_STUDENTS:
        db.add(Student(student_no=sno, full_name=name, currency="USD"))
        db.flush()
        for term, amount, paid in terms:
            db.add(Tuition(student_no=sno, term=term, amount=Decimal(amount), paid_amount=Decimal(paid), currency="USD"))
        db.add(AppUser(username=sno, password_hash=hash_password(DEMO_PASSWORDS["student"]), role="student",
                       student_no=sno))
    db.add(AppUser(username="bankapp", password_hash=hash_password(DEMO_PASSWORDS["bank"]), role="bank"))
    db.add(AppUser(username="admin", password_hash=hash_password(DEMO_PASSWORDS["admin"]), role="admin"))
    db.commit()
