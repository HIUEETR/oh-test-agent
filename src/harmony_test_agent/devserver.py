"""Supervise the local FastAPI and Vite development servers from one command."""

from __future__ import annotations

import contextlib
import os
import shutil
import signal
import socket
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from typing import IO

ROOT = Path(__file__).resolve().parents[2]
WEB_DIR = ROOT / "web"


class DevServerError(RuntimeError):
    """A user-correctable development server startup error."""


@dataclass(frozen=True, slots=True)
class DevServerConfig:
    api_host: str = "127.0.0.1"
    api_port: int = 8000
    web_host: str = "127.0.0.1"
    web_port: int = 5173
    reload: bool = False
    install: bool = False
    startup_timeout: float = 30.0
    shutdown_timeout: float = 8.0


@dataclass(frozen=True, slots=True)
class ProcessSpec:
    name: str
    command: tuple[str, ...]
    cwd: Path
    env: dict[str, str]


def _url_host(host: str) -> str:
    if host in {"0.0.0.0", "::"}:
        return "127.0.0.1"
    if ":" in host and not host.startswith("["):
        return f"[{host}]"
    return host


def _find_npm() -> str | None:
    return shutil.which("npm.cmd" if os.name == "nt" else "npm") or shutil.which("npm")


def validate_environment(config: DevServerConfig, *, require_node_modules: bool = True) -> str:
    """Validate local tools, files, and ports before starting either child."""
    for label, port in (("API", config.api_port), ("Web", config.web_port)):
        if not 1 <= port <= 65535:
            raise DevServerError(f"{label} port must be between 1 and 65535: {port}")
    if config.api_host == config.web_host and config.api_port == config.web_port:
        raise DevServerError("API and Web cannot use the same host and port")
    if shutil.which("node") is None:
        raise DevServerError("Node.js was not found in PATH; install Node.js 24 or a Vite-compatible version")
    npm = _find_npm()
    if npm is None:
        raise DevServerError("npm was not found in PATH")
    if not (WEB_DIR / "package.json").is_file() or not (WEB_DIR / "package-lock.json").is_file():
        raise DevServerError(f"Web package files are missing under {WEB_DIR}")
    if require_node_modules and not (WEB_DIR / "node_modules").is_dir():
        raise DevServerError("web/node_modules is missing; run `uv run main.py --install` once")
    _assert_port_available(config.api_host, config.api_port, "API")
    _assert_port_available(config.web_host, config.web_port, "Web")
    return npm


def _assert_port_available(host: str, port: int, label: str) -> None:
    family = socket.AF_INET6 if ":" in host else socket.AF_INET
    try:
        with socket.socket(family, socket.SOCK_STREAM) as probe:
            probe.bind((host, port))
    except OSError as exc:
        raise DevServerError(f"{label} port {host}:{port} is unavailable: {exc}") from exc


def build_process_specs(config: DevServerConfig, npm: str | None = None) -> tuple[ProcessSpec, ProcessSpec]:
    """Build deterministic child commands and their isolated environments."""
    npm = npm or _find_npm()
    if npm is None:
        raise DevServerError("npm was not found in PATH")
    api_url = f"http://{_url_host(config.api_host)}:{config.api_port}"
    web_origin = f"http://{_url_host(config.web_host)}:{config.web_port}"
    shared_env = os.environ.copy()
    configured_origins = (
        origin.strip() for origin in shared_env.get("HARMONY_CORS_ORIGINS", "").split(",") if origin.strip()
    )
    cors_origins = list(dict.fromkeys((web_origin, *configured_origins)))
    python_path = str(ROOT / "src")
    if shared_env.get("PYTHONPATH"):
        python_path += os.pathsep + shared_env["PYTHONPATH"]

    api_env = shared_env | {
        "HARMONY_CORS_ORIGINS": ",".join(cors_origins),
        "PYTHONPATH": python_path,
        "PYTHONUNBUFFERED": "1",
    }
    web_env = shared_env | {"VITE_API_PROXY_TARGET": api_url}
    web_env.pop("VITE_API_URL", None)
    api_command = [
        sys.executable,
        "-m",
        "uvicorn",
        "harmony_test_agent.api.app:create_app",
        "--factory",
        "--host",
        config.api_host,
        "--port",
        str(config.api_port),
    ]
    if config.reload:
        api_command.append("--reload")
    web_command = [
        npm,
        "run",
        "dev",
        "--",
        "--host",
        config.web_host,
        "--port",
        str(config.web_port),
        "--strictPort",
    ]
    return (
        ProcessSpec("api", tuple(api_command), ROOT, api_env),
        ProcessSpec("web", tuple(web_command), WEB_DIR, web_env),
    )


def _creation_flags() -> int:
    return subprocess.CREATE_NEW_PROCESS_GROUP if os.name == "nt" else 0


def _start_process(spec: ProcessSpec) -> subprocess.Popen[str]:
    return subprocess.Popen(
        spec.command,
        cwd=spec.cwd,
        env=spec.env,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        encoding="utf-8",
        errors="replace",
        bufsize=1,
        creationflags=_creation_flags(),
        start_new_session=os.name != "nt",
    )


def _stream_output(name: str, stream: IO[str] | None) -> None:
    if stream is None:
        return
    for line in iter(stream.readline, ""):
        print(f"[{name}] {line.rstrip()}", flush=True)


def _wait_for_url(url: str, process: subprocess.Popen[str], timeout: float) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        code = process.poll()
        if code is not None:
            raise DevServerError(f"{url} did not become ready; child exited with {code}")
        try:
            with urllib.request.urlopen(url, timeout=1) as response:
                if 200 <= response.status < 500:
                    return
        except OSError, urllib.error.URLError:
            time.sleep(0.2)
    raise DevServerError(f"timed out waiting for {url}")


def _terminate_process_tree(process: subprocess.Popen[str], timeout: float) -> None:
    if process.poll() is not None:
        return
    if os.name == "nt":
        with contextlib.suppress(OSError, ValueError):
            process.send_signal(signal.CTRL_BREAK_EVENT)
    else:
        with contextlib.suppress(ProcessLookupError):
            os.killpg(process.pid, signal.SIGTERM)
    try:
        process.wait(timeout=timeout)
        return
    except subprocess.TimeoutExpired:
        pass
    if os.name == "nt":
        subprocess.run(
            ["taskkill", "/PID", str(process.pid), "/T", "/F"],
            check=False,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
    else:
        with contextlib.suppress(ProcessLookupError):
            os.killpg(process.pid, signal.SIGKILL)
    with contextlib.suppress(subprocess.TimeoutExpired):
        process.wait(timeout=2)


def _install_web_dependencies(npm: str) -> None:
    print("[dev] Installing locked Web dependencies with npm ci...", flush=True)
    completed = subprocess.run([npm, "ci"], cwd=WEB_DIR, check=False)
    if completed.returncode:
        raise DevServerError(f"npm ci failed with exit code {completed.returncode}")


def run_dev_server(config: DevServerConfig) -> int:
    """Start both children, wait for readiness, and own their complete lifetime."""
    npm = validate_environment(config, require_node_modules=not config.install)
    if config.install:
        _install_web_dependencies(npm)
        if not (WEB_DIR / "node_modules").is_dir():
            raise DevServerError("npm ci completed without creating web/node_modules")
    api_spec, web_spec = build_process_specs(config, npm)
    processes: list[subprocess.Popen[str]] = []
    try:
        for spec in (api_spec, web_spec):
            process = _start_process(spec)
            processes.append(process)
            threading.Thread(target=_stream_output, args=(spec.name, process.stdout), daemon=True).start()
        api_process, web_process = processes
        api_url = f"http://{_url_host(config.api_host)}:{config.api_port}"
        web_url = f"http://{_url_host(config.web_host)}:{config.web_port}"
        _wait_for_url(f"{api_url}/api/health/live", api_process, config.startup_timeout)
        _wait_for_url(web_url, web_process, config.startup_timeout)
        print(f"[dev] API ready: {api_url}", flush=True)
        print(f"[dev] Web ready: {web_url}", flush=True)
        print(f"[dev] Health: {api_url}/api/health/live", flush=True)
        print("[dev] Press Ctrl+C to stop both services.", flush=True)
        while True:
            for process in processes:
                code = process.poll()
                if code is not None:
                    return code if code != 0 else 1
            time.sleep(0.2)
    except KeyboardInterrupt:
        print("\n[dev] Stopping API and Web...", flush=True)
        return 130
    finally:
        for process in reversed(processes):
            _terminate_process_tree(process, config.shutdown_timeout)
