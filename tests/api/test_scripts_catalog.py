"""统一脚本目录（ScriptCatalog）与 /api/scripts 契约测试。"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient

from harmony_test_agent.api.app import create_app
from harmony_test_agent.config import Settings
from harmony_test_agent.storage.script_catalog import ScriptCatalog


def write_script(
    runtime_dir: Path,
    run_id: str,
    filename: str,
    *,
    config: dict[str, Any] | None = None,
    metadata: dict[str, Any] | None = None,
    text: str = "print('hi')\n",
) -> Path:
    generated = runtime_dir / run_id / "generated"
    generated.mkdir(parents=True, exist_ok=True)
    script = generated / filename
    script.write_text(text, encoding="utf-8")
    if config is not None:
        script.with_suffix(".json").write_text(json.dumps(config), encoding="utf-8")
    if metadata is not None:
        (generated / "generation_metadata.json").write_text(json.dumps(metadata), encoding="utf-8")
    return script


class TestScriptCatalog:
    def test_lists_live_and_dc_scripts_with_metadata(self, tmp_path: Path) -> None:
        runtime_dir = tmp_path / "runs"
        write_script(
            runtime_dir,
            "run-20260101T000000Z-aaaa1111",
            "test_run.py",
            config={
                "case_id": "run_case",
                "bundle_name": "com.example.app",
                "purpose": "acceptance",
                "replay_eligible": True,
            },
            metadata={"included_action_count": 7, "omitted_action_count": 2, "incomplete_reasons": []},
        )
        write_script(
            runtime_dir,
            "dc-20260102T000000Z-bbbb2222",
            "dc_test_dc.py",
            config={
                "case_id": "dc_case",
                "bundle_name": "com.example.app",
                "purpose": "dc_recording",
                "replay_eligible": False,
                "included_operations": 5,
                "omitted_operations": 10,
                "warnings": ["swipe direction inferred as LEFT"],
            },
        )

        entries = ScriptCatalog(runtime_dir).list_entries()
        by_id = {entry.script_id: entry for entry in entries}

        assert len(entries) == 2
        live = by_id["run-20260101T000000Z-aaaa1111/generated/test_run.py"]
        assert live.source == "run"
        assert live.case_id == "run_case"
        assert live.replay_eligible is True
        assert live.included_actions == 7
        assert live.omitted_actions == 2
        assert live.diagnostic is False

        dc = by_id["dc-20260102T000000Z-bbbb2222/generated/dc_test_dc.py"]
        assert dc.source == "dc"
        assert dc.replay_eligible is False
        assert dc.diagnostic is True
        assert dc.included_actions == 5
        assert dc.omitted_actions == 10
        assert dc.warnings == ["swipe direction inferred as LEFT"]
        assert dc.size_bytes > 0

    def test_skips_non_python_and_private_files(self, tmp_path: Path) -> None:
        runtime_dir = tmp_path / "runs"
        script = write_script(runtime_dir, "run-1", "test_a.py")
        script.parent.joinpath("_private.py").write_text("x", encoding="utf-8")
        script.parent.joinpath("notes.txt").write_text("x", encoding="utf-8")

        entries = ScriptCatalog(runtime_dir).list_entries()

        assert [entry.filename for entry in entries] == ["test_a.py"]

    def test_missing_runtime_dir_lists_nothing(self, tmp_path: Path) -> None:
        assert ScriptCatalog(tmp_path / "absent").list_entries() == []

    def test_read_returns_source_and_config(self, tmp_path: Path) -> None:
        runtime_dir = tmp_path / "runs"
        write_script(runtime_dir, "dc-1", "dc_test_dc_1.py", config={"case_id": "dc_case"}, text="print('dc')\n")
        catalog = ScriptCatalog(runtime_dir)

        entry, python_text, config = catalog.read("dc-1/generated/dc_test_dc_1.py")

        assert entry.case_id == "dc_case"
        assert python_text == "print('dc')\n"
        assert config["case_id"] == "dc_case"

    @pytest.mark.parametrize(
        "script_id",
        [
            "../../etc/passwd",
            "dc-1/generated/../../../outside.py",
            "/absolute/path.py",
            "dc-1/generated/missing.py",
            "",
        ],
    )
    def test_traversal_and_missing_ids_are_rejected(self, tmp_path: Path, script_id: str) -> None:
        runtime_dir = tmp_path / "runs"
        write_script(runtime_dir, "dc-1", "dc_test_dc_1.py")
        catalog = ScriptCatalog(runtime_dir)

        with pytest.raises(ValueError):
            catalog.resolve(script_id)

    def test_unreadable_config_is_tolerated(self, tmp_path: Path) -> None:
        runtime_dir = tmp_path / "runs"
        script = write_script(runtime_dir, "dc-1", "dc_test_dc_1.py")
        script.with_suffix(".json").write_text("{broken", encoding="utf-8")

        entries = ScriptCatalog(runtime_dir).list_entries()

        assert len(entries) == 1
        assert entries[0].case_id is None
        assert entries[0].replay_eligible is False


@pytest.fixture
def client(tmp_path: Path) -> TestClient:
    settings = Settings(
        runtime_dir=tmp_path / "runs",
        database_path=tmp_path / "agent.db",
        target_profile_path=None,
        profiles_dir=tmp_path / "profiles",
        runtime_home=tmp_path / "runtime-home",
        agent_provider="mock",
    )
    return TestClient(create_app(settings), raise_server_exceptions=False)


class TestScriptsEndpoint:
    def test_list_and_detail(self, client: TestClient, tmp_path: Path) -> None:
        runtime_dir = tmp_path / "runs"
        write_script(
            runtime_dir,
            "dc-20260102T000000Z-bbbb2222",
            "dc_test_dc.py",
            config={"case_id": "dc_case", "replay_eligible": False},
            text="print('dc')\n",
        )

        listed = client.get("/api/scripts")
        assert listed.status_code == 200
        payload = listed.json()
        assert [item["script_id"] for item in payload] == ["dc-20260102T000000Z-bbbb2222/generated/dc_test_dc.py"]

        detail = client.get("/api/scripts/dc-20260102T000000Z-bbbb2222/generated/dc_test_dc.py")
        assert detail.status_code == 200
        body = detail.json()
        assert body["python"] == "print('dc')\n"
        assert body["entry"]["case_id"] == "dc_case"
        assert body["config"]["case_id"] == "dc_case"

    def test_empty_catalog_returns_empty_list(self, client: TestClient) -> None:
        response = client.get("/api/scripts")
        assert response.status_code == 200
        assert response.json() == []

    def test_unknown_script_returns_404(self, client: TestClient) -> None:
        assert client.get("/api/scripts/dc-absent/generated/nope.py").status_code == 404

    def test_traversal_is_rejected(self, client: TestClient) -> None:
        assert client.get("/api/scripts/..%2F..%2Fetc%2Fpasswd").status_code == 404
        assert client.get("/api/scripts/../../etc/passwd").status_code in (404, 400)
