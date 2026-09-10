from __future__ import annotations

from pathlib import Path

from fastapi.testclient import TestClient

from harmony_test_agent.api import create_app
from harmony_test_agent.config import Settings


def _settings(tmp_path: Path) -> Settings:
    return Settings(
        _env_file=None,
        runtime_dir=tmp_path / "runs",
        database_path=tmp_path / "agent.db",
        target_profile_path=tmp_path / "profile.json",
        runtime_home=tmp_path / "runtime-home",
        agent_provider="mock",
    )


def test_live_health_endpoint_is_lightweight_and_ready(tmp_path: Path) -> None:
    with TestClient(create_app(_settings(tmp_path))) as client:
        response = client.get("/api/health/live")

    assert response.status_code == 200
    assert response.json() == {"status": "ok"}


def test_cors_uses_origin_from_environment(
    tmp_path: Path,
    monkeypatch,
) -> None:
    allowed_origin = "http://127.0.0.1:15432"
    monkeypatch.setenv("HARMONY_CORS_ORIGINS", allowed_origin)
    app = create_app(_settings(tmp_path))

    with TestClient(app) as client:
        allowed = client.options(
            "/api/health/live",
            headers={
                "Origin": allowed_origin,
                "Access-Control-Request-Method": "GET",
            },
        )
        rejected = client.options(
            "/api/health/live",
            headers={
                "Origin": "http://127.0.0.1:59999",
                "Access-Control-Request-Method": "GET",
            },
        )

    assert allowed.status_code == 200
    assert allowed.headers["access-control-allow-origin"] == allowed_origin
    assert rejected.status_code == 400
    assert "access-control-allow-origin" not in rejected.headers
