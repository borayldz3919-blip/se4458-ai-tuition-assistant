"""Uniform error format: {"error": {"code": "...", "message": "..."}}"""
from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse


class AppError(Exception):
    def __init__(self, status: int, code: str, message: str, headers: dict | None = None):
        self.status, self.code, self.message, self.headers = status, code, message, headers


def not_found(msg: str) -> AppError:
    return AppError(404, "NOT_FOUND", msg)


def forbidden(msg: str = "You are not allowed to access this resource") -> AppError:
    return AppError(403, "FORBIDDEN", msg)


def conflict(code: str, msg: str) -> AppError:
    return AppError(409, code, msg)


def bad_request(code: str, msg: str) -> AppError:
    return AppError(400, code, msg)


def install_error_handlers(app: FastAPI) -> None:
    @app.exception_handler(AppError)
    async def _app_error(_: Request, exc: AppError):
        return JSONResponse(
            status_code=exc.status,
            content={"error": {"code": exc.code, "message": exc.message}},
            headers=exc.headers,
        )

    @app.exception_handler(RequestValidationError)
    async def _validation(_: Request, exc: RequestValidationError):
        details = [
            {"field": ".".join(str(p) for p in e["loc"] if p != "body"), "message": e["msg"]}
            for e in exc.errors()
        ]
        return JSONResponse(
            status_code=422,
            content={"error": {"code": "VALIDATION_ERROR", "message": "Invalid request", "details": details}},
        )
