from pathlib import Path

import pytest

from src.core.grok.runtime import flaresolverr_manager as manager_module


class DummyProcess:
    def poll(self):
        return None


def test_parse_command_strips_wrapping_quotes_from_windows_path():
    manager = manager_module.FlareSolverrManager()
    command = r'"C:\Users\admin\AppData\Roaming\shuguang-desktop\shuguang\flaresolverr\current\flaresolverr\flaresolverr.exe" --verbose'

    parts = manager._parse_command(command)

    assert parts == [
        r"C:\Users\admin\AppData\Roaming\shuguang-desktop\shuguang\flaresolverr\current\flaresolverr\flaresolverr.exe",
        "--verbose",
    ]


def test_start_uses_executable_parent_as_workdir_for_absolute_path(tmp_path, monkeypatch):
    executable = tmp_path / "flaresolverr.exe"
    executable.write_text("", encoding="utf-8")
    captured = {}

    def fake_popen(parts, **kwargs):
        captured["parts"] = parts
        captured["cwd"] = kwargs.get("cwd")
        return DummyProcess()

    monkeypatch.setattr(manager_module.subprocess, "Popen", fake_popen)

    manager = manager_module.FlareSolverrManager()
    status = manager.start(f'"{executable}" --verbose')

    assert captured["parts"] == [str(executable), "--verbose"]
    assert captured["cwd"] == str(tmp_path)
    assert status["running"] is True


def test_start_falls_back_to_default_workdir_when_command_not_resolved(tmp_path, monkeypatch):
    fallback_dir = tmp_path / "runtime"
    fallback_dir.mkdir()
    captured = {}

    def fake_popen(parts, **kwargs):
        captured["parts"] = parts
        captured["cwd"] = kwargs.get("cwd")
        return DummyProcess()

    monkeypatch.setattr(manager_module.subprocess, "Popen", fake_popen)
    monkeypatch.setattr(manager_module.shutil, "which", lambda _: None)
    monkeypatch.setenv("APPDATA", str(tmp_path / "missing-appdata"))

    manager = manager_module.FlareSolverrManager()
    monkeypatch.setattr(manager, "_default_cwd", lambda: fallback_dir)
    manager.start("flaresolverr")

    assert captured["parts"] == ["flaresolverr"]
    assert captured["cwd"] == str(fallback_dir)


def test_start_discovers_shuguang_appdata_install(tmp_path, monkeypatch):
    install_dir = tmp_path / "shuguang-desktop" / "shuguang" / "flaresolverr" / "current" / "flaresolverr"
    install_dir.mkdir(parents=True)
    executable = install_dir / "flaresolverr.exe"
    executable.write_text("", encoding="utf-8")
    captured = {}

    def fake_popen(parts, **kwargs):
        captured["parts"] = parts
        captured["cwd"] = kwargs.get("cwd")
        return DummyProcess()

    monkeypatch.setattr(manager_module.subprocess, "Popen", fake_popen)
    monkeypatch.setattr(manager_module.shutil, "which", lambda _: None)
    monkeypatch.setenv("APPDATA", str(tmp_path))

    manager = manager_module.FlareSolverrManager()
    manager.start("flaresolverr")

    assert captured["parts"] == [str(executable)]
    assert captured["cwd"] == str(install_dir)


def test_start_reports_command_context_on_file_not_found(tmp_path, monkeypatch):
    missing_executable = tmp_path / "missing.exe"

    def fake_popen(parts, **kwargs):
        raise FileNotFoundError(2, "系统找不到指定的文件。", parts[0])

    monkeypatch.setattr(manager_module.subprocess, "Popen", fake_popen)

    manager = manager_module.FlareSolverrManager()

    with pytest.raises(RuntimeError) as exc_info:
        manager.start(f'"{missing_executable}"')

    message = str(exc_info.value)
    assert "command=" in message
    assert str(missing_executable) in message
    assert "cwd=" in message
