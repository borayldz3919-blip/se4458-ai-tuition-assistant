"""Runtime configuration, read from environment variables (12-factor style)."""
import os


def _bool(name: str, default: str) -> bool:
    return os.getenv(name, default).strip().lower() in ("1", "true", "yes")


class Settings:
    def __init__(self) -> None:
        self.database_url = os.getenv(
            "DATABASE_URL", "postgresql+psycopg://tuition:tuition@localhost:5432/tuition"
        )
        # HS256 shared secret. The same secret is configured in the API Gateway (Kong JWT plugin)
        self.jwt_secret = os.getenv("JWT_SECRET", "dev-only-secret-change-me-0123456789abcdef")
        # `iss` claim. Kong uses it to find the matching JWT credential.
        self.jwt_issuer = os.getenv("JWT_ISSUER", "tuition-auth")
        self.jwt_ttl_minutes = int(os.getenv("JWT_TTL_MINUTES", "60"))
        self.mobile_daily_limit = int(os.getenv("MOBILE_DAILY_LIMIT", "3"))
        self.timezone = os.getenv("APP_TIMEZONE", "Europe/Istanbul")
        self.init_db = _bool("INIT_DB", "true")
        self.seed_demo_data = _bool("SEED_DEMO_DATA", "true")


settings = Settings()
