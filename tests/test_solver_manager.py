from pathlib import Path

from src.core.grok.runtime import solver_manager as manager_module


class DummyProcess:
    def poll(self):
        return None

    def terminate(self):
        return None

    def wait(self, timeout=None):
        return None


def test_parse_command_strips_wrapping_quotes_from_windows_path():
    manager = manager_module.LocalSolverManager()
    command = r'"D:\tools\grokzhuce\api_solver.py" --browser_type camoufox'

    parts = manager._parse_command(command)

    assert parts == [r"D:\tools\grokzhuce\api_solver.py", "--browser_type", "camoufox"]


def test_probe_local_solver_service_falls_back_to_root(monkeypatch):
    seen = []

    monkeypatch.setattr(manager_module, "_read_json_response", lambda url, timeout=2.0: None)

    def fake_probe(url, timeout=2.0):
        seen.append(url)
        return url == "http://127.0.0.1:5072"

    monkeypatch.setattr(manager_module, "probe_http_service", fake_probe)

    assert manager_module.probe_local_solver_service("http://127.0.0.1:5072") is True
    assert seen == ["http://127.0.0.1:5072"]


def test_probe_local_solver_service_rejects_placeholder(monkeypatch):
    monkeypatch.setattr(
        manager_module,
        "_read_json_response",
        lambda url, timeout=2.0: {"ok": True, "service": "grok-local-solver", "mode": "placeholder"},
    )

    details = manager_module.inspect_local_solver_service("http://127.0.0.1:5072")

    assert details["placeholder"] is True
    assert details["healthy"] is False
    assert manager_module.probe_local_solver_service("http://127.0.0.1:5072") is False


def test_resolve_launch_parts_discovers_api_solver(tmp_path, monkeypatch):
    script = tmp_path / "api_solver.py"
    script.write_text("", encoding="utf-8")
    manager = manager_module.LocalSolverManager()
    monkeypatch.setattr(manager, "_candidate_solver_scripts", lambda: [script])

    parts = manager._resolve_launch_parts(None, 5072)

    assert parts[:2] == [manager_module.sys.executable, str(script.resolve())]
    assert "--port" in parts
    assert "5072" in parts


def test_start_uses_script_parent_as_workdir(tmp_path, monkeypatch):
    script = tmp_path / "api_solver.py"
    script.write_text("", encoding="utf-8")
    captured = {}

    def fake_popen(parts, **kwargs):
        captured["parts"] = parts
        captured["cwd"] = kwargs.get("cwd")
        captured["env"] = kwargs.get("env")
        return DummyProcess()

    monkeypatch.setattr(manager_module.subprocess, "Popen", fake_popen)
    monkeypatch.setattr(manager_module, "probe_local_solver_service", lambda url, timeout=2.0: True)
    monkeypatch.setattr(
        manager_module,
        "inspect_local_solver_service",
        lambda url, timeout=2.0: {"healthy": False, "placeholder": False, "url": url},
    )

    manager = manager_module.LocalSolverManager()
    status = manager.start(5072, command=f'"{script}" --browser_type camoufox')

    assert captured["parts"][0] == manager_module.sys.executable
    assert captured["parts"][1] == str(script)
    assert captured["cwd"] == str(tmp_path)
    assert captured["env"]["PYTHONUTF8"] == "1"
    assert captured["env"]["PYTHONIOENCODING"] == "utf-8"
    assert status["running"] is True


def test_start_reports_command_context_on_file_not_found(tmp_path, monkeypatch):
    missing = tmp_path / "api_solver.py"

    def fake_popen(parts, **kwargs):
        raise FileNotFoundError(2, "系统找不到指定的文件。", parts[0])

    monkeypatch.setattr(manager_module.subprocess, "Popen", fake_popen)
    monkeypatch.setattr(
        manager_module,
        "inspect_local_solver_service",
        lambda url, timeout=2.0: {"healthy": False, "placeholder": False, "url": url},
    )

    manager = manager_module.LocalSolverManager()

    try:
        manager.start(5072, command=f'"{missing}"')
    except RuntimeError as exc:
        message = str(exc)
    else:
        raise AssertionError("Expected RuntimeError")

    assert "command=" in message
    assert "cwd=" in message
    assert "TurnstileSolver" in message


def test_start_rejects_existing_placeholder_service(monkeypatch):
    manager = manager_module.LocalSolverManager()
    monkeypatch.setattr(
        manager_module,
        "inspect_local_solver_service",
        lambda url, timeout=2.0: {"healthy": False, "placeholder": True, "url": url},
    )

    try:
        manager.start(5072, command="python api_solver.py")
    except RuntimeError as exc:
        message = str(exc)
    else:
        raise AssertionError("Expected RuntimeError")

    assert "placeholder" in message.lower()


def test_build_launch_env_enforces_utf8_defaults(monkeypatch):
    monkeypatch.delenv("PYTHONUTF8", raising=False)
    monkeypatch.delenv("PYTHONIOENCODING", raising=False)
    manager = manager_module.LocalSolverManager()

    env = manager._build_launch_env()

    assert env["PYTHONUTF8"] == "1"
    assert env["PYTHONIOENCODING"] == "utf-8"
    assert env["NO_COLOR"] == "1"
    assert env["TERM"] == "dumb"
