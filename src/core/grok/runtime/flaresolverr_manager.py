"""
FlareSolverr 托管管理器。
"""

from __future__ import annotations

import os
import shlex
import shutil
import subprocess
import sys
import threading
from pathlib import Path
from typing import Any, Dict, Optional

from .solver_manager import probe_http_service


class FlareSolverrManager:
    def __init__(self):
        self._lock = threading.Lock()
        self._process: Optional[subprocess.Popen] = None
        self._url = "http://127.0.0.1:8191/v1"
        self._last_error: Optional[str] = None

    def _parse_command(self, command: str) -> list[str]:
        parts = shlex.split(command, posix=False)
        return [part[1:-1] if len(part) >= 2 and part[0] == part[-1] and part[0] in {"'", '"'} else part for part in parts]

    def _default_cwd(self) -> Optional[Path]:
        if getattr(sys, "frozen", False):
            candidate = Path(sys.executable).resolve().parent
        else:
            candidate = Path(__file__).resolve().parents[4]
        return candidate if candidate.exists() else None

    def _candidate_command_paths(self, executable: str) -> list[Path]:
        raw_path = Path(executable).expanduser()
        names = [raw_path.name]
        if os.name == "nt" and raw_path.suffix.lower() != ".exe":
            names.append(f"{raw_path.name}.exe")

        roots: list[Path] = []
        default_cwd = self._default_cwd()
        if default_cwd:
            roots.extend([default_cwd, default_cwd / "vendor", default_cwd / "flaresolverr"])

        appdata = os.getenv("APPDATA")
        if appdata:
            roots.append(Path(appdata) / "shuguang-desktop" / "shuguang")

        candidates: list[Path] = []
        seen: set[str] = set()
        for root in roots:
            for name in names:
                for candidate in (
                    root / name,
                    root / "flaresolverr" / name,
                    root / "current" / "flaresolverr" / name,
                    root / "flaresolverr" / "current" / "flaresolverr" / name,
                ):
                    key = str(candidate)
                    if key in seen:
                        continue
                    seen.add(key)
                    candidates.append(candidate)
        return candidates

    def _resolve_command(self, executable: str) -> str:
        command_path = Path(executable).expanduser()
        if command_path.is_absolute():
            return str(command_path)

        resolved = shutil.which(executable)
        if resolved:
            return str(Path(resolved).resolve())

        for candidate in self._candidate_command_paths(executable):
            if candidate.is_file():
                return str(candidate.resolve())

        return executable

    def _resolve_cwd(self, executable: str) -> Optional[str]:
        command_path = Path(executable).expanduser()
        if command_path.is_absolute() and command_path.exists():
            return str(command_path.parent)

        resolved = shutil.which(executable)
        if resolved:
            return str(Path(resolved).resolve().parent)

        default_cwd = self._default_cwd()
        return str(default_cwd) if default_cwd else None

    def is_running(self) -> bool:
        return bool(self._process and self._process.poll() is None)

    def status(self, url: Optional[str] = None) -> Dict[str, Any]:
        effective_url = (url or self._url).strip() or self._url
        return {
            "running": self.is_running(),
            "managed": self.is_running(),
            "healthy": probe_http_service(effective_url),
            "url": effective_url,
            "last_error": self._last_error,
        }

    def start(self, command: str, url: Optional[str] = None) -> Dict[str, Any]:
        with self._lock:
            if self.is_running():
                return self.status(url=url)

            parts = self._parse_command(command)
            if not parts:
                raise RuntimeError("FlareSolverr command is empty.")

            executable = self._resolve_command(parts[0])
            parts[0] = executable
            working_dir = self._resolve_cwd(executable)
            creationflags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
            try:
                self._process = subprocess.Popen(
                    parts,
                    cwd=working_dir,
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                    creationflags=creationflags,
                )
            except FileNotFoundError as exc:
                context = f"{exc} (command={executable}, cwd={working_dir or '<current>'})"
                self._last_error = context
                raise RuntimeError(f"Failed to start FlareSolverr: {context}") from exc

            self._url = (url or self._url).strip() or self._url
            self._last_error = None
            return self.status(url=self._url)

    def stop(self, url: Optional[str] = None) -> Dict[str, Any]:
        with self._lock:
            if self._process and self._process.poll() is None:
                self._process.terminate()
                try:
                    self._process.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    self._process.kill()
                    self._process.wait(timeout=5)
            self._process = None
            return self.status(url=url)


_flaresolverr_manager = FlareSolverrManager()


def get_flaresolverr_manager() -> FlareSolverrManager:
    return _flaresolverr_manager
