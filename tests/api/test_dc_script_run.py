"""POST /api/dc/scripts/run：直流脚本诊断启动契约测试。"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from harmony_test_agent.api.app import create_app
from harmony_test_agent.config import Settings

DC_RUN_ID = "dc-20260101T000000Z-aaaa1111"

PASSING_SCRIPT = """\
import json
import os
from pathlib import Path

report = Path(os.environ["HARMONY_AGENT_REPORT_DIR"])
(report / "generated_result.json").write_text(json.dumps({"passed": True}), encoding="utf-8")
print("ok")
"""


def write_dc_script(runtime_dir: Path, name: str = "dc_test_dc_1.py", body: str = PASSING_SCRIPT) -> Path:
    generated = runtime_dir / DC_RUN_ID / "generated"
    generated.mkdir(parents=True, exist_ok=True)
    script = generated / name
    script.write_text(body, encoding="utf-8")
    script.with_suffix(".json").write_text(
        json.dumps({"purpose": "dc_recording", "replay_eligible": False}), encoding="utf-8"
    )
    return script


@pytest.fixture
def settings(tmp_path: Path) -> Settings:
    return Settings(
        runtime_dir=tmp_path / "runs",
        database_path=tmp_path / "agent.db",
        target_profile_path=None,
        profiles_dir=tmp_path / "profiles",
        runtime_home=tmp_path / "runtime-home",
        agent_provider="mock",
    )


@pytest.fixture
def client(settings: Settings) -> TestClient:
    return TestClient(create_app(settings), raise_server_exceptions=False)


class TestRunDiagnosticScript:
    def test_runs_dc_script_and_returns_results(self, client: TestClient, settings: Settings) -> None:
        script = write_dc_script(settings.resolved_runtime_dir)
        script_id = f"{DC_RUN_ID}/generated/{script.name}"

        response = client.post("/api/dc/scripts/run", json={"script_id": script_id})

        assert response.status_code == 200, response.text
        body = response.json()
        assert body["session_id"] == DC_RUN_ID
        assert len(body["results"]) == 1
        result = body["results"][0]
        assert result["status"] == "passed"
        assert result["passed"] is True
        assert "hypium/attempt-01/stdout.log" in result["evidence_paths"]
        # 证据落在会话目录内
        assert (settings.resolved_runtime_dir / DC_RUN_ID / "hypium" / "attempt-01" / "stdout.log").is_file()

    def test_multiple_attempts_are_isolated(self, client: TestClient, settings: Settings) -> None:
        script = write_dc_script(settings.resolved_runtime_dir)
        script_id = f"{DC_RUN_ID}/generated/{script.name}"

        response = client.post("/api/dc/scripts/run", json={"script_id": script_id, "attempts": 2})

        assert response.status_code == 200
        results = response.json()["results"]
        assert [item["attempt"] for item in results] == [1, 2]
        assert all(item["passed"] for item in results)

    def test_missing_script_returns_404(self, client: TestClient) -> None:
        response = client.post("/api/dc/scripts/run", json={"script_id": f"{DC_RUN_ID}/generated/absent.py"})

        assert response.status_code == 404

    def test_non_dc_script_is_refused(self, client: TestClient, settings: Settings) -> None:
        run_dir = settings.resolved_runtime_dir / "run-20260101T000000Z-bbbb2222" / "generated"
        run_dir.mkdir(parents=True, exist_ok=True)
        (run_dir / "test_run.py").write_text("print('live')\n", encoding="utf-8")

        response = client.post(
            "/api/dc/scripts/run", json={"script_id": "run-20260101T000000Z-bbbb2222/generated/test_run.py"}
        )

        assert response.status_code == 400

    @pytest.mark.parametrize("script_id", ["../outside.py", f"{DC_RUN_ID}/../evil.py"])
    def test_traversal_is_refused(self, client: TestClient, script_id: str) -> None:
        response = client.post("/api/dc/scripts/run", json={"script_id": script_id})

        assert response.status_code == 400

    def test_attempts_out_of_range_is_422(self, client: TestClient, settings: Settings) -> None:
        script = write_dc_script(settings.resolved_runtime_dir)
        script_id = f"{DC_RUN_ID}/generated/{script.name}"

        response = client.post("/api/dc/scripts/run", json={"script_id": script_id, "attempts": 9})

        assert response.status_code == 422
