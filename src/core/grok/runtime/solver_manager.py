"""
Managed Turnstile solver runtime helpers.
"""

from __future__ import annotations

import json
import os
import shlex
import shutil
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any, Dict, Optional

LOCAL_SOLVER_STARTUP_TIMEOUT_SECONDS = 60


def probe_http_service(url: str, *, timeout: float = 2.0) -> bool:
    try:
        request = urllib.request.Request(url, method="GET")
        with urllib.request.urlopen(request, timeout=timeout) as response:
            return 200 <= int(getattr(response, "status", 200)) < 500
    except urllib.error.HTTPError as exc:
        return 200 <= int(exc.code) < 500
    except Exception:
        return False


def _read_json_response(url: str, *, timeout: float = 2.0) -> Optional[dict[str, Any]]:
    try:
        request = urllib.request.Request(url, method="GET")
        with urllib.request.urlopen(request, timeout=timeout) as response:
            payload = response.read().decode("utf-8", "ignore")
    except urllib.error.HTTPError as exc:
        try:
            payload = exc.read().decode("utf-8", "ignore")
        except Exception:  # noqa: BLE001
            return None
    except Exception:
        return None

    try:
        data = json.loads(payload)
    except Exception:  # noqa: BLE001
        return None
    return data if isinstance(data, dict) else None


def inspect_local_solver_service(url: str, *, timeout: float = 2.0) -> Dict[str, Any]:
    base = (url or "").strip().rstrip("/")
    result = {
        "url": base or None,
        "reachable": False,
        "healthy": False,
        "placeholder": False,
        "service": None,
        "mode": None,
    }
    if not base:
        return result

    health_payload = _read_json_response(f"{base}/health", timeout=timeout)
    if health_payload is not None:
        result["reachable"] = True
        result["service"] = health_payload.get("service")
        result["mode"] = health_payload.get("mode")
        result["placeholder"] = str(health_payload.get("mode") or "").strip().lower() == "placeholder"
        result["healthy"] = not result["placeholder"]
        return result

    if probe_http_service(base, timeout=timeout):
        result["reachable"] = True
        result["healthy"] = True
    return result


def probe_local_solver_service(url: str, *, timeout: float = 2.0) -> bool:
    return bool(inspect_local_solver_service(url, timeout=timeout).get("healthy"))


class LocalSolverManager:
    def __init__(self):
        self._lock = threading.RLock()
        self._process: Optional[subprocess.Popen] = None
        self._port: Optional[int] = None
        self._command: Optional[str] = None
        self._last_error: Optional[str] = None

    @property
    def url(self) -> Optional[str]:
        if not self._port:
            return None
        return f"http://127.0.0.1:{self._port}"

    def _parse_command(self, command: str) -> list[str]:
        parts = shlex.split(command, posix=False)
        return [part[1:-1] if len(part) >= 2 and part[0] == part[-1] and part[0] in {"'", '"'} else part for part in parts]

    def _default_cwd(self) -> Optional[Path]:
        if getattr(sys, "frozen", False):
            candidate = Path(sys.executable).resolve().parent
        else:
            candidate = Path(__file__).resolve().parents[4]
        return candidate if candidate.exists() else None

    def _candidate_solver_scripts(self) -> list[Path]:
        repo_root = self._default_cwd()
        if not repo_root:
            return []
        roots = [
            repo_root / "vendor" / "grokzhuce",
            repo_root / "vendor" / "turnstile_solver",
            repo_root / "grokzhuce",
        ]
        if len(repo_root.parents) >= 4:
            workspace_root = repo_root.parents[3]
            roots.extend(
                [
                    workspace_root / "Visual Studio开发库" / "小工具" / "Grok注册机" / "grokzhuce",
                    workspace_root / "小工具" / "Grok注册机" / "grokzhuce",
                ]
            )
        seen: set[str] = set()
        candidates: list[Path] = []
        for root in roots:
            candidate = root / "api_solver.py"
            key = str(candidate)
            if key in seen:
                continue
            seen.add(key)
            candidates.append(candidate)
        return candidates

    def _discover_default_command(self, port: int) -> list[str]:
        for candidate in self._candidate_solver_scripts():
            if candidate.is_file():
                return [
                    sys.executable,
                    str(candidate.resolve()),
                    "--host",
                    "127.0.0.1",
                    "--port",
                    str(port),
                    "--browser_type",
                    "camoufox",
                    "--thread",
                    "5",
                ]
        raise RuntimeError(
            "TurnstileSolver was not found. Set the Grok solver command or place api_solver.py in a discoverable path."
        )

    def _resolve_executable(self, executable: str) -> str:
        command_path = Path(executable).expanduser()
        if command_path.is_absolute():
            return str(command_path)
        resolved = shutil.which(executable)
        if resolved:
            return str(Path(resolved).resolve())
        return executable

    def _ensure_solver_args(self, parts: list[str], port: int) -> list[str]:
        prepared = list(parts)
        if prepared and prepared[0].lower().endswith(".py"):
            prepared.insert(0, sys.executable)
        if prepared:
            prepared[0] = self._resolve_executable(prepared[0])
        has_host = any(arg == "--host" or arg.startswith("--host=") for arg in prepared)
        has_port = any(arg == "--port" or arg.startswith("--port=") for arg in prepared)
        if not has_host:
            prepared.extend(["--host", "127.0.0.1"])
        if not has_port:
            prepared.extend(["--port", str(port)])
        return prepared

    def _resolve_launch_parts(self, command: Optional[str], port: int) -> list[str]:
        if command and command.strip():
            parts = self._parse_command(command)
            if not parts:
                raise RuntimeError("TurnstileSolver command is empty.")
            return self._ensure_solver_args(parts, port)
        return self._discover_default_command(port)

    def _resolve_cwd(self, parts: list[str]) -> Optional[str]:
        script_index = 1 if len(parts) >= 2 and parts[1].lower().endswith(".py") else None
        if script_index is not None:
            script_path = Path(parts[script_index]).expanduser()
            if script_path.exists():
                return str(script_path.resolve().parent)
        executable = Path(parts[0]).expanduser()
        if executable.is_absolute() and executable.exists():
            return str(executable.parent)
        default_cwd = self._default_cwd()
        return str(default_cwd) if default_cwd else None

    def _build_launch_env(self) -> dict[str, str]:
        env = os.environ.copy()
        env.setdefault("PYTHONUTF8", "1")
        env.setdefault("PYTHONIOENCODING", "utf-8")
        env.setdefault("NO_COLOR", "1")
        env.setdefault("TERM", "dumb")
        return env

    def is_running(self) -> bool:
        return bool(self._process and self._process.poll() is None)

    def status(self, url: Optional[str] = None) -> Dict[str, Any]:
        effective_url = (url or self.url or "").strip() or None
        details = inspect_local_solver_service(effective_url) if effective_url else {}
        return {
            "running": self.is_running(),
            "managed": self.is_running(),
            "healthy": bool(details.get("healthy")),
            "placeholder": bool(details.get("placeholder")),
            "url": effective_url,
            "command": self._command,
            "last_error": self._last_error,
        }

    def start(self, port: int, command: Optional[str] = None) -> Dict[str, Any]:
        with self._lock:
            if self.is_running() and self._port == port:
                return self.status()
            if self.is_running():
                self.stop()

            self._port = port
            current_url = self.url
            current_details = inspect_local_solver_service(current_url) if current_url else {}
            if current_details.get("placeholder"):
                self._last_error = (
                    f"Placeholder local solver is already running on {current_url}. "
                    "Stop the old local_solver_app process and retry."
                )
                raise RuntimeError(self._last_error)
            parts = self._resolve_launch_parts(command, port)
            working_dir = self._resolve_cwd(parts)
            self._command = subprocess.list2cmdline(parts)
            creationflags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
            try:
                self._process = subprocess.Popen(
                    parts,
                    cwd=working_dir,
                    env=self._build_launch_env(),
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                    creationflags=creationflags,
                )
            except FileNotFoundError as exc:
                context = f"{exc} (command={parts[0]}, cwd={working_dir or '<current>'})"
                self._last_error = context
                raise RuntimeError(f"Failed to start TurnstileSolver: {context}") from exc

            deadline = time.time() + LOCAL_SOLVER_STARTUP_TIMEOUT_SECONDS
            while time.time() < deadline:
                if self._process.poll() is not None:
                    self._last_error = "Managed TurnstileSolver exited unexpectedly during startup."
                    break
                if self.url and probe_local_solver_service(self.url):
                    self._last_error = None
                    return self.status()
                time.sleep(0.2)

            self.stop()
            if not self._last_error:
                self._last_error = (
                    f"Managed TurnstileSolver did not become healthy within "
                    f"{LOCAL_SOLVER_STARTUP_TIMEOUT_SECONDS} seconds."
                )
            raise RuntimeError(self._last_error)

    def stop(self) -> Dict[str, Any]:
        with self._lock:
            if self._process and self._process.poll() is None:
                self._process.terminate()
                try:
                    self._process.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    self._process.kill()
                    self._process.wait(timeout=5)
            self._process = None
            return self.status()


_local_solver_manager = LocalSolverManager()


def get_local_solver_manager() -> LocalSolverManager:
    return _local_solver_manager
