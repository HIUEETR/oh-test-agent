from __future__ import annotations

import sys
from pathlib import Path

import pytest

from harmony_test_agent import cli, devserver
from harmony_test_agent.devserver import (
    DevServerConfig,
    DevServerError,
    ProcessSpec,
    build_process_specs,
    run_dev_server,
    validate_environment,
)


def test_cli_without_arguments_dispatches_default_dev_config(monkeypatch: pytest.MonkeyPatch) -> None:
    received: list[DevServerConfig] = []
    monkeypatch.setattr(devserver, "run_dev_server", lambda config: received.append(config) or 0)

    with pytest.raises(SystemExit) as exit_info:
        cli.main([])

    assert exit_info.value.code == 0
    assert received == [DevServerConfig()]


def test_cli_root_dev_options_are_dispatched(monkeypatch: pytest.MonkeyPatch) -> None:
    received: list[DevServerConfig] = []
    monkeypatch.setattr(devserver, "run_dev_server", lambda config: received.append(config) or 0)

    with pytest.raises(SystemExit) as exit_info:
        cli.main(["--install", "--api-port", "18000"])

    assert exit_info.value.code == 0
    assert received == [DevServerConfig(api_port=18000, install=True)]


def test_cli_explicit_dev_arguments_are_dispatched(monkeypatch: pytest.MonkeyPatch) -> None:
    received: list[DevServerConfig] = []
    monkeypatch.setattr(devserver, "run_dev_server", lambda config: received.append(config) or 0)

    with pytest.raises(SystemExit) as exit_info:
        cli.main(
            [
                "dev",
                "--api-host",
                "0.0.0.0",
                "--api-port",
                "18000",
                "--web-host",
                "0.0.0.0",
                "--web-port",
                "15173",
                "--reload",
                "--install",
            ]
        )

    assert exit_info.value.code == 0
    assert received == [
        DevServerConfig(
            api_host="0.0.0.0",
            api_port=18000,
            web_host="0.0.0.0",
            web_port=15173,
            reload=True,
            install=True,
        )
    ]


def test_build_process_specs_constructs_commands_and_child_environments(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("PYTHONPATH", "existing-python-path")
    monkeypatch.delenv("HARMONY_CORS_ORIGINS", raising=False)
    config = DevServerConfig(
        api_host="0.0.0.0",
        api_port=18000,
        web_host="0.0.0.0",
        web_port=15173,
        reload=True,
    )

    api, web = build_process_specs(config, npm="npm-test")

    assert api.name == "api"
    assert api.cwd == devserver.ROOT
    assert api.command == (
        sys.executable,
        "-m",
        "uvicorn",
        "harmony_test_agent.api.app:create_app",
        "--factory",
        "--host",
        "0.0.0.0",
        "--port",
        "18000",
        "--reload",
    )
    assert api.env["HARMONY_CORS_ORIGINS"] == "http://127.0.0.1:15173"
    assert api.env["PYTHONUNBUFFERED"] == "1"
    assert api.env["PYTHONPATH"].split(devserver.os.pathsep) == [
        str(devserver.ROOT / "src"),
        "existing-python-path",
    ]

    assert web.name == "web"
    assert web.cwd == devserver.WEB_DIR
    assert web.command == (
        "npm-test",
        "run",
        "dev",
        "--",
        "--host",
        "0.0.0.0",
        "--port",
        "15173",
        "--strictPort",
    )
    assert web.env["VITE_API_PROXY_TARGET"] == "http://127.0.0.1:18000"
    assert "VITE_API_URL" not in web.env


def test_build_process_specs_brackets_ipv6_hosts(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("HARMONY_CORS_ORIGINS", raising=False)

    api, web = build_process_specs(
        DevServerConfig(api_host="::1", web_host="::1"),
        npm="npm-test",
    )

    assert api.env["HARMONY_CORS_ORIGINS"] == "http://[::1]:5173"
    assert web.env["VITE_API_PROXY_TARGET"] == "http://[::1]:8000"
    assert "VITE_API_URL" not in web.env


def test_build_process_specs_merges_configured_cors_origins(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(
        "HARMONY_CORS_ORIGINS",
        " http://localhost:5173, http://192.168.1.20:5173, http://127.0.0.1:5173 ",
    )

    api, _ = build_process_specs(DevServerConfig(), npm="npm-test")

    assert api.env["HARMONY_CORS_ORIGINS"].split(",") == [
        "http://127.0.0.1:5173",
        "http://localhost:5173",
        "http://192.168.1.20:5173",
    ]


def _prepare_web_tree(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, *, node_modules: bool) -> Path:
    web_dir = tmp_path / "web"
    web_dir.mkdir()
    (web_dir / "package.json").write_text("{}", encoding="utf-8")
    (web_dir / "package-lock.json").write_text("{}", encoding="utf-8")
    if node_modules:
        (web_dir / "node_modules").mkdir()
    monkeypatch.setattr(devserver, "WEB_DIR", web_dir)
    monkeypatch.setattr(devserver.shutil, "which", lambda executable: f"/tools/{executable}")
    return web_dir


def test_validate_environment_rejects_missing_npm(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(devserver.shutil, "which", lambda executable: "/tools/node" if executable == "node" else None)

    with pytest.raises(DevServerError, match="npm was not found"):
        validate_environment(DevServerConfig(), require_node_modules=False)


def test_validate_environment_rejects_missing_node_modules(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _prepare_web_tree(tmp_path, monkeypatch, node_modules=False)

    with pytest.raises(DevServerError, match="node_modules is missing"):
        validate_environment(DevServerConfig())


def test_validate_environment_rejects_an_occupied_port(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import socket

    _prepare_web_tree(tmp_path, monkeypatch, node_modules=True)
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as listener:
        listener.bind(("127.0.0.1", 0))
        occupied_port = listener.getsockname()[1]

        with pytest.raises(DevServerError, match=rf"API port 127\.0\.0\.1:{occupied_port} is unavailable"):
            validate_environment(DevServerConfig(api_port=occupied_port, web_port=occupied_port + 1))


def test_run_dev_server_returns_failed_child_code_and_cleans_up_both_children(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class FakeProcess:
        def __init__(self, returncode: int | None) -> None:
            self.returncode = returncode
            self.stdout = None

        def poll(self) -> int | None:
            return self.returncode

    api_process = FakeProcess(None)
    web_process = FakeProcess(7)
    processes = iter((api_process, web_process))
    specs = (
        ProcessSpec("api", ("api",), Path("api"), {}),
        ProcessSpec("web", ("web",), Path("web"), {}),
    )
    cleaned: list[FakeProcess] = []

    monkeypatch.setattr(devserver, "validate_environment", lambda config, require_node_modules: "npm-test")
    monkeypatch.setattr(devserver, "build_process_specs", lambda config, npm: specs)
    monkeypatch.setattr(devserver, "_start_process", lambda spec: next(processes))
    fake_thread = type("Thread", (), {"start": lambda self: None})
    monkeypatch.setattr(devserver.threading, "Thread", lambda **kwargs: fake_thread())
    monkeypatch.setattr(devserver, "_wait_for_url", lambda url, process, timeout: None)
    monkeypatch.setattr(devserver, "_terminate_process_tree", lambda process, timeout: cleaned.append(process))

    assert run_dev_server(DevServerConfig()) == 7
    assert cleaned == [web_process, api_process]
