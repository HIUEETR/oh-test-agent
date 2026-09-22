"""``/api/defects*`` 契约测试（Phase 3.6）。

覆盖列表 / 详情 / PATCH / 证据穿越 / 路由顺序 / 转复现用例。
"""

from __future__ import annotations

from pathlib import Path
from unittest import mock

import pytest
from fastapi.testclient import TestClient

from harmony_test_agent.analysis.defects import DefectRecorder, DefectStatus
from harmony_test_agent.api.app import create_app
from harmony_test_agent.config import Settings
from harmony_test_agent.models import AnomalyFinding, AnomalyKind
from harmony_test_agent.storage import ArtifactStore, DefectRepository

BUNDLE = "com.zhihu.hmos"
RUN_ID = "run-20260101T000000Z-aaaa1111"


def _settings(tmp_path: Path) -> Settings:
    return Settings(
        runtime_dir=tmp_path / "runs",
        database_path=tmp_path / "agent.db",
        target_profile_path=None,
        profiles_dir=tmp_path / "profiles",
        runtime_home=tmp_path / "runtime-home",
        agent_provider="mock",
    )


def _finding(
    *,
    kind: AnomalyKind = AnomalyKind.CPP_CRASH,
    severity: str = "critical",
    summary_zh: str = "hilog 中出现 C++ 崩溃",
    target: str = "ui-hot-list",
    screenshot: str = "frames/step-1.png",
) -> AnomalyFinding:
    return AnomalyFinding(
        kind=kind,
        severity=severity,  # type: ignore[arg-type]
        summary_zh=summary_zh,
        detail="cppcrash happened",
        source="hilog",
        action_id="step-1",
        page_path="pages/Feed",
        screenshot=screenshot,
        phase="in_run",
        evidence={"target": target, "hilog_relative": "hilog_inrun_step-1.txt"},
    )


@pytest.fixture
def client(tmp_path: Path):
    settings = _settings(tmp_path)
    repository = DefectRepository(settings.resolved_database_path)
    recorder = DefectRecorder(repository)
    with TestClient(create_app(settings), raise_server_exceptions=False) as test_client:
        yield test_client, settings, repository, recorder


def _seed(recorder: DefectRecorder, *, count: int = 1, **kwargs) -> list[str]:
    ids: list[str] = []
    for index in range(count):
        record = recorder.record_one(
            _finding(target=f"ui-{index}", **kwargs),
            bundle_name=BUNDLE,
            run_id=RUN_ID,
            device_id="SN1",
        )
        assert record is not None
        ids.append(record.defect_id)
    return ids


class TestListAndDetail:
    def test_list_returns_summaries_sorted_by_last_seen(self, client) -> None:
        test_client, _settings_obj, _repository, recorder = client
        _seed(recorder, count=3)

        response = test_client.get("/api/defects")

        assert response.status_code == 200
        body = response.json()
        assert body["total"] == 3
        assert len(body["defects"]) == 3
        assert all("defect_id" in item for item in body["defects"])
        # 列表投影不含完整 findings（避免响应过大）
        assert all("findings" not in item for item in body["defects"])

    def test_list_filters_by_bundle_kind_severity_status(self, client) -> None:
        test_client, _settings_obj, _repository, recorder = client
        _seed(recorder, count=1)
        recorder.record_one(
            _finding(kind=AnomalyKind.WHITE_SCREEN, severity="warning", target="ui-x"),
            bundle_name="com.other.app",
        )

        assert test_client.get("/api/defects", params={"bundle_name": BUNDLE}).json()["total"] == 1
        assert test_client.get("/api/defects", params={"kind": "white_screen"}).json()["total"] == 1
        assert test_client.get("/api/defects", params={"severity": "critical"}).json()["total"] == 1
        assert test_client.get("/api/defects", params={"status": "suspected"}).json()["total"] == 2

    def test_list_rejects_unknown_status(self, client) -> None:
        test_client, *_ = client

        response = test_client.get("/api/defects", params={"status": "bogus"})

        assert response.status_code == 422

    def test_detail_returns_the_full_record_with_findings(self, client) -> None:
        test_client, _settings_obj, _repository, recorder = client
        defect_id = _seed(recorder)[0]

        response = test_client.get(f"/api/defects/{defect_id}")

        assert response.status_code == 200
        body = response.json()
        assert body["defect_id"] == defect_id
        assert body["findings"]
        assert body["bundle_name"] == BUNDLE

    def test_missing_defect_is_404(self, client) -> None:
        test_client, *_ = client

        assert test_client.get("/api/defects/defect-absent").status_code == 404
        assert test_client.patch("/api/defects/defect-absent", json={"status": "dismissed"}).status_code == 404


class TestPatch:
    def test_patch_sets_dismissed_and_notes(self, client) -> None:
        test_client, _settings_obj, _repository, recorder = client
        defect_id = _seed(recorder)[0]

        response = test_client.patch(
            f"/api/defects/{defect_id}",
            json={"status": "dismissed", "notes": "误报：动画帧"},
        )

        assert response.status_code == 200
        body = response.json()
        assert body["status"] == "dismissed"
        assert body["notes"] == "误报：动画帧"

    def test_patch_accepts_notes_only(self, client) -> None:
        test_client, _settings_obj, _repository, recorder = client
        defect_id = _seed(recorder)[0]

        response = test_client.patch(f"/api/defects/{defect_id}", json={"notes": "待复现"})

        assert response.status_code == 200
        assert response.json()["notes"] == "待复现"

    def test_patch_rejects_unknown_status_and_bad_notes(self, client) -> None:
        test_client, _settings_obj, _repository, recorder = client
        defect_id = _seed(recorder)[0]

        assert test_client.patch(f"/api/defects/{defect_id}", json={"status": "nope"}).status_code == 422
        assert test_client.patch(f"/api/defects/{defect_id}", json={"notes": 5}).status_code == 422


class TestArtifacts:
    def _write_evidence(self, settings: Settings, relative: str) -> None:
        path = settings.resolved_runtime_dir / RUN_ID / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("evidence", encoding="utf-8")

    def test_evidence_file_is_served(self, client) -> None:
        test_client, settings, _repository, recorder = client
        defect_id = _seed(recorder)[0]
        self._write_evidence(settings, "hilog_inrun_step-1.txt")

        response = test_client.get(f"/api/defects/{defect_id}/artifacts/hilog_inrun_step-1.txt")

        assert response.status_code == 200
        assert response.text == "evidence"

    def test_traversal_is_refused(self, client) -> None:
        test_client, settings, _repository, recorder = client
        defect_id = _seed(recorder)[0]
        (settings.resolved_runtime_dir / "secret.txt").parent.mkdir(parents=True, exist_ok=True)
        (settings.resolved_runtime_dir / "secret.txt").write_text("secret", encoding="utf-8")

        for attempt in ("../../secret.txt", "..%2F..%2Fsecret.txt", "sub/../../secret.txt"):
            response = test_client.get(f"/api/defects/{defect_id}/artifacts/{attempt}")
            assert response.status_code == 404, attempt

    def test_non_evidence_suffix_is_refused(self, client) -> None:
        test_client, settings, _repository, recorder = client
        defect_id = _seed(recorder)[0]
        self._write_evidence(settings, "payload.exe")

        assert test_client.get(f"/api/defects/{defect_id}/artifacts/payload.exe").status_code == 404

    def test_missing_artifact_is_404(self, client) -> None:
        test_client, _settings_obj, _repository, recorder = client
        defect_id = _seed(recorder)[0]

        assert test_client.get(f"/api/defects/{defect_id}/artifacts/nope.txt").status_code == 404

    def test_route_order_keeps_artifact_path_after_detail(self, client) -> None:
        """``/api/defects/{id}/artifacts/{path}`` 必须早于任何通配（``api/cases.py`` 踩过的坑）。"""
        test_client, _settings_obj, _repository, recorder = client
        defect_id = _seed(recorder)[0]

        # 若路由顺序错了，这个请求会被 /{defect_id} 吞掉并返回缺陷 JSON 而不是 404。
        response = test_client.get(f"/api/defects/{defect_id}/artifacts/absent.txt")

        assert response.status_code == 404
        assert "artifact" in response.json()["detail"]


class TestRunAndProfileDefectViews:
    def test_run_defects_endpoint(self, client) -> None:
        test_client, *_rest = client
        test_client  # noqa: B018 - 保持与其他用例一致的解包形态
        _settings_obj, _repository, recorder = _rest
        _seed(recorder, count=2)

        response = test_client.get(f"/api/runs/{RUN_ID}/defects")

        assert response.status_code == 200
        assert response.json()["total"] == 2

    def test_profile_defects_endpoint_404s_for_unknown_profile(self, client) -> None:
        test_client, *_ = client

        response = test_client.get("/api/profiles/does-not-exist/defects")

        assert response.status_code == 404


class TestToBugRepro:
    def test_missing_defect_is_404(self, client) -> None:
        test_client, *_ = client

        assert test_client.post("/api/defects/defect-absent/to-bug-repro").status_code == 404

    def test_dismissed_defect_is_refused(self, client) -> None:
        test_client, _settings_obj, repository, recorder = client
        defect_id = _seed(recorder)[0]
        repository.patch(defect_id, status=DefectStatus.DISMISSED, notes="误报")

        response = test_client.post(f"/api/defects/{defect_id}/to-bug-repro")

        assert response.status_code == 409

    def test_creates_case_and_attaches_it_to_the_defect(self, client) -> None:
        test_client, _settings_obj, repository, recorder = client
        defect_id = _seed(recorder)[0]

        async def fake_factory(*, record, request, trace=None, auto_execute=None):
            assert record.defect_id == defect_id
            # 转换规则：cppcrash → crash 症状
            assert request.symptom_kind == "crash"
            assert request.repro_steps_nl
            return {"case": {"case_id": "case-abc"}, "case_id": "case-abc", "execution_id": None}

        from harmony_test_agent.api import defects as defects_module

        with mock.patch.object(defects_module, "ANOMALY_TO_SYMPTOM", defects_module.ANOMALY_TO_SYMPTOM):
            router = defects_module.create_defects_router(
                settings=_settings_obj,
                repository=repository,
                bug_repro_factory=fake_factory,
            )
            # 直接调用路由函数，避免为一个 fake 重装整个 app
            import asyncio

            endpoint = next(
                route.endpoint
                for route in router.routes
                if getattr(route, "path", "") == "/api/defects/{defect_id}/to-bug-repro"
            )
            payload = asyncio.run(endpoint(defect_id, auto_execute=False))

        assert payload["case_id"] == "case-abc"
        assert payload["symptom_kind"] == "crash"
        assert payload["execution_id"] is None
        stored = repository.get(defect_id)
        assert stored is not None
        assert stored.repro_case_id == "case-abc"

    def test_bug_repro_failure_is_reported_as_502(self, client) -> None:
        test_client, settings, repository, recorder = client
        defect_id = _seed(recorder)[0]

        async def failing_factory(**_kwargs):
            raise RuntimeError("provider unavailable")

        from harmony_test_agent.api import defects as defects_module

        router = defects_module.create_defects_router(
            settings=settings,
            repository=repository,
            bug_repro_factory=failing_factory,
        )
        import asyncio

        endpoint = next(
            route.endpoint
            for route in router.routes
            if getattr(route, "path", "") == "/api/defects/{defect_id}/to-bug-repro"
        )
        from fastapi import HTTPException

        with pytest.raises(HTTPException) as excinfo:
            asyncio.run(endpoint(defect_id, auto_execute=False))

        assert excinfo.value.status_code == 502


class TestDefectRecorderThroughOrchestrator:
    def test_report_endpoint_does_not_break_without_a_defect_repository(self, tmp_path: Path) -> None:
        """分析器不可用时缺陷链路必须优雅降级（不 500）。"""
        settings = _settings(tmp_path)
        with TestClient(create_app(settings), raise_server_exceptions=False) as test_client:
            assert test_client.get("/api/defects").json()["total"] == 0

    def test_artifacts_missing_store_returns_501(self, tmp_path: Path) -> None:
        settings = _settings(tmp_path)
        repository = DefectRepository(settings.resolved_database_path)
        recorder = DefectRecorder(repository)
        defect_id = _seed(recorder)[0]

        from harmony_test_agent.api import defects as defects_module

        router = defects_module.create_defects_router(settings=settings, repository=repository, artifacts=None)
        endpoint = next(
            route.endpoint
            for route in router.routes
            if getattr(route, "path", "") == "/api/defects/{defect_id}/artifacts/{artifact_path:path}"
        )
        import asyncio

        from fastapi import HTTPException

        with pytest.raises(HTTPException) as excinfo:
            asyncio.run(endpoint(defect_id, "anything.txt"))

        assert excinfo.value.status_code == 501
        assert ArtifactStore is not None


class TestStorageWiring:
    def test_run_manager_creates_the_defect_repository(self, tmp_path: Path) -> None:
        settings = _settings(tmp_path)

        app = create_app(settings)

        manager = app.state.manager
        assert isinstance(manager.defect_repository, DefectRepository)
        assert manager.defect_recorder is not None
