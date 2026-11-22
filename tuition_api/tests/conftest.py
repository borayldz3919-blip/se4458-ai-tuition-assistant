"""Unit-test setup. No cloud, no gateway, no LLM needed.

By default tests run against an in-memory SQLite DB (fast, zero setup). To run the exact same tests
against PostgreSQL set TEST_DATABASE_URL, e.g.
    TEST_DATABASE_URL=postgresql+psycopg://tuition:tuition@localhost:5432/tuition_test pytest
"""
import os
import sys
from pathlib import Path

os.environ["INIT_DB"] = "false"  # the app must not touch the real DB on startup
os.environ["PASSWORD_HASH_ROUNDS"] = "1000"  # fast hashing for tests
os.environ["MOBILE_DAILY_LIMIT"] = "3"
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import pytest  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402
from sqlalchemy import create_engine  # noqa: E402
from sqlalchemy.orm import sessionmaker  # noqa: E402
from sqlalchemy.pool import StaticPool  # noqa: E402

from app.db import Base, get_db  # noqa: E402
from app.main import create_app  # noqa: E402
from app.seed import seed  # noqa: E402


def _ensure_database(url: str) -> None:
    """Create the (separate) test database on the PostgreSQL server if it does not exist yet."""
    from sqlalchemy import text
    from sqlalchemy.engine import make_url

    u = make_url(url)
    admin = create_engine(u.set(database="postgres"), isolation_level="AUTOCOMMIT")
    with admin.connect() as c:
        if not c.execute(text("SELECT 1 FROM pg_database WHERE datname = :d"), {"d": u.database}).scalar():
            c.execute(text(f'CREATE DATABASE "{u.database}"'))
    admin.dispose()


@pytest.fixture(scope="session", autouse=True)
def _test_database():
    if os.getenv("TEST_DATABASE_URL"):
        _ensure_database(os.environ["TEST_DATABASE_URL"])


@pytest.fixture
def engine():
    url = os.getenv("TEST_DATABASE_URL")
    if url:
        eng = create_engine(url)
        Base.metadata.drop_all(eng)
    else:
        eng = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    Base.metadata.create_all(eng)
    yield eng
    Base.metadata.drop_all(eng)
    eng.dispose()


@pytest.fixture
def db(engine):
    factory = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)
    with factory() as s:
        seed(s)
    with factory() as s:
        yield s


@pytest.fixture
def client(engine, db):
    factory = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)
    app = create_app()

    def _get_db():
        s = factory()
        try:
            yield s
        finally:
            s.close()

    app.dependency_overrides[get_db] = _get_db
    with TestClient(app) as c:
        yield c


def _login(client, username, password):
    r = client.post("/api/v1/auth/login", json={"username": username, "password": password})
    assert r.status_code == 200, r.text
    return {"Authorization": f"Bearer {r.json()['accessToken']}"}


@pytest.fixture
def student1(client):
    return _login(client, "20230001", "Student123!")


@pytest.fixture
def bank(client):
    return _login(client, "bankapp", "Bank123!")


@pytest.fixture
def admin(client):
    return _login(client, "admin", "Admin123!")
