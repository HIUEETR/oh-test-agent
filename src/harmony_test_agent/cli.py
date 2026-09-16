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
    parser.add_argument("--max-pages", type=int, default=20, choices=range(1, 21), metavar="1..20")
    parser.add_argument("--max-actions-per-page", type=int, default=8, choices=range(1, 9), metavar="1..8")
    parser.add_argument("--discovery-timeout", type=int, default=900, choices=range(1, 901), metavar="1..900")
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
        "max_pages": args.max_pages,
        "max_actions_per_page": args.max_actions_per_page,
        "max_duration_seconds": args.discovery_timeout,
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
    commands = {"dev", "preflight", "run", "profiles", "generate", "execute", "serve"}
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
        orchestrator = AgentOrchestrator(settings)

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
        from .runner import HypiumRunner

        trace = repository.get_trace(args.run_id) or artifacts.load_trace(args.run_id)
        if not trace.generated:
            raise SystemExit("run has no generated Hypium artifact; use generate first")
        trace.replays = HypiumRunner(settings.resolved_runtime_home).execute_repeated(trace.generated, args.attempts)
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


if __name__ == "__main__":
    main()
