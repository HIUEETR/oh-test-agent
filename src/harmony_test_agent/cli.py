"""提供目标发现、Profile 管理、测试运行、脚本生成和开发服务的命令行入口。"""

from __future__ import annotations

import argparse
import asyncio
import inspect
import json
import sys
from typing import Any

RUN_MODES = ("regression",)  # 历史模式仅用于 trace 读取；新 Run 一律 regression
ALLOW_FLAGS = ("login", "permission", "submit", "publish", "download")
_SMOKE_TASK = "启动应用，探索可达页面，验证返回和重启恢复。"


def _add_target_options(parser: argparse.ArgumentParser, *, task_required: bool) -> None:
    parser.add_argument("--app", help="installed application display name")
    parser.add_argument("--bundle-name", help="exact installed bundleName")
    parser.add_argument("--target-app", help="deprecated legacy Profile id")
    parser.add_argument("--task", required=task_required, default=_SMOKE_TASK)
    parser.add_argument("--device")
    parser.add_argument("--no-discovery", action="store_true")
    for name in ALLOW_FLAGS:
        parser.add_argument(f"--allow-{name}", action="store_true")
    # 探索预算默认交给 Settings（BOOTSTRAP_MAX_*）：只有用户显式给出参数才算「请求显式值」，
    # 否则 CLI 的 argparse 默认值会把 Settings 里收紧过的 bootstrap 预算整体顶掉（计划 4.2）。
    parser.add_argument("--max-pages", type=int, default=None, choices=range(1, 21), metavar="1..20")
    parser.add_argument("--max-actions-per-page", type=int, default=None, choices=range(1, 9), metavar="1..8")
    parser.add_argument("--discovery-timeout", type=int, default=None, choices=range(1, 901), metavar="1..900")
    parser.add_argument("--temporary-test", action="store_true")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="OpenHarmony multimodal UI testing agent")
    sub = parser.add_subparsers(dest="command")

    dev = sub.add_parser("dev", help="start and supervise the FastAPI and Vite development servers")
    dev.add_argument("--api-host", default="127.0.0.1")
    dev.add_argument("--api-port", type=int, default=8000)
    dev.add_argument("--web-host", default="127.0.0.1")
    dev.add_argument("--web-port", type=int, default=5173)
    dev.add_argument("--reload", action="store_true")
    dev.add_argument("--install", action="store_true", help="run npm ci before starting the servers")

    preflight = sub.add_parser("preflight", help="validate dependencies, model, HDC, screenshot and Hypium")
    preflight.add_argument("--no-screenshot", action="store_true")

    run = sub.add_parser("run", help="resolve an application and run a natural-language UI test task")
    _add_target_options(run, task_required=False)
    run.add_argument("--mode", choices=RUN_MODES, default="regression")
    run.add_argument("--max-steps", type=int, default=20)
    run.add_argument("--provider", choices=["auto", "mock", "openai"])
    run.add_argument("--no-generate", action="store_true")
    run.add_argument("--execute", action="store_true")

    profiles = sub.add_parser("profiles", help="list, show, verify or lock application Profiles")
    profile_sub = profiles.add_subparsers(dest="profile_command", required=True)
    profile_sub.add_parser("list")
    for action in ("show", "verify", "lock", "unlock"):
        command = profile_sub.add_parser(action)
        identity = command.add_mutually_exclusive_group(required=True)
        identity.add_argument("--profile-id")
        identity.add_argument("--bundle-name")
        if action == "verify":
            command.add_argument("--device")

    generate = sub.add_parser("generate", help="generate a Hypium project from a stored Run Trace")
    generate.add_argument("--run-id", required=True)
    execute = sub.add_parser("execute", help="execute a generated Hypium case")
    execute.add_argument("--run-id", required=True)
    execute.add_argument("--attempts", type=int, choices=[1, 2, 3], default=3)

    defects = sub.add_parser("defects", help="list, show, reproduce or dismiss application defects")
    defect_sub = defects.add_subparsers(dest="defect_command", required=True)
    defect_list = defect_sub.add_parser("list")
    defect_list.add_argument("--bundle", dest="bundle_name")
    defect_list.add_argument("--kind")
    defect_list.add_argument("--severity")
    defect_list.add_argument("--status")
    defect_list.add_argument("--limit", type=int, default=100)
    defect_show = defect_sub.add_parser("show")
    defect_show.add_argument("defect_id")
    defect_repro = defect_sub.add_parser("repro", help="turn a defect into a bug reproduction case")
    defect_repro.add_argument("defect_id")
    defect_repro.add_argument("--execute", action="store_true", help="run the generated case immediately")
    defect_dismiss = defect_sub.add_parser("dismiss")
    defect_dismiss.add_argument("defect_id")
    defect_dismiss.add_argument("--notes", default="", help="why this defect is a false positive")

    serve = sub.add_parser("serve", help="start only the FastAPI server")
    serve.add_argument("--host", default="127.0.0.1")
    serve.add_argument("--port", type=int, default=8000)
    serve.add_argument("--reload", action="store_true")
    return parser


def _request_payload(args: argparse.Namespace, *, discover_only: bool = False) -> dict[str, Any]:
    """构造 RunRequest 载荷。

    ``discover_only`` 保留（默认 False）：CLI 不再提供 discover 子命令，该参数只为
    ``bootstrap_only`` 字段的历史契约留位，不再影响 ``mode``。
    """
    target = {key: value for key, value in {"app_name": args.app, "bundle_name": args.bundle_name}.items() if value}
    discovery = {
        "enabled": not args.no_discovery,
        **{
            key: value
            for key, value in (
                ("max_pages", args.max_pages),
                ("max_actions_per_page", args.max_actions_per_page),
                ("max_duration_seconds", args.discovery_timeout),
            )
            if value is not None
        },
        "temporary_test": args.temporary_test,
        **{f"allow_{name}": bool(getattr(args, f"allow_{name}")) for name in ALLOW_FLAGS},
    }
    data: dict[str, Any] = {
        "target": target or None,
        "task": args.task,
        "mode": args.mode,
        "device_id": args.device,
        "exploration_policy": discovery,
        "temporary_test": args.temporary_test,
        "bootstrap_only": discover_only,
        "auto_generate": not discover_only and not getattr(args, "no_generate", False),
        "auto_execute": not discover_only and getattr(args, "execute", False),
    }
    if args.target_app:
        print("warning: --target-app is deprecated; use --app or --bundle-name", file=sys.stderr)
        data["target_app_id"] = args.target_app
    return {key: value for key, value in data.items() if value is not None}


def _make_run_request(data: dict[str, Any]):
    from .models import RunRequest

    fields = RunRequest.model_fields
    if "target" not in fields:
        target = data.pop("target", None) or {}
        data["target_app_id"] = data.get("target_app_id") or target.get("bundle_name") or "zhihu-plus"
    if "exploration_policy" not in fields:
        data.pop("exploration_policy", None)
    if "temporary_test" not in fields:
        data.pop("temporary_test", None)
    return RunRequest.model_validate(data)


def _execution_analyzer(settings: Any) -> Any | None:
    """构造执行结果分析器（与 api/app.py 同一实现口径）；不可用时返回 None。"""
    from .runner import build_execution_analyzer

    return build_execution_analyzer(settings)


def _defect_repository(settings: Any) -> Any | None:
    """构造缺陷仓库（与 api/app.py 同一实现口径）；不可用时返回 None。"""
    try:
        from .storage.defect_repository import DefectRepository

        return DefectRepository(settings.resolved_database_path)
    except Exception:  # noqa: BLE001 - 缺陷落库是可选能力
        return None


def _defect_recorder(settings: Any) -> Any | None:
    """构造缺陷记录器；关闭分析或仓库不可用时返回 None（行为与历史完全一致）。"""
    if not getattr(settings, "case_analysis_enabled", True):
        return None
    repository = _defect_repository(settings)
    if repository is None:
        return None
    try:
        from .analysis.defects import DefectRecorder

        return DefectRecorder(repository)
    except Exception:  # noqa: BLE001
        return None


def _trace_bundle_name(trace: Any) -> str:
    """从冻结 Profile / 解析目标里取被测 bundle（CLI execute 的分析归属判定）。"""
    for profile in (trace.profile_snapshot, getattr(trace.resolved_target, "profile_snapshot", None)):
        bundle = getattr(profile, "bundle_name", "") if profile is not None else ""
        if bundle:
            return str(bundle)
    resolved = getattr(trace, "resolved_target", None)
    return str(getattr(resolved, "bundle_name", "") or "")


def _case_library(settings: Any) -> Any | None:
    """构造用例库（可选注入）；依赖不可用时返回 None。"""
    try:
        from .cases.library import CaseLibrary
        from .storage.case_repository import CaseRepository

        cases_root = settings.resolved_cases_dir
        return CaseLibrary(
            CaseRepository(cases_root),
            cases_root,
            min_observed_rounds=settings.profile_verification_rounds,
        )
    except Exception:  # noqa: BLE001 - 用例沉淀是可选能力
        return None


def _call_service(service: Any, names: tuple[str, ...], **values: Any) -> Any:
    for name in names:
        method = getattr(service, name, None)
        if not callable(method):
            continue
        signature = inspect.signature(method)
        kwargs = {key: value for key, value in values.items() if key in signature.parameters}
        return method(**kwargs)
    raise SystemExit(f"Profile service does not implement: {', '.join(names)}")


def main(argv: list[str] | None = None) -> None:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
        sys.stderr.reconfigure(encoding="utf-8")
    raw_args = list(sys.argv[1:] if argv is None else argv)
    commands = {"dev", "preflight", "run", "profiles", "generate", "execute", "defects", "serve"}
    if raw_args and raw_args[0] not in commands and raw_args[0] not in {"-h", "--help"}:
        raw_args.insert(0, "dev")
    args = build_parser().parse_args(raw_args)
    command = args.command or "dev"

    if command == "dev":
        from .devserver import DevServerConfig, DevServerError, run_dev_server

        config = DevServerConfig(
            api_host=getattr(args, "api_host", "127.0.0.1"),
            api_port=getattr(args, "api_port", 8000),
            web_host=getattr(args, "web_host", "127.0.0.1"),
            web_port=getattr(args, "web_port", 5173),
            reload=getattr(args, "reload", False),
            install=getattr(args, "install", False),
        )
        try:
            code = run_dev_server(config)
        except DevServerError as exc:
            print(f"error: {exc}", file=sys.stderr)
            code = 2
        raise SystemExit(code)

    from .config import get_settings

    settings = get_settings()
    if command == "preflight":
        from .preflight import PreflightService

        report = PreflightService(settings).run(include_screenshot=not args.no_screenshot)
        print(report.model_dump_json(indent=2))
        raise SystemExit(0 if report.status == "pass" else 1)

    if command == "run":
        from .agents import AgentOrchestrator
        from .models import RunState

        if args.provider:
            settings.agent_provider = args.provider
        request = _make_run_request(_request_payload(args, discover_only=False))
        # 与 API 同一条流水线：注入可选用例库与执行结果分析器，
        # 让 CLI 运行同样产出 analysis.json（计划 G1：reports/report.html + analysis.json）。
        orchestrator = AgentOrchestrator(
            settings,
            case_library=_case_library(settings),
            analyzer=_execution_analyzer(settings),
            defect_recorder=_defect_recorder(settings),
        )

        async def execute_run():
            task = asyncio.create_task(orchestrator.run(request))
            while not task.done():
                await asyncio.sleep(0.2)
                waiting = next(
                    (
                        item
                        for item in orchestrator.repository.list_runs(limit=5)
                        if item["state"] == RunState.WAITING_TARGET_SELECTION
                    ),
                    None,
                )
                if not waiting:
                    continue
                trace = orchestrator.repository.get_trace(waiting["run_id"])
                candidates = trace.target_candidates if trace else []
                if not sys.stdin.isatty():
                    orchestrator.request_stop(waiting["run_id"])
                    await task
                    print(
                        json.dumps(
                            {"state": RunState.WAITING_TARGET_SELECTION, "candidates": candidates},
                            ensure_ascii=False,
                            indent=2,
                        )
                    )
                    raise SystemExit(2)
                print("检测到多个同名应用：")
                for index, candidate in enumerate(candidates, 1):
                    print(f"{index}. {candidate.get('display_name')} ({candidate.get('bundle_name')})")
                choice = int(input("请选择序号：")) - 1
                await orchestrator.select_target(waiting["run_id"], bundle_name=candidates[choice]["bundle_name"])
            return await task

        trace = asyncio.run(execute_run())
        print(trace.model_dump_json(indent=2))
        raise SystemExit(0 if trace.state == RunState.COMPLETED else 1)

    from .storage import ArtifactStore, RunRepository

    artifacts = ArtifactStore(settings.resolved_runtime_dir)
    repository = RunRepository(settings.resolved_database_path)
    if command == "profiles":
        from .api.app import RunManager, _fallback_profile_get, _fallback_profile_list, _fallback_profile_lock

        manager = RunManager(settings)
        identity = getattr(args, "profile_id", None) or getattr(args, "bundle_name", None)
        if args.profile_command == "list":
            result = (
                _call_service(manager.profile_registry, ("list", "list_profiles", "all"))
                if manager.profile_registry
                else _fallback_profile_list(settings)
            )
        elif args.profile_command == "show":
            result = (
                _call_service(
                    manager.profile_registry,
                    ("get", "get_profile", "load"),
                    target_app_id=identity,
                    profile_id=identity,
                    identifier=identity,
                )
                if manager.profile_registry
                else _fallback_profile_get(settings, identity)
            )
        elif args.profile_command == "verify":
            if not manager.profile_registry:
                raise SystemExit("Profile verification service is unavailable")
            result = _call_service(
                manager.profile_registry,
                ("verify", "verify_profile", "request_verification"),
                profile_id=identity,
                identifier=identity,
                device_id=args.device,
            )
        else:
            locked = args.profile_command == "lock"
            result = (
                _call_service(
                    manager.profile_registry,
                    ("set_locked", "lock", "lock_profile"),
                    target_app_id=identity,
                    profile_id=identity,
                    identifier=identity,
                    locked=locked,
                )
                if manager.profile_registry
                else _fallback_profile_lock(settings, identity, locked)
            )
        print(
            json.dumps(
                result.model_dump(mode="json") if hasattr(result, "model_dump") else result,
                ensure_ascii=False,
                indent=2,
            )
        )
        return

    if command == "defects":
        raise SystemExit(_run_defects_command(args, settings))

    if command == "generate":
        from .generation import HypiumGenerator
        from .reporting import ReportBuilder

        trace = repository.get_trace(args.run_id) or artifacts.load_trace(args.run_id)
        trace.generated = HypiumGenerator(artifacts, min_observed_rounds=settings.profile_verification_rounds).generate(
            trace
        )
        repository.save_trace(trace)
        artifacts.save_trace(trace)
        ReportBuilder(artifacts).build(trace)
        print(trace.generated.model_dump_json(indent=2))
        return
    if command == "execute":
        from .models import RunState
        from .reporting import ReportBuilder
        from .runner import make_hypium_runner

        trace = repository.get_trace(args.run_id) or artifacts.load_trace(args.run_id)
        if not trace.generated:
            raise SystemExit("run has no generated Hypium artifact; use generate first")
        analyzer = _execution_analyzer(settings) if settings.analysis_on_replay_endpoints else None
        runner = make_hypium_runner(
            settings,
            analyzer=analyzer,
            analyze=bool(settings.analysis_on_replay_endpoints),
            bundle_resolver=lambda: _trace_bundle_name(trace),
            device_id=trace.device_id,
        )
        trace.replays = runner.execute_repeated(trace.generated, args.attempts)
        trace.state = RunState.COMPLETED if all(item.passed for item in trace.replays) else RunState.FAILED_SCRIPT
        repository.save_trace(trace)
        artifacts.save_trace(trace)
        ReportBuilder(artifacts).build(trace)
        print(json.dumps([item.model_dump(mode="json") for item in trace.replays], ensure_ascii=False, indent=2))
        raise SystemExit(0 if all(item.passed for item in trace.replays) else 1)
    if command == "serve":
        import uvicorn

        uvicorn.run(
            "harmony_test_agent.api.app:create_app", factory=True, host=args.host, port=args.port, reload=args.reload
        )


def _run_defects_command(args: argparse.Namespace, settings: Any) -> int:
    """``defects list / show / repro / dismiss`` 子命令实现（Phase 4.4）。

    直接读写缺陷仓库，不经 HTTP：CLI 与 API 共用同一条 :class:`DefectRepository`。
    """
    repository = _defect_repository(settings)
    if repository is None:
        print("error: defect repository is unavailable", file=sys.stderr)
        return 2
    from .analysis.defects import DefectStatus

    command = args.defect_command
    if command == "list":
        summaries = repository.list(
            bundle_name=args.bundle_name,
            kind=args.kind,
            severity=args.severity,
            status=args.status,
            limit=max(1, int(args.limit)),
        )
        print(json.dumps([item.model_dump(mode="json") for item in summaries], ensure_ascii=False, indent=2))
        return 0
    if command == "show":
        record = repository.get(args.defect_id)
        if record is None:
            print(f"error: defect {args.defect_id} not found", file=sys.stderr)
            return 1
        print(record.model_dump_json(indent=2))
        return 0
    if command == "repro":
        return _run_defect_repro(args, settings, repository=repository)
    if command == "dismiss":
        updated = repository.patch(args.defect_id, status=DefectStatus.DISMISSED, notes=args.notes)
        if updated is None:
            print(f"error: defect {args.defect_id} not found", file=sys.stderr)
            return 1
        print(updated.model_dump_json(indent=2))
        return 0
    print(f"error: unknown defects subcommand: {command}", file=sys.stderr)
    return 2


def _run_defect_repro(args: argparse.Namespace, settings: Any, *, repository: Any) -> int:
    """把一条缺陷转成复现用例（``--execute`` 时立即重跑一次）。

    刻意**复用 API 的 bug-repro 实现**（``api/cases.py::build_and_save_bug_repro_case``），
    而不是在 CLI 里重写一遍构建逻辑：两处实现必然漂移，而「缺陷 → 复现用例」的产物结构
    必须与 API 完全一致。调度回调在这里是 ``None``：CLI 的 ``--execute`` 直接同步跑，
    不需要 HTTP 层的后台任务。
    """
    import asyncio as _asyncio

    from .analysis.defects import defect_to_bug_repro_request
    from .api.cases import build_and_save_bug_repro_case
    from .cases.library import CaseLibrary
    from .models import TargetQuery
    from .profiles import ProfileRegistry
    from .runner import make_hypium_runner
    from .storage.case_repository import CaseRepository
    from .storage.repository import RunRepository

    record = repository.get(args.defect_id)
    if record is None:
        print(f"error: defect {args.defect_id} not found", file=sys.stderr)
        return 1
    trace = None
    try:
        if record.run_id:
            trace = RunRepository(settings.resolved_database_path).get_trace(record.run_id)
    except Exception:  # noqa: BLE001 - 没有 trace 时复现步骤退化为兜底文案
        trace = None
    request = defect_to_bug_repro_request(record, trace=trace)
    if not request.target and record.bundle_name:
        request.target = TargetQuery(bundle_name=record.bundle_name)
    registry = ProfileRegistry(
        settings.resolved_profiles_dir,
        promotion_replay_attempts=settings.hypium_replay_attempts,
        min_evidence_rounds=settings.profile_verification_rounds,
    )
    case_repository = CaseRepository(settings.resolved_database_path)
    library = CaseLibrary(
        case_repository,
        settings.resolved_cases_dir,
        min_observed_rounds=settings.profile_verification_rounds,
        runner_factory=lambda root, timeout: make_hypium_runner(settings, timeout=timeout, analyze=False),
        analyzer=_execution_analyzer(settings),
        defect_repository=repository,
    )
    try:
        case_record, _execution_id = _asyncio.run(
            build_and_save_bug_repro_case(
                settings=settings,
                library=library,
                repository=case_repository,
                registry=registry,
                request=request,
                auto_execute=False,
                defect_id=record.defect_id,
            )
        )
    except Exception as exc:  # noqa: BLE001 - 如实报告失败原因
        print(f"error: cannot build repro case: {type(exc).__name__}: {exc}", file=sys.stderr)
        return 1
    if case_record is None:
        print("error: bug reproduction pipeline returned no case", file=sys.stderr)
        return 1
    repository.attach_repro_case(record.defect_id, case_id=case_record.case_id)
    print(case_record.model_dump_json(indent=2))
    if not args.execute:
        return 0
    execution = library.execute(case_record.case_id, engine="hypium_standalone")
    repository.patch(record.defect_id, repro_execution_id=execution.execution_id)
    print(execution.model_dump_json(indent=2))
    return 0 if execution.status == "passed" else 1


if __name__ == "__main__":
    main()
