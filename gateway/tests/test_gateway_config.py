"""Static checks of the Kong declarative config (Kong itself is exercised by scripts/smoke_test.sh)."""
import re
from pathlib import Path

import yaml

TEMPLATE = Path(__file__).resolve().parents[1] / "kong.yml.template"


def render(**env):
    text = TEMPLATE.read_text()
    for k, v in {"API_UPSTREAM": "http://api:8000", "JWT_SECRET": "s3cret", "JWT_ISSUER": "tuition-auth",
                 "MOBILE_DAILY_LIMIT": "3", "GATEWAY_SHARED_SECRET": "gw-secret", **env}.items():
        text = text.replace("${" + k + "}", v)
    assert not re.findall(r"\$\{[A-Z_]+\}", text)
    return yaml.safe_load(text)


def routes():
    return {r["name"]: r for r in render()["services"][0]["routes"]}


def plugins(route):
    return {p["name"]: p.get("config", {}) for p in route.get("plugins", [])}


def test_all_public_routes_are_versioned():
    for r in routes().values():
        for p in r["paths"]:
            assert p.startswith("/api/v1/") or p in ("/docs", "/openapi.json")


def test_protected_routes_require_jwt():
    for name in ("banking-v1", "admin-v1"):
        cfg = plugins(routes()[name])["jwt"]
        assert cfg["key_claim_name"] == "iss" and "exp" in cfg["claims_to_verify"]


def test_public_routes_have_no_jwt():
    assert "jwt" not in plugins(routes()["auth-v1"])
    assert "jwt" not in plugins(routes()["mobile-tuition-v1"])


def test_mobile_rate_limit_is_keyed_by_student_number_not_ip():
    p = plugins(routes()["mobile-tuition-v1"])
    assert p["rate-limiting"]["day"] == 3
    assert p["rate-limiting"]["limit_by"] == "header" and p["rate-limiting"]["header_name"] == "X-Student-No"
    lua = p["pre-function"]["access"][0]
    assert "X-Student-No" in lua and "/api/v1/mobile/tuition/" in lua


def test_jwt_consumer_matches_api_issuer():
    secret = render()["consumers"][0]["jwt_secrets"][0]
    assert secret == {"key": "tuition-auth", "secret": "s3cret", "algorithm": "HS256"}


def test_gateway_adds_shared_secret_and_strips_client_value():
    rt = {p["name"]: p["config"] for p in render()["plugins"]}["request-transformer"]
    assert rt["remove"]["headers"] == ["X-Gateway-Secret"]
    assert rt["add"]["headers"] == ["X-Gateway-Secret:gw-secret"]
