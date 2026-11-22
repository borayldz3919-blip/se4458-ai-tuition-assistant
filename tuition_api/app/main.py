import time
from contextlib import asynccontextmanager

from fastapi import FastAPI
from sqlalchemy.exc import OperationalError

from .config import settings
from .db import Base, get_engine, get_session_factory
from .errors import install_error_handlers
from .routers.v1 import routers
from .seed import seed

DESCRIPTION = """
Tuition REST API used by the University Mobile App, the Banking App, the University admin web site
and the AI Tuition Assistant.

* All routes are **versioned** (`/api/v1/...`) and are exposed to clients only through the **API Gateway**.
* Protected routes need `Authorization: Bearer <JWT>` from `POST /api/v1/auth/login`.
* Errors always look like `{"error": {"code": "...", "message": "..."}}`.
"""


def _wait_for_db(attempts: int = 30) -> None:
    """Cloud databases may still be starting when the container boots: retry instead of crashing."""
    for i in range(attempts):
        try:
            with get_engine().connect():
                return
        except OperationalError:
            if i == attempts - 1:
                raise
            time.sleep(2)


@asynccontextmanager
async def lifespan(_: FastAPI):
    if settings.init_db:
        _wait_for_db()
        Base.metadata.create_all(get_engine())
        if settings.seed_demo_data:
            with get_session_factory()() as db:
                seed(db)
    yield


def create_app() -> FastAPI:
    app = FastAPI(title="Tuition API", version="1.0.0", description=DESCRIPTION, lifespan=lifespan,
                  docs_url="/docs", openapi_url="/openapi.json")
    install_error_handlers(app)
    for r in routers:
        app.include_router(r)

    @app.get("/health", tags=["Ops"])
    def health():
        return {"status": "ok"}

    return app


app = create_app()
