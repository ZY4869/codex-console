import asyncio
import importlib
import socket
from pathlib import Path

import pytest
from fastapi import BackgroundTasks, HTTPException
from fastapi.testclient import TestClient

from src.config import settings as settings_module
from src.database import grok_crud
from src.database import session as db_session
from src.database.init_db import initialize_database
from src.database.session import get_db
from src.web.routes import grok_register as grok_register_routes
from src.web.task_manager import task_manager


@pytest.fixture()
def temp_database(tmp_path, monkeypatch):
    db_url = f"sqlite:///{tmp_path / 'grok-routes.db'}"
    monkeypatch.setenv("APP_DATABASE_URL", db_url)
    monkeypatch.setenv("APP_DATA_DIR", str(tmp_path))
    db_session._db_manager = None
    settings_module._settings = None
    initialize_database(db_url)
    yield
    db_session._db_manager = None
    settings_module._settings = None


@pytest.fixture()
def app_module(tmp_path, monkeypatch):
    db_url = f"sqlite:///{tmp_path / 'grok-app.db'}"
    monkeypatch.setenv("APP_DATABASE_URL", db_url)
    monkeypatch.setenv("APP_DATA_DIR", str(tmp_path))
    db_session._db_manager = None
    settings_module._settings = None

    module = importlib.import_module("src.web.app")
    module = importlib.reload(module)
    yield module
    db_session._db_manager = None
    settings_module._settings = None


def pick_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def test_grok_config_roundtrip_secret_flags(temp_database):
    config = asyncio.run(grok_register_routes.get_grok_register_config())
    assert config["has_bczy_api_key"] is False
    assert config["has_yescaptcha_key"] is False
    assert config["email_service_type"] == "auto"
    assert config["email_service_id"] is None

    asyncio.run(
        grok_register_routes.update_grok_register_config(
            grok_register_routes.GrokRegisterConfigUpdateRequest(
                target_count=3,
                thread_count=2,
                proxy="http://127.0.0.1:8080",
                email_service_type="freemail",
                email_service_id=7,
                solver_command="python api_solver.py --browser_type camoufox",
                bczy_api_key="bczy-secret",
                yescaptcha_key="yes-secret",
            )
        )
    )

    updated = asyncio.run(grok_register_routes.get_grok_register_config())
    assert updated["target_count"] == 3
    assert updated["thread_count"] == 2
    assert updated["proxy"] == "http://127.0.0.1:8080"
    assert updated["email_service_type"] == "freemail"
    assert updated["email_service_id"] == 7
    assert updated["solver_command"] == "python api_solver.py --browser_type camoufox"
    assert updated["has_bczy_api_key"] is True
    assert updated["has_yescaptcha_key"] is True

    asyncio.run(
        grok_register_routes.update_grok_register_config(
            grok_register_routes.GrokRegisterConfigUpdateRequest(
                email_service_type="auto",
                email_service_id=None,
                solver_command="",
                bczy_api_key="",
                yescaptcha_key="",
            )
        )
    )

    cleared = asyncio.run(grok_register_routes.get_grok_register_config())
    assert cleared["email_service_type"] == "auto"
    assert cleared["email_service_id"] is None
    assert cleared["solver_command"] == ""
    assert cleared["has_bczy_api_key"] is False
    assert cleared["has_yescaptcha_key"] is False


def test_create_grok_task_uses_saved_defaults(temp_database, monkeypatch):
    settings_module.update_settings(
        grok_default_proxy="http://127.0.0.1:9000",
        grok_default_email_service_type="freemail",
        grok_default_email_service_id="9",
        grok_default_bczy_api_key="saved-bczy",
        grok_default_yescaptcha_key="saved-yes",
        grok_default_solver_url="http://127.0.0.1:5072",
        grok_solver_command="python api_solver.py --browser_type camoufox",
    )
    monkeypatch.setattr(grok_register_routes, "_run_grok_task", lambda task_uuid: None)

    background_tasks = BackgroundTasks()
    response = asyncio.run(
        grok_register_routes.create_grok_register_task(
            grok_register_routes.GrokRegisterCreateRequest(
                target_count=2,
                thread_count=1,
            ),
            background_tasks,
        )
    )

    assert response["proxy"] == "http://127.0.0.1:9000"
    assert response["target_count"] == 2
    assert response["config"]["email_service_type"] == "freemail"
    assert response["config"]["email_service_id"] == 9
    assert response["config"]["has_bczy_api_key"] is True
    assert response["config"]["has_yescaptcha_key"] is True
    assert len(background_tasks.tasks) == 1

    with get_db() as db:
        task = grok_crud.get_grok_task(db, response["task_uuid"])
        assert task.config["email_service_type"] == "freemail"
        assert task.config["email_service_id"] == 9
        assert task.config["solver_url"] == "http://127.0.0.1:5072"
        assert task.config["solver_command"] == "python api_solver.py --browser_type camoufox"


def test_create_grok_task_falls_back_to_global_proxy(temp_database, monkeypatch):
    settings_module.update_settings(
        grok_default_proxy="",
        proxy_enabled=True,
        proxy_type="http",
        proxy_host="127.0.0.1",
        proxy_port=8899,
    )
    monkeypatch.setattr(grok_register_routes, "_run_grok_task", lambda task_uuid: None)

    background_tasks = BackgroundTasks()
    response = asyncio.run(
        grok_register_routes.create_grok_register_task(
            grok_register_routes.GrokRegisterCreateRequest(
                target_count=1,
                thread_count=1,
            ),
            background_tasks,
        )
    )

    assert response["proxy"] == "http://127.0.0.1:8899"
    with get_db() as db:
        task = grok_crud.get_grok_task(db, response["task_uuid"])
        assert task.proxy == "http://127.0.0.1:8899"


def test_create_grok_task_rejects_unreachable_local_solver(temp_database, monkeypatch):
    monkeypatch.setattr(grok_register_routes, "probe_local_solver_service", lambda url: False)

    with pytest.raises(HTTPException) as exc_info:
        asyncio.run(
            grok_register_routes.create_grok_register_task(
                grok_register_routes.GrokRegisterCreateRequest(
                    captcha_mode="local",
                    solver_url="http://10.0.0.8:5072",
                ),
                BackgroundTasks(),
            )
        )

    assert exc_info.value.status_code == 400
    assert "Local solver" in exc_info.value.detail


def test_create_grok_task_autostarts_managed_local_solver(temp_database, monkeypatch):
    settings_module.update_settings(grok_solver_command="python api_solver.py --browser_type camoufox")
    monkeypatch.setattr(grok_register_routes, "_run_grok_task", lambda task_uuid: None)

    calls = {"start": None}
    probe_calls = {"count": 0}

    class FakeManager:
        def start(self, port, command=None):
            calls["start"] = (port, command)
            return {
                "running": True,
                "managed": True,
                "healthy": True,
                "url": f"http://127.0.0.1:{port}",
                "command": command,
                "last_error": None,
            }

    def fake_probe(url):
        probe_calls["count"] += 1
        return probe_calls["count"] >= 2 and url == "http://127.0.0.1:5072"

    monkeypatch.setattr(grok_register_routes, "probe_local_solver_service", fake_probe)
    monkeypatch.setattr(grok_register_routes, "get_local_solver_manager", lambda: FakeManager())

    background_tasks = BackgroundTasks()
    response = asyncio.run(
        grok_register_routes.create_grok_register_task(
            grok_register_routes.GrokRegisterCreateRequest(captcha_mode="local"),
            background_tasks,
        )
    )

    assert calls["start"] == (5072, "python api_solver.py --browser_type camoufox")
    assert response["config"]["solver_url"] == "http://127.0.0.1:5072"
    assert len(background_tasks.tasks) == 1


def test_cancel_pending_task_marks_cancelled(temp_database):
    with get_db() as db:
        task = grok_crud.create_grok_task(
            db,
            task_uuid="grok-pending-task",
            target_count=1,
            thread_count=1,
            config={},
        )

    response = asyncio.run(grok_register_routes.cancel_grok_register_task("grok-pending-task"))

    assert response["success"] is True
    with get_db() as db:
        saved = grok_crud.get_grok_task(db, "grok-pending-task")
        assert saved.status == "cancelled"


def test_solver_start_stop_status(temp_database, monkeypatch):
    port = pick_port()
    settings_module.update_settings(grok_solver_command="python api_solver.py --browser_type camoufox")
    calls = {"start": None, "stop": 0}
    state = {"running": False}

    class FakeManager:
        def start(self, port, command=None):
            state["running"] = True
            calls["start"] = (port, command)
            return self.status()

        def stop(self):
            state["running"] = False
            calls["stop"] += 1
            return self.status()

        def is_running(self):
            return state["running"]

        def status(self):
            return {
                "running": state["running"],
                "managed": state["running"],
                "healthy": state["running"],
                "url": f"http://127.0.0.1:{port}",
                "command": "python api_solver.py --browser_type camoufox",
                "last_error": None,
            }

    monkeypatch.setattr(grok_register_routes, "get_local_solver_manager", lambda: FakeManager())
    status = asyncio.run(
        grok_register_routes.start_local_solver(
            grok_register_routes.GrokRuntimeActionRequest(port=port)
        )
    )
    assert status["running"] is True
    assert status["url"].endswith(str(port))
    assert calls["start"] == (port, "python api_solver.py --browser_type camoufox")
    current = asyncio.run(grok_register_routes.get_local_solver_status(None))
    assert current["running"] is True
    stopped = asyncio.run(grok_register_routes.stop_local_solver())
    assert stopped["running"] is False
    assert calls["stop"] == 1


def test_grok_page_requires_auth(app_module):
    with TestClient(app_module.app) as client:
        response = client.get("/grok", follow_redirects=False)

    assert response.status_code == 302
    assert response.headers["location"].startswith("/login")


def test_grok_page_renders_after_login(app_module):
    with TestClient(app_module.app) as client:
        login = client.post("/login", data={"password": "admin123", "next": "/grok"}, follow_redirects=False)
        assert login.status_code == 302
        response = client.get("/grok")

    assert response.status_code == 200
    assert "Grok Register Console" in response.text


def test_grok_websocket_alias_streams_status_and_logs(app_module):
    task_uuid = "grok-ws-task"
    task_manager.update_status(task_uuid, "running", snapshot={"status": "running", "task_uuid": task_uuid})

    with TestClient(app_module.app) as client:
        with client.websocket_connect(f"/api/ws/grok-register/{task_uuid}") as websocket:
            first = websocket.receive_json()
            task_manager.add_log(task_uuid, "[00:00:00] hello")
            second = websocket.receive_json()

    assert first["type"] == "status"
    assert second["type"] == "log"
    assert second["message"] == "[00:00:00] hello"


def test_navigation_templates_link_to_grok():
    templates = [
        Path("templates/index.html"),
        Path("templates/accounts.html"),
        Path("templates/email_services.html"),
        Path("templates/payment.html"),
        Path("templates/settings.html"),
        Path("templates/team.html"),
        Path("templates/team_invite.html"),
    ]

    for template_path in templates:
        template = template_path.read_text(encoding="utf-8")
        assert '/grok' in template, f"{template_path} is missing the Grok navigation link"
