"""提供预检、测试运行、脚本生成、脚本执行和开发服务的命令行入口。"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys

RUN_MODES = ("regression", "exploration", "stability", "reproduction")


def build_parser() -> argparse.ArgumentParser:
    """构建包含全部子命令及参数约束的命令行解析器。"""
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

    run = sub.add_parser("run", help="run a natural-language UI test task")
    run.add_argument("--task", required=True)
    run.add_argument("--target-app", default="zhihu-plus")
    run.add_argument("--device")
    run.add_argument("--mode", choices=RUN_MODES, default="regression")
    run.add_argument("--max-steps", type=int, default=20)
    run.add_argument("--provider", choices=["auto", "mock", "openai"])
    run.add_argument("--no-generate", action="store_true")
    run.add_argument("--execute", action="store_true")

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


def main(argv: list[str] | None = None) -> None:
    """解析命令行参数，并分派到对应的测试代理工作流。"""
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
        sys.stderr.reconfigure(encoding="utf-8")
    raw_args = list(sys.argv[1:] if argv is None else argv)
    commands = {"dev", "preflight", "run", "generate", "execute", "serve"}
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
        from .models import RunRequest, RunState

        if args.provider:
            settings.agent_provider = args.provider
        request = RunRequest(
            target_app_id=args.target_app,
            task=args.task,
            mode=args.mode,
            device_id=args.device,
            max_steps=args.max_steps,
            auto_generate=not args.no_generate,
            auto_execute=args.execute,
        )
        trace = asyncio.run(AgentOrchestrator(settings).run(request))
        print(trace.model_dump_json(indent=2))
        raise SystemExit(0 if trace.state == RunState.COMPLETED else 1)

    from .storage import ArtifactStore, RunRepository

    artifacts = ArtifactStore(settings.resolved_runtime_dir)
    repository = RunRepository(settings.resolved_database_path)
    if command == "generate":
        from .generation import HypiumGenerator
        from .reporting import ReportBuilder

        trace = repository.get_trace(args.run_id) or artifacts.load_trace(args.run_id)
        profile = artifacts.load_profile(settings.resolved_target_profile_path)
        trace.generated = HypiumGenerator(artifacts).generate(trace, profile)
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
            "harmony_test_agent.api.app:create_app",
            factory=True,
            host=args.host,
            port=args.port,
            reload=args.reload,
        )


if __name__ == "__main__":
    main()
