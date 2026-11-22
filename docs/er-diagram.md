# ER / Data Model

PostgreSQL 16 (Azure Database for PostgreSQL Flexible Server in the cloud, `postgres:16-alpine` locally).
Tables are created by SQLAlchemy at API start-up (`tuition_api/app/models.py`).

```mermaid
erDiagram
    STUDENTS ||--o{ TUITIONS : "is billed"
    TUITIONS ||--o{ PAYMENTS : "receives"
    STUDENTS ||--o| APP_USERS : "logs in as"
    STUDENTS ||--o{ API_USAGE : "rate-limit counter"

    STUDENTS {
        varchar student_no PK
        varchar full_name
        char3 currency "single billing currency per student"
    }
    TUITIONS {
        int id PK
        varchar student_no FK
        varchar term "e.g. 2026-FALL"
        numeric amount "12,2  >= 0"
        numeric paid_amount "12,2  0 <= paid <= amount"
        char3 currency
        timestamptz created_at
        timestamptz updated_at
    }
    PAYMENTS {
        int id PK
        varchar transaction_id UK "idempotency key, duplicates rejected"
        int tuition_id FK
        varchar student_no
        varchar term
        numeric amount "> 0"
        varchar paid_by "username from JWT"
        timestamptz created_at
    }
    APP_USERS {
        varchar username PK
        varchar password_hash "PBKDF2-SHA256"
        varchar role "student | bank | admin"
        varchar student_no FK "only for students"
    }
    API_USAGE {
        varchar student_no PK
        date usage_date PK "Europe/Istanbul day"
        varchar endpoint PK
        int call_count
    }
```

Constraints
- `UNIQUE (student_no, term)` on `tuitions`: one tuition record per student per term (Add Tuition upserts).
- `UNIQUE (transaction_id)` on `payments`: duplicate transaction IDs are rejected (HTTP 409).
- `CHECK (paid_amount <= amount)`: a payment can never exceed the balance, even under concurrency
  (the API also locks the tuition row with `SELECT ... FOR UPDATE`).
- Balance is derived: `amount - paid_amount` (never stored twice).
