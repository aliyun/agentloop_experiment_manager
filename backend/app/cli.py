from __future__ import annotations

import argparse
import asyncio
import getpass
import inspect
import json
import os
import stat
import sys
import threading
import time
import urllib.request
import webbrowser
from ipaddress import ip_address
from pathlib import Path
from typing import TYPE_CHECKING

from cryptography.fernet import Fernet
from platformdirs import user_data_path

from app import __version__
from app.config import ensure_private_data_dir
from app.secrets.encrypted_file import validate_encrypted_secret_file

if TYPE_CHECKING:
    from app.config import Settings

MASTER_KEY_FILE = "master.key"
MANAGED_AGENT_ENVELOPE_MAX_BYTES = 256 * 1024


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="agentloop-launcher",
        description="Start the customer-side AgentLoop experiment launcher.",
    )
    parser.add_argument(
        "command",
        nargs="?",
        choices=("start", "doctor", "reference-agent", "managed-agent"),
        default="start",
    )
    parser.add_argument("--version", action="version", version=__version__)
    parser.add_argument("--host", default=None, help="Listen address (default: 127.0.0.1)")
    parser.add_argument("--port", type=int, default=None, help="Listen port (default: 8090)")
    parser.add_argument(
        "--allow-remote-access",
        action=argparse.BooleanOptionalAction,
        default=None,
        help=(
            "Permit a non-loopback listen address. Requires production mode and an "
            "external or explicitly enabled local master key; the launcher does not provide "
            "user authentication."
        ),
    )
    parser.add_argument(
        "--use-local-master-key",
        action="store_true",
        help=(
            "Force loading or creating data-dir/master.key instead of using "
            "AGENTLOOP_LAUNCHER_MASTER_KEY in production. Intended for a single trusted host."
        ),
    )
    parser.add_argument("--data-dir", type=Path, default=None, help="Persistent data directory")
    parser.add_argument(
        "--environment",
        choices=("development", "production"),
        default=None,
        help="Runtime security mode (default: development)",
    )
    parser.add_argument(
        "--no-scheduler",
        action="store_true",
        help="Disable periodic experiment execution",
    )
    parser.add_argument(
        "--no-open-browser",
        action="store_true",
        help="Do not open the management UI automatically",
    )
    parser.add_argument(
        "--log-level",
        choices=("critical", "error", "warning", "info", "debug"),
        default="info",
    )
    parser.add_argument(
        "--doctor",
        action="store_true",
        help="Validate the packaged runtime and exit without starting a server",
    )
    parser.add_argument("--url", help="WebSocket connectUrl for the reference Agent")
    parser.add_argument(
        "--reconnect",
        action="store_true",
        help="Keep retrying the reference Agent connection after a failure",
    )
    return parser


def default_data_dir() -> Path:
    configured = os.environ.get("AGENTLOOP_LAUNCHER_DATA_DIR")
    if configured:
        return Path(configured).expanduser()
    return user_data_path(
        appname="AgentLoop Launcher",
        appauthor="Alibaba Cloud",
        ensure_exists=False,
    )


def load_or_create_master_key(data_dir: Path, configured_key: str | None) -> str:
    """Load an external key or atomically create the local single-user key."""

    ensure_private_data_dir(data_dir)
    secret_path = data_dir / "secrets.enc"
    validate_encrypted_secret_file(secret_path)
    if configured_key:
        Fernet(configured_key.encode("ascii"))
        return configured_key

    key_path = data_dir / MASTER_KEY_FILE
    if key_path.is_symlink():
        raise RuntimeError("master.key must not be a symbolic link")
    if key_path.exists():
        descriptor = os.open(
            key_path,
            os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0),
        )
        try:
            metadata = os.fstat(descriptor)
            if not stat.S_ISREG(metadata.st_mode):
                raise RuntimeError("master.key must be a regular file")
            if hasattr(os, "getuid") and metadata.st_uid != os.getuid():
                raise RuntimeError("master.key is owned by another user")
            if metadata.st_mode & 0o077:
                raise RuntimeError("master.key must not be group/world accessible")
            with os.fdopen(descriptor, "r", encoding="ascii") as stream:
                descriptor = -1
                key = stream.read().strip()
        finally:
            if descriptor >= 0:
                os.close(descriptor)
        Fernet(key.encode("ascii"))
        return key

    if secret_path.exists():
        raise RuntimeError(
            "Encrypted credentials already exist but master.key is missing. "
            "Restore the original key or set AGENTLOOP_LAUNCHER_MASTER_KEY."
        )

    generated = Fernet.generate_key().decode("ascii")
    descriptor = os.open(
        key_path,
        os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0),
        0o600,
    )
    try:
        with os.fdopen(descriptor, "w", encoding="ascii") as stream:
            stream.write(generated)
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
    except BaseException:
        key_path.unlink(missing_ok=True)
        raise
    key_path.chmod(0o600)
    return generated


def runtime_settings(args: argparse.Namespace) -> Settings:
    from app.config import Settings

    raw_data_dir = (args.data_dir or default_data_dir()).expanduser()
    data_dir = ensure_private_data_dir(raw_data_dir)
    configured = Settings(_env_file=None, data_dir=data_dir)
    environment = args.environment or configured.environment
    host = args.host or configured.host
    cli_remote_access = getattr(args, "allow_remote_access", None)
    allow_remote_access = (
        configured.allow_remote_access
        if cli_remote_access is None
        else cli_remote_access
    )
    use_local_master_key = bool(getattr(args, "use_local_master_key", False))
    if not _is_loopback_host(host):
        if not allow_remote_access:
            raise RuntimeError(
                "A non-loopback listen address requires --allow-remote-access (or "
                "AGENTLOOP_LAUNCHER_ALLOW_REMOTE_ACCESS=true). Prefer an authenticated "
                "TLS reverse proxy for remote access."
            )
        if environment != "production":
            raise RuntimeError(
                "A non-loopback listen address requires --environment production. "
                "Development mode remains local-only."
            )
    if environment == "production" and not configured.master_key and not use_local_master_key:
        raise RuntimeError(
            "Production mode requires AGENTLOOP_LAUNCHER_MASTER_KEY from an external secret "
            "manager or the explicit --use-local-master-key single-host mode."
        )
    configured_master_key = None if use_local_master_key else configured.master_key
    master_key = load_or_create_master_key(data_dir, configured_master_key)
    return configured.model_copy(
        update={
            "environment": environment,
            "host": host,
            "port": args.port or configured.port,
            "allow_remote_access": allow_remote_access,
            "data_dir": data_dir,
            "gateway_mode": "agentloop",
            "master_key": master_key,
            "scheduler_enabled": not args.no_scheduler,
        }
    )


def _is_loopback_host(value: str) -> bool:
    if value.casefold() == "localhost":
        return True
    try:
        return ip_address(value).is_loopback
    except ValueError:
        return False


def _open_when_ready(url: str) -> None:
    def worker() -> None:
        health_url = f"{url}/api/v1/health"
        for _ in range(100):
            try:
                with urllib.request.urlopen(health_url, timeout=0.25) as response:
                    if response.status == 200:
                        webbrowser.open(url)
                        return
            except OSError:
                time.sleep(0.1)

    threading.Thread(target=worker, name="browser-opener", daemon=True).start()


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    if args.command == "managed-agent":
        return run_managed_agent()
    if args.command == "reference-agent":
        return run_reference_agent(args)
    if args.command == "doctor" or args.doctor:
        return run_doctor(args)
    try:
        from app.web import require_web_dir

        web_dir = require_web_dir()
        settings = runtime_settings(args)
    except (OSError, RuntimeError, ValueError) as exc:
        print(f"Startup check failed: {exc}", file=sys.stderr)
        return 2

    remote_listen = not _is_loopback_host(settings.host)
    url_host = "127.0.0.1" if settings.host in {"0.0.0.0", "::"} else settings.host
    url = f"http://{_url_host(url_host)}:{settings.port}"
    print(f"AgentLoop Launcher {__version__}")
    print(f"UI: {url}")
    if remote_listen:
        print(f"Listen: http://{_url_host(settings.host)}:{settings.port}")
        if settings.host in {"0.0.0.0", "::"}:
            print(f"Remote UI: http://<server-ip>:{settings.port}")
        print(
            "WARNING: remote access is enabled, but this launcher has no built-in user "
            "authentication. Restrict the port to a trusted network or put it behind "
            "a TLS/VPN/SSO reverse proxy."
        )
    if args.use_local_master_key:
        print(f"Master key: {settings.data_dir / MASTER_KEY_FILE} (local single-host mode)")
    print(f"Data: {settings.data_dir}")
    if not args.no_open_browser:
        _open_when_ready(url)
    import uvicorn

    from app.domain.instance_lock import InstanceAlreadyRunningError, InstanceFileLock
    from app.main import create_app
    from app.runtime import build_container

    instance_lock = InstanceFileLock(settings.data_dir / "app.lock")
    try:
        instance_lock.acquire()
    except InstanceAlreadyRunningError:
        print(
            f"AgentLoop Launcher is already using {settings.data_dir}. "
            f"Open the existing service instead of starting a second instance.",
            file=sys.stderr,
        )
        return 3
    try:
        container = build_container(settings, instance_lock=instance_lock)
        uvicorn.run(
            create_app(settings=settings, container=container, web_dir=web_dir),
            host=settings.host,
            port=settings.port,
            log_level=args.log_level,
            access_log=False,
        )
    finally:
        instance_lock.release()
    return 0


def run_doctor(args: argparse.Namespace) -> int:
    """Read-only import and asset integrity check for a packaged runtime."""

    try:
        import sqlite3

        import uvicorn  # noqa: F401
        import websockets  # noqa: F401
        from agentloop_sdk import AgentLoopBenchmark  # noqa: F401
        from alibabacloud_agentloop20260520.client import Client  # noqa: F401
        from aliyun.log import GetLogsRequest, LogClient  # noqa: F401

        from app.config import Settings
        from app.main import create_app
        from app.web import require_web_dir

        web_dir = require_web_dir()
        index = (web_dir / "index.html").read_text(encoding="utf-8")
        referenced_assets = _frontend_assets(index)
        if not referenced_assets:
            raise RuntimeError("Frontend index.html does not reference built assets")
        missing = [asset for asset in referenced_assets if not (web_dir / asset).is_file()]
        if missing:
            raise RuntimeError(f"Frontend assets are missing: {', '.join(missing)}")
        with sqlite3.connect(":memory:") as connection:
            connection.execute("SELECT 1").fetchone()
        app = create_app(
            settings=Settings(_env_file=None, environment="test", gateway_mode="fake"),
            web_dir=web_dir,
        )
        if "/api/v1/health" not in app.openapi().get("paths", {}):
            raise RuntimeError("FastAPI routes are incomplete")
    except (OSError, RuntimeError, ValueError) as exc:
        print(f"Runtime check failed: {exc}", file=sys.stderr)
        return 2

    requested_data_dir = (args.data_dir or default_data_dir()).expanduser()
    print(f"AgentLoop Launcher {__version__}")
    print(f"Frontend: {web_dir}")
    print(f"Data (not initialized): {requested_data_dir}")
    print("Runtime check: OK")
    return 0


def _frontend_assets(index: str) -> list[str]:
    import re

    assets = re.findall(r"(?:src|href)=[\"'](/assets/[^\"']+)[\"']", index)
    return sorted({value.lstrip("/") for value in assets})


def _url_host(value: str) -> str:
    """Format an IPv4, IPv6, or hostname value for an HTTP URL."""

    try:
        parsed = ip_address(value)
    except ValueError:
        return value
    return f"[{value}]" if parsed.version == 6 else value


def run_reference_agent(args: argparse.Namespace) -> int:
    if not args.url:
        print("reference-agent requires --url", file=sys.stderr)
        return 2
    token = os.environ.get("AGENT_CONNECTION_TOKEN")
    if not token:
        token = getpass.getpass("Paste the one-time Agent connection token: ")
    if not token:
        print("Agent connection token is required", file=sys.stderr)
        return 2

    from app.agent_connector import AgentConnector
    from app.reference_agent import handle

    try:
        asyncio.run(
            AgentConnector(
                connect_url=args.url,
                token=token,
                handler=handle,
                reconnect=args.reconnect,
            ).run()
        )
    except KeyboardInterrupt:
        return 0
    except Exception as exc:
        print(
            f"Reference Agent connection failed ({type(exc).__name__}). "
            "Check the URL and rotate the one-time Token if necessary.",
            file=sys.stderr,
        )
        return 4
    return 0


def run_managed_agent() -> int:
    """Run trusted local code in a child process from a one-shot stdin envelope."""

    raw = bytearray()
    envelope: dict[str, object] = {}
    source = ""
    token = ""
    try:
        raw.extend(sys.stdin.buffer.readline(MANAGED_AGENT_ENVELOPE_MAX_BYTES + 1))
        if not raw or len(raw) > MANAGED_AGENT_ENVELOPE_MAX_BYTES or not raw.endswith(b"\n"):
            return 2
        parsed = json.loads(raw.decode("utf-8"))
        if not isinstance(parsed, dict):
            return 2
        envelope = parsed
        if envelope.get("protocolVersion") != 1 or envelope.get("language") != "python":
            return 2
        connect_url = envelope.get("connectUrl")
        token_value = envelope.get("token")
        source_value = envelope.get("source")
        if not all(isinstance(value, str) and value for value in (connect_url, token_value)):
            return 2
        if not isinstance(source_value, str) or not source_value.strip():
            return 2
        source = source_value
        token = token_value

        namespace: dict[str, object] = {
            "__builtins__": __builtins__,
            "__name__": "__managed_local_agent__",
        }
        executable = compile(source, "<managed-local-agent>", "exec", dont_inherit=True)
        exec(executable, namespace, namespace)
        configured_solution = namespace.get("solution_http")
        configured_handle = namespace.get("handle")
        if not callable(configured_solution) and not callable(configured_handle):
            return 2

        from agentloop_sdk import SolutionOutput, Task

        from app.agent_connector import AgentConnector, LocalAgentResponse

        async def managed_handler(payload: dict[str, object]):
            if callable(configured_solution):
                sdk_task = payload.get("sdkTask")
                if not isinstance(sdk_task, dict):
                    raise TypeError("Solution Provider task envelope is missing")
                task_input = sdk_task.get("input")
                if not isinstance(task_input, (dict, list, str, int, float, bool)):
                    if task_input is not None:
                        raise TypeError("Solution Provider task input is not JSON compatible")
                metadata = sdk_task.get("metadata")
                metadata = dict(metadata) if isinstance(metadata, dict) else {}
                tags = sdk_task.get("tags")
                if not isinstance(tags, dict) or not all(
                    isinstance(key, str) and isinstance(value, str) for key, value in tags.items()
                ):
                    tags = None
                task = Task(
                    id=str(sdk_task.get("id") or payload.get("requestId") or "local-task"),
                    input=task_input,
                    ground_truth=sdk_task.get("groundTruth"),
                    metrics=[],
                    tags=tags,
                    metadata=metadata,
                )
                result = configured_solution(task)
                if inspect.isawaitable(result):
                    result = await result
                if not isinstance(result, SolutionOutput):
                    raise TypeError("Solution Provider must return SolutionOutput")
                meta = result.meta or {}
                if not isinstance(meta, dict):
                    raise TypeError("SolutionOutput.meta must be an object")
                trajectory = result.trajectory or []
                if not isinstance(trajectory, list):
                    raise TypeError("SolutionOutput.trajectory must be an array")
                return LocalAgentResponse(
                    output=result.output,
                    meta=meta,
                    success=bool(result.success),
                    trajectory=trajectory,
                )

            assert callable(configured_handle)
            result = configured_handle(payload)
            if inspect.isawaitable(result):
                result = await result
            if isinstance(result, dict) and "output" in result:
                meta = result.get("meta", {})
                if not isinstance(meta, dict):
                    raise TypeError("managed Agent meta must be an object")
                return LocalAgentResponse(output=result["output"], meta=meta)
            return result

        connector = AgentConnector(
            connect_url=connect_url,
            token=token,
            handler=managed_handler,
            reconnect=False,
        )
        envelope.clear()
        source = ""
        token = ""
        asyncio.run(connector.run())
    except KeyboardInterrupt:
        return 0
    except Exception:
        return 4
    finally:
        envelope.clear()
        source = ""
        token = ""
        raw[:] = b"\0" * len(raw)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
