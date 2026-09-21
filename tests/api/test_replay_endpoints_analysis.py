"""两个 replay 端点的执行结果分析契约（Phase 1：``analysis_on_replay_endpoints``）。

- ``POST /api/runs/{id}/execute`` → 分析进 ``trace.replays[*].analysis``（``GET /api/runs/{id}`` 带出）；
- ``POST /api/profiles/{id}/replay`` → 响应每项追加 ``analysis``。

两条路径都必须保持「分析永不翻转 passed / 晋级门禁」。
"""

from __future__ import annotations

from pathlib import Path
from unittest import mock

from fastapi.testclient import TestClient

from harmony_test_agent.analysis.service import ExecutionAnalyzer
from harmony_test_agent.api.app import create_app
from harmony_test_agent.config import Settings
from harmony_test_agent.models import (
    AnomalyFinding,
    AnomalyKind,
    AssertionDefinition,
    CommandResult,
    GeneratedArtifact,
    ProfileStatus,
    ReplayResult,
    RunState,
    RunTrace,
    StableLocator,
    TargetAppProfile,
)
from harmony_test_agent.profiles import ProfileRegistry
from harmony_test_agent.runner import HypiumRunner
from harmony_test_agent.storage import ArtifactStore, RunRepository

BUNDLE = "com.example.notes"
TARGET_APP_ID = "com-example-notes"
RUN_ID = "run-20260101T000000Z-cccc3333"


def _settings(tmp_path: Path, **overrides) -> Settings:
    payload = {
        "runtime_dir": tmp_path / "runs",
        "database_path": tmp_path / "agent.db",
        "target_profile_path": None,
        "profiles_dir": tmp_path / "profiles",
        "runtime_home": tmp_path / "runtime-home",
        "agent_provider": "mock",
        "hypium_replay_attempts": 1,
        "profile_verification_rounds": 1,
    }
    payload.update(overrides)
    return Settings(**payload)


def _finding() -> AnomalyFinding:
    return AnomalyFinding(
        kind=AnomalyKind.CPP_CRASH,
        severity="critical",
        summary_zh="hilog 中出现 C++ 崩溃",
        source="hilog",
    )


def _analysis(subject: str, subject_id: str) -> object:
    from harmony_test_agent.models import ExecutionAnalysis

    return ExecutionAnalysis(
        subject=subject,  # type: ignore[arg-type]
        subject_id=subject_id,
        bundle_name=BUNDLE,
        device_id="SN1",
        healthy=False,
        findings=[_finding()],
    )


def _analysis_for(run_id: str):
    """生成 ``analyze_replay`` 的 side_effect：按 attempt 编号返回对应 subject_id 的分析。"""

    def factory(replay, **kwargs):
        return _analysis("hypium_replay", f"{run_id}#attempt-{replay.attempt:02d}")

    return factory


def _seed_run(settings: Settings) -> None:
    artifacts = ArtifactStore(settings.resolved_runtime_dir)
    repository = RunRepository(settings.resolved_database_path)
    run_dir = artifacts.run_dir(RUN_ID)
    generated_dir = run_dir / "generated"
    generated_dir.mkdir(parents=True, exist_ok=True)
    script = generated_dir / "test_run.py"
    script.write_text("print('noop')\n", encoding="utf-8")
    script.with_suffix(".json").write_text("{}\n", encoding="utf-8")
    repository.save_trace(
        RunTrace(
            run_id=RUN_ID,
            target_app_id=TARGET_APP_ID,
            task="回放分析接线",
            device_id="SN1",
            state=RunState.COMPLETED,
            profile_snapshot=TargetAppProfile(target_app_id=TARGET_APP_ID, display_name="Notes", bundle_name=BUNDLE),
            generated=GeneratedArtifact(
                python_path=script,
                config_path=script.with_suffix(".json"),
                metadata_path=script.with_suffix(".json"),
                purpose="acceptance",
                replay_eligible=True,
            ),
        )
    )


def _profile(*, status: ProfileStatus = ProfileStatus.CANDIDATE) -> TargetAppProfile:
    return TargetAppProfile(
        status=status,
        target_app_id=TARGET_APP_ID,
        display_name="Notes",
        bundle_name=BUNDLE,
        main_ability="MainAbility",
        stable_locator_inventory=[
            StableLocator(
                name=f"locator-{index}",
                page_signature=f"page-{index}",
                key=f"key-{index}",
                observed_rounds=1,
                unique_match_rounds=1,
                evidence_snapshot_ids=[f"snap-{index}"],
            )
            for index in range(1, 4)
        ],
        assertion_inventory=[
            AssertionDefinition(
                name=f"assertion-{index}",
                kind="visible",
                target=f"key-{index}",
                page_signature=f"page-{index}",
                observed_rounds=1,
                evidence_snapshot_ids=[f"asnap-{index}"],
            )
            for index in range(1, 3)
        ],
        core_flows=[
            {
                "pages": ["page-1", "page-2", "page-3"],
                "steps": [],
                "interaction_types": ["click", "input"],
            }
        ],
        provenance={"discovery_run_id": "run-profile", "evidence": {"verification_passed": True}},
    )


def _seed_profile_script(settings: Settings) -> Path:
    path = settings.resolved_runtime_dir / "run-profile" / "generated" / "test_run_profile.py"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("print('noop')\n", encoding="utf-8")
    path.with_suffix(".json").write_text("{}\n", encoding="utf-8")
    return path


class TestRunExecuteAnalysis:
    def test_execute_attaches_analysis_to_every_replay(self, tmp_path: Path) -> None:
        import time

        settings = _settings(tmp_path)
        _seed_run(settings)

        with (
            mock.patch.object(
                ExecutionAnalyzer,
                "analyze_replay",
                side_effect=_analysis_for(RUN_ID),
            ) as analyze,
            TestClient(create_app(settings), raise_server_exceptions=False) as client,
        ):
            response = client.post(f"/api/runs/{RUN_ID}/execute")
            assert response.status_code in {200, 202}, response.text
            deadline = time.monotonic() + 30
            body: dict = {}
            while time.monotonic() < deadline:
                body = client.get(f"/api/runs/{RUN_ID}").json()
                if body.get("replays"):
                    break
                time.sleep(0.05)

        replays = body["replays"]
        assert replays
        assert all(item["analysis"] is not None for item in replays)
        assert replays[0]["analysis"]["subject"] == "hypium_replay"
        assert analyze.call_args.kwargs["run_dir"] == settings.resolved_runtime_dir / RUN_ID
        assert analyze.call_args.kwargs["bundle_name"] == BUNDLE
        assert analyze.call_args.kwargs["device_id"] == "SN1"

    def test_switch_off_skips_analysis(self, tmp_path: Path) -> None:
        import time

        settings = _settings(tmp_path, analysis_on_replay_endpoints=False)
        _seed_run(settings)

        with (
            mock.patch.object(ExecutionAnalyzer, "analyze_replay") as analyze,
            TestClient(create_app(settings), raise_server_exceptions=False) as client,
        ):
            client.post(f"/api/runs/{RUN_ID}/execute")
            deadline = time.monotonic() + 30
            body: dict = {}
            while time.monotonic() < deadline:
                body = client.get(f"/api/runs/{RUN_ID}").json()
                if body.get("replays"):
                    break
                time.sleep(0.05)

        assert body["replays"]
        assert all(item["analysis"] is None for item in body["replays"])
        analyze.assert_not_called()


class TestProfileReplayAnalysis:
    def _prepare(self, settings: Settings) -> ProfileRegistry:
        registry = ProfileRegistry(settings.resolved_profiles_dir, promotion_replay_attempts=3, min_evidence_rounds=1)
        registry.save_candidate(_profile())
        candidate = registry.read(TARGET_APP_ID, ProfileStatus.CANDIDATE)
        registry._atomic_write(
            registry.candidate_dir / f"{TARGET_APP_ID}.json",
            candidate.model_copy(
                update={
                    "provenance": candidate.provenance.model_copy(
                        update={"generated_script_path": str(_seed_profile_script(settings))}, deep=True
                    )
                },
                deep=True,
            ),
        )
        return registry

    @staticmethod
    def _fake_execute(*, passed: bool = True):
        """替换 ``HypiumRunner.execute``：跳过真实设备执行，但**保留 hook 调用**。

        hook 是 Phase 1 的接线本体，必须被走到，否则测不到分析是否真的挂上了。
        """

        def execute(self, generated, attempt: int = 1):
            attempt_dir = generated.python_path.parent.parent / "hypium" / f"attempt-{attempt:02d}"
            attempt_dir.mkdir(parents=True, exist_ok=True)
            result = ReplayResult(
                attempt=attempt,
                command=CommandResult(command="hypium", returncode=0 if passed else 1),
                report_path=attempt_dir,
                passed=passed,
                status="passed" if passed else "failed",
                evidence_paths=[f"hypium/attempt-{attempt:02d}/stdout.log"],
            )
            hook = self.analysis_hook
            if hook is None:
                return result
            try:
                analysis = hook(result, attempt_dir)
            except Exception:  # 与 HypiumRunner._attach_analysis 同语义：分析永远 advisory
                return result
            return result if analysis is None else result.model_copy(update={"analysis": analysis})

        return execute

    def test_response_item_carries_analysis(self, tmp_path: Path) -> None:
        settings = _settings(tmp_path)
        self._prepare(settings)

        with (
            mock.patch.object(HypiumRunner, "execute", self._fake_execute()),
            mock.patch.object(
                ExecutionAnalyzer,
                "analyze_replay",
                side_effect=lambda replay, **kwargs: _analysis("hypium_replay", f"profile-attempt-{replay.attempt}"),
            ),
            TestClient(create_app(settings), raise_server_exceptions=False) as client,
        ):
            response = client.post(f"/api/profiles/{TARGET_APP_ID}/replay", json={"attempts": 1})

        assert response.status_code == 200, response.text
        results = response.json()["results"]
        assert results and results[0]["analysis"] is not None
        assert results[0]["analysis"]["subject"] == "hypium_replay"
        assert results[0]["analysis"]["healthy"] is False

    def test_unhealthy_analysis_does_not_block_promotion(self, tmp_path: Path) -> None:
        """红线：分析是 advisory —— critical finding 不改变晋级门禁读的 passed。"""
        settings = _settings(tmp_path)
        self._prepare(settings)

        with (
            mock.patch.object(HypiumRunner, "execute", self._fake_execute()),
            mock.patch.object(
                ExecutionAnalyzer,
                "analyze_replay",
                side_effect=lambda replay, **kwargs: _analysis("hypium_replay", f"profile-attempt-{replay.attempt}"),
            ),
            TestClient(create_app(settings), raise_server_exceptions=False) as client,
        ):
            response = client.post(f"/api/profiles/{TARGET_APP_ID}/replay", json={"attempts": 1})

        body = response.json()
        assert body["results"][0]["passed"] is True
        assert body["results"][0]["analysis"]["healthy"] is False
        assert body["total_replays"] == 1

    def test_analysis_failure_does_not_break_the_endpoint(self, tmp_path: Path) -> None:
        settings = _settings(tmp_path)
        self._prepare(settings)

        with (
            mock.patch.object(HypiumRunner, "execute", self._fake_execute()),
            mock.patch.object(ExecutionAnalyzer, "analyze_replay", side_effect=RuntimeError("analyzer down")),
            TestClient(create_app(settings), raise_server_exceptions=False) as client,
        ):
            response = client.post(f"/api/profiles/{TARGET_APP_ID}/replay", json={"attempts": 1})

        assert response.status_code == 200, response.text
        results = response.json()["results"]
        assert results[0]["passed"] is True
        assert results[0]["analysis"] is None
