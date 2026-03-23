import asyncio
from contextlib import contextmanager
from pathlib import Path

from src.database import crud
from src.database.models import Base
from src.database.session import DatabaseSessionManager
from src.web import task_manager as task_manager_module
from src.web.routes import registration as registration_routes


class DummyRegistrationResult:
    def __init__(self, email: str):
        self.email = email

    def to_dict(self):
        return {"email": self.email}


def _build_db_manager(name: str) -> DatabaseSessionManager:
    runtime_dir = Path("tests_runtime")
    runtime_dir.mkdir(exist_ok=True)
    db_path = runtime_dir / name
    if db_path.exists():
        db_path.unlink()

    manager = DatabaseSessionManager(f"sqlite:///{db_path}")
    Base.metadata.create_all(bind=manager.engine)
    return manager


def test_get_task_logs_merges_runtime_state_and_memory_logs(monkeypatch):
    manager = _build_db_manager("registration_runtime_logs.db")
    task_uuid = "runtime-logs-task"

    with manager.session_scope() as session:
        crud.create_registration_task(session, task_uuid=task_uuid)
        crud.append_task_log(session, task_uuid, "[系统] 数据库日志")

    @contextmanager
    def fake_get_db():
        session = manager.SessionLocal()
        try:
            yield session
        finally:
            session.close()

    monkeypatch.setattr(registration_routes, "get_db", fake_get_db)

    task_manager_module.task_manager.update_status(
        task_uuid,
        "running",
        attempt=2,
        max_attempts=4,
        retrying=True,
        last_error="temporary failure",
        next_retry_in_seconds=4,
        email="runtime@example.com",
        email_service="freemail",
    )
    task_manager_module.task_manager.add_log(task_uuid, "[系统] 内存日志")

    data = asyncio.run(registration_routes.get_task_logs(task_uuid))

    assert data["status"] == "running"
    assert data["attempt"] == 2
    assert data["max_attempts"] == 4
    assert data["retrying"] is True
    assert data["last_error"] == "temporary failure"
    assert data["next_retry_in_seconds"] == 4
    assert data["email"] == "runtime@example.com"
    assert data["email_service"] == "freemail"
    assert data["logs"] == ["[系统] 数据库日志", "[系统] 内存日志"]


def test_run_sync_registration_task_retries_until_success(monkeypatch):
    manager = _build_db_manager("registration_runtime_retry.db")
    task_uuid = "runtime-retry-task"

    with manager.session_scope() as session:
        crud.create_registration_task(session, task_uuid=task_uuid)

    @contextmanager
    def fake_get_db():
        session = manager.SessionLocal()
        try:
            yield session
        finally:
            session.close()

    attempts = {"count": 0}

    def fake_execute(**kwargs):
        attempts["count"] += 1
        if attempts["count"] == 1:
            raise RuntimeError("temporary failure")
        return DummyRegistrationResult("retry-success@example.com"), "freemail"

    monkeypatch.setattr(registration_routes, "get_db", fake_get_db)
    monkeypatch.setattr(registration_routes, "get_proxy_for_registration", lambda db: (None, None))
    monkeypatch.setattr(registration_routes, "_execute_single_registration_attempt", fake_execute)
    monkeypatch.setattr(registration_routes, "_perform_registration_uploads", lambda *args, **kwargs: None)
    monkeypatch.setattr(registration_routes, "_wait_for_retry_or_cancel", lambda *args, **kwargs: True)

    registration_routes._run_sync_registration_task(
        task_uuid=task_uuid,
        email_service_type="freemail",
        proxy=None,
        email_service_config=None,
    )

    with manager.session_scope() as session:
        task = crud.get_registration_task(session, task_uuid)
        task_status = task.status
        task_result = task.result

    runtime_status = task_manager_module.task_manager.get_status(task_uuid)
    assert attempts["count"] == 2
    assert task_status == "completed"
    assert task_result["email"] == "retry-success@example.com"
    assert runtime_status["status"] == "completed"
    assert runtime_status["attempt"] == 2
    assert runtime_status["email"] == "retry-success@example.com"


def test_run_sync_registration_task_can_cancel_during_retry_wait(monkeypatch):
    manager = _build_db_manager("registration_runtime_cancel.db")
    task_uuid = "runtime-cancel-task"

    with manager.session_scope() as session:
        crud.create_registration_task(session, task_uuid=task_uuid)

    @contextmanager
    def fake_get_db():
        session = manager.SessionLocal()
        try:
            yield session
        finally:
            session.close()

    attempts = {"count": 0}

    def fake_execute(**kwargs):
        attempts["count"] += 1
        raise RuntimeError("retry me")

    def fake_wait(current_task_uuid, wait_seconds):
        task_manager_module.task_manager.cancel_task(current_task_uuid)
        return False

    monkeypatch.setattr(registration_routes, "get_db", fake_get_db)
    monkeypatch.setattr(registration_routes, "get_proxy_for_registration", lambda db: (None, None))
    monkeypatch.setattr(registration_routes, "_execute_single_registration_attempt", fake_execute)
    monkeypatch.setattr(registration_routes, "_wait_for_retry_or_cancel", fake_wait)

    registration_routes._run_sync_registration_task(
        task_uuid=task_uuid,
        email_service_type="freemail",
        proxy=None,
        email_service_config=None,
    )

    with manager.session_scope() as session:
        task = crud.get_registration_task(session, task_uuid)
        task_status = task.status

    runtime_status = task_manager_module.task_manager.get_status(task_uuid)
    assert attempts["count"] == 1
    assert task_status == "cancelled"
    assert runtime_status["status"] == "cancelled"
    assert runtime_status["last_error"] == "retry me"


def test_task_manager_update_status_broadcasts(monkeypatch):
    captured = {}

    class FakeLoop:
        def is_running(self):
            return True

    async def fake_broadcast(task_uuid, status, **kwargs):
        captured["task_uuid"] = task_uuid
        captured["status"] = status
        captured["kwargs"] = kwargs

    def fake_run_coroutine_threadsafe(coro, loop):
        try:
            coro.send(None)
        except StopIteration:
            pass

        class DummyFuture:
            pass

        return DummyFuture()

    monkeypatch.setattr(task_manager_module.task_manager, "_loop", FakeLoop())
    monkeypatch.setattr(task_manager_module.task_manager, "broadcast_status", fake_broadcast)
    monkeypatch.setattr(task_manager_module.asyncio, "run_coroutine_threadsafe", fake_run_coroutine_threadsafe)

    task_manager_module.task_manager.update_status("broadcast-task", "running", attempt=1, retrying=False)

    assert captured["task_uuid"] == "broadcast-task"
    assert captured["status"] == "running"
    assert captured["kwargs"]["attempt"] == 1
