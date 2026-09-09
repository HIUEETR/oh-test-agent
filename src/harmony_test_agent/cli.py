"""提供预检、测试运行、脚本生成、脚本执行和 API 服务的命令行入口。"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys

from .agents import AgentOrchestrator
from .api import create_app
from .config import get_settings
from .generation import HypiumGenerator
from .models import RunMode, RunRequest, RunState
from .preflight import PreflightService
from .reporting import ReportBuilder
from .runner import HypiumRunner
from .storage import ArtifactStore, RunRepository


def build_parser() -> argparse.ArgumentParser:
    """构建包含全部子命令及参数约束的命令行解析器。"""
    parser = argparse.ArgumentParser(description="OpenHarmony multimodal UI testing agent")
    sub = parser.add_subparsers(dest="command", required=True)

    preflight = sub.add_parser("preflight", help="validate dependencies, model, HDC, screenshot and Hypium")
    preflight.add_argument("--no-screenshot", action="store_true")

    run = sub.add_parser("run", help="run a natural-language UI test task")
    run.add_argument("--task", required=True)
    run.add_argument("--target-app", default="zhihu-plus")
    run.add_argument("--device")
    run.add_argument("--mode", choices=[item.value for item in RunMode], default=RunMode.REGRESSION)
    run.add_argument("--max-steps", type=int, default=20)
    run.add_argument("--provider", choices=["auto", "mock", "openai"])
    run.add_argument("--no-generate", action="store_true")
    run.add_argument("--execute", action="store_true")

    generate = sub.add_parser("generate", help="generate a Hypium project from a stored Run Trace")
    generate.add_argument("--run-id", required=True)

    execute = sub.add_parser("execute", help="execute a generated Hypium case")
    execute.add_argument("--run-id", required=True)
    execute.add_argument("--attempts", type=int, choices=[1, 2, 3], default=3)

    serve = sub.add_parser("serve", help="start the FastAPI server")
    serve.add_argument("--host", default="127.0.0.1")
    serve.add_argument("--port", type=int, default=8000)
    serve.add_argument("--reload", action="store_true")
    return parser


def main(argv: list[str] | None = None) -> None:
    """解析命令行参数，并分派到对应的测试代理工作流。"""
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
        sys.stderr.reconfigure(encoding="utf-8")
    args = build_parser().parse_args(argv)
    settings = get_settings()
    if args.command == "preflight":
        report = PreflightService(settings).run(include_screenshot=not args.no_screenshot)
        print(report.model_dump_json(indent=2))
        raise SystemExit(0 if report.status == "pass" else 1)
    if args.command == "run":
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
    artifacts = ArtifactStore(settings.resolved_runtime_dir)
    repository = RunRepository(settings.resolved_database_path)
    if args.command == "generate":
        trace = repository.get_trace(args.run_id) or artifacts.load_trace(args.run_id)
        profile = artifacts.load_profile(settings.resolved_target_profile_path)
        trace.generated = HypiumGenerator(artifacts).generate(trace, profile)
        repository.save_trace(trace)
        artifacts.save_trace(trace)
        ReportBuilder(artifacts).build(trace)
        print(trace.generated.model_dump_json(indent=2))
        return
    if args.command == "execute":
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
    if args.command == "serve":
        import uvicorn

        uvicorn.run(create_app(settings), host=args.host, port=args.port, reload=args.reload)


if __name__ == "__main__":
    main()
