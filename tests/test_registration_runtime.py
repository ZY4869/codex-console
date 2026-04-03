import asyncio
from contextlib import contextmanager
from datetime import timedelta
from pathlib import Path
from types import SimpleNamespace
from uuid import uuid4

import pytest

from src.database import crud
from src.database.models import Base
from src.database.session import DatabaseSessionManager
from src.core.timezone_utils import utcnow_naive
from src.web import task_manager as task_manager_module
from src.web.routes import registration as registration_routes
from src.web.routes.registration_selection import RegistrationSelectionExhaustedError


class DummyRegistrationResult:
    def __init__(self, email: str, success: bool = True, error_code: str = "", error_message: str = ""):
        self.success = success
        self.email = email
        self.error_code = error_code
        self.error_message = error_message

    def to_dict(self):
        return {
            "success": self.success,
            "email": self.email,
            "error_code": self.error_code,
            "error_message": self.error_message,
        }


def _build_db_manager(name: str) -> DatabaseSessionManager:
    runtime_dir = Path("tests_runtime")
    runtime_dir.mkdir(exist_ok=True)
    db_path = runtime_dir / name
    if db_path.exists():
        try:
            db_path.unlink()
        except PermissionError:
            db_path = runtime_dir / f"{db_path.stem}-{uuid4().hex}{db_path.suffix}"

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


def test_run_sync_registration_task_skips_add_phone_without_wait(monkeypatch):
    manager = _build_db_manager("registration_runtime_add_phone_skip.db")
    task_uuid = "runtime-add-phone-skip"
    add_phone_email = "first@example.com"
    success_email = "second@example.com"

    with manager.session_scope() as session:
        crud.create_registration_task(session, task_uuid=task_uuid)

    @contextmanager
    def fake_get_db():
        session = manager.SessionLocal()
        try:
            yield session
        finally:
            session.close()

    attempts = {"execute": 0, "wait": 0}
    skipped_snapshots = []

    def fake_execute(**kwargs):
        attempts["execute"] += 1
        skipped_snapshots.append(list(kwargs.get("skipped_email_addresses") or []))
        db = kwargs["db"]
        if attempts["execute"] == 1:
            raise registration_routes.RegistrationAttemptError(
                DummyRegistrationResult(
                    add_phone_email,
                    success=False,
                    error_code="add_phone_required",
                    error_message="当前账号进入 add_phone 页面，需要补充手机号后才能继续授权",
                ),
                "freemail",
            )

        assert skipped_snapshots[-1] == [add_phone_email]
        if not crud.get_account_by_email(db, success_email):
            crud.create_account(db, email=success_email, email_service="freemail")
        return DummyRegistrationResult(success_email), "freemail"

    def fake_wait(*args, **kwargs):
        attempts["wait"] += 1
        return True

    monkeypatch.setattr(registration_routes, "get_db", fake_get_db)
    monkeypatch.setattr(registration_routes, "get_proxy_for_registration", lambda db: (None, None))
    monkeypatch.setattr(registration_routes, "_get_max_registration_attempts", lambda: 2)
    monkeypatch.setattr(registration_routes, "_execute_single_registration_attempt", fake_execute)
    monkeypatch.setattr(registration_routes, "_perform_registration_uploads", lambda *args, **kwargs: None)
    monkeypatch.setattr(registration_routes, "_wait_for_retry_or_cancel", fake_wait)

    registration_routes._run_sync_registration_task(
        task_uuid=task_uuid,
        email_service_type="freemail",
        proxy=None,
        email_service_config=None,
        selected_email_addresses=[add_phone_email, success_email],
    )

    with manager.session_scope() as session:
        task = crud.get_registration_task(session, task_uuid)
        add_phone_stat = crud.get_email_registration_stat_by_email(session, add_phone_email)
        success_stat = crud.get_email_registration_stat_by_email(session, success_email)
        task_status = task.status
        add_phone_total_attempts = add_phone_stat.total_attempts if add_phone_stat else None
        add_phone_count = add_phone_stat.add_phone_count if add_phone_stat else None
        add_phone_failure_count = add_phone_stat.failure_count if add_phone_stat else None
        success_total_attempts = success_stat.total_attempts if success_stat else None
        success_count = success_stat.success_count if success_stat else None

    runtime_status = task_manager_module.task_manager.get_status(task_uuid)
    assert attempts["execute"] == 2
    assert attempts["wait"] == 0
    assert skipped_snapshots[0] == []
    assert skipped_snapshots[1] == [add_phone_email]
    assert task_status == "completed"
    assert runtime_status["status"] == "completed"
    assert add_phone_stat is not None
    assert add_phone_total_attempts == 1
    assert add_phone_count == 1
    assert add_phone_failure_count == 0
    assert success_stat is not None
    assert success_total_attempts == 1
    assert success_count == 1


def test_run_sync_registration_task_fails_when_addresses_exhausted_after_add_phone(monkeypatch):
    manager = _build_db_manager("registration_runtime_add_phone_exhausted.db")
    task_uuid = "runtime-add-phone-exhausted"
    exhausted_email = "only@example.com"

    with manager.session_scope() as session:
        crud.create_registration_task(session, task_uuid=task_uuid)

    @contextmanager
    def fake_get_db():
        session = manager.SessionLocal()
        try:
            yield session
        finally:
            session.close()

    attempts = {"execute": 0, "wait": 0}

    def fake_execute(**kwargs):
        attempts["execute"] += 1
        skipped = list(kwargs.get("skipped_email_addresses") or [])
        if attempts["execute"] == 1:
            assert skipped == []
            raise registration_routes.RegistrationAttemptError(
                DummyRegistrationResult(
                    exhausted_email,
                    success=False,
                    error_code="add_phone_required",
                    error_message="当前账号进入 add_phone 页面，需要补充手机号后才能继续授权",
                ),
                "freemail",
            )

        assert skipped == [exhausted_email]
        raise RegistrationSelectionExhaustedError("当前可选邮箱地址已耗尽")

    def fake_wait(*args, **kwargs):
        attempts["wait"] += 1
        return True

    monkeypatch.setattr(registration_routes, "get_db", fake_get_db)
    monkeypatch.setattr(registration_routes, "get_proxy_for_registration", lambda db: (None, None))
    monkeypatch.setattr(registration_routes, "_get_max_registration_attempts", lambda: 3)
    monkeypatch.setattr(registration_routes, "_execute_single_registration_attempt", fake_execute)
    monkeypatch.setattr(registration_routes, "_wait_for_retry_or_cancel", fake_wait)

    registration_routes._run_sync_registration_task(
        task_uuid=task_uuid,
        email_service_type="freemail",
        proxy=None,
        email_service_config=None,
        selected_email_addresses=[exhausted_email],
    )

    with manager.session_scope() as session:
        task = crud.get_registration_task(session, task_uuid)
        stat = crud.get_email_registration_stat_by_email(session, exhausted_email)
        task_status = task.status
        task_error = task.error_message
        stat_total_attempts = stat.total_attempts if stat else None
        stat_add_phone_count = stat.add_phone_count if stat else None
        stat_failure_count = stat.failure_count if stat else None

    runtime_status = task_manager_module.task_manager.get_status(task_uuid)
    assert attempts["execute"] == 2
    assert attempts["wait"] == 0
    assert task_status == "failed"
    assert "当前可选邮箱地址已耗尽" in (task_error or "")
    assert runtime_status["status"] == "failed"
    assert "当前可选邮箱地址已耗尽" in (runtime_status["last_error"] or "")
    assert stat is not None
    assert stat_total_attempts == 1
    assert stat_add_phone_count == 1
    assert stat_failure_count == 0


def test_run_sync_registration_task_reuses_saved_account_when_upload_retry_succeeds(monkeypatch):
    manager = _build_db_manager("registration_runtime_upload_reuse_success.db")
    task_uuid = "runtime-upload-reuse-success"
    account_email = "upload-reuse@example.com"

    with manager.session_scope() as session:
        crud.create_registration_task(session, task_uuid=task_uuid)

    @contextmanager
    def fake_get_db():
        session = manager.SessionLocal()
        try:
            yield session
        finally:
            session.close()

    attempts = {"execute": 0, "upload": 0}

    def fake_execute(**kwargs):
        attempts["execute"] += 1
        db = kwargs["db"]
        account = crud.get_account_by_email(db, account_email)
        if not account:
            account = crud.create_account(
                db,
                email=account_email,
                email_service="freemail",
            )
        return DummyRegistrationResult(account_email), "freemail"

    def fake_upload(db, account_email: str, account_id: int, log_callback, **kwargs):
        attempts["upload"] += 1
        account = crud.get_account_by_email(db, account_email)
        assert account is not None
        assert account_id == account.id
        if attempts["upload"] == 1:
            return {
                "success": False,
                "error_summary": "CPA(primary): timeout",
                "failed_platforms": ["cpa"],
                "platforms": {
                    "cpa": {
                        "enabled": True,
                        "success_count": 0,
                        "failed_count": 1,
                        "skipped_count": 0,
                        "services": [],
                    }
                },
            }
        return {
            "success": True,
            "error_summary": "",
            "failed_platforms": [],
            "platforms": {
                "cpa": {
                    "enabled": True,
                    "success_count": 1,
                    "failed_count": 0,
                    "skipped_count": 0,
                    "services": [],
                }
            },
        }

    monkeypatch.setattr(registration_routes, "get_db", fake_get_db)
    monkeypatch.setattr(registration_routes, "get_proxy_for_registration", lambda db: (None, None))
    monkeypatch.setattr(registration_routes, "_get_max_registration_attempts", lambda: 2)
    monkeypatch.setattr(registration_routes, "_execute_single_registration_attempt", fake_execute)
    monkeypatch.setattr(registration_routes, "_perform_registration_uploads", fake_upload)
    monkeypatch.setattr(registration_routes, "_wait_for_retry_or_cancel", lambda *args, **kwargs: True)

    registration_routes._run_sync_registration_task(
        task_uuid=task_uuid,
        email_service_type="freemail",
        proxy=None,
        email_service_config=None,
        auto_upload_cpa=True,
    )

    with manager.session_scope() as session:
        task = crud.get_registration_task(session, task_uuid)
        account = crud.get_account_by_email(session, account_email)
        account_count = crud.get_accounts_count(session)
        task_status = task.status
        task_result = task.result
        saved_account_id = account.id if account else None

    runtime_status = task_manager_module.task_manager.get_status(task_uuid)
    assert attempts["execute"] == 1
    assert attempts["upload"] == 2
    assert account is not None
    assert account_count == 1
    assert task_status == "completed"
    assert task_result["email"] == account_email
    assert task_result["saved_account_email"] == account_email
    assert task_result["saved_account_id"] == saved_account_id
    assert task_result["upload_retry_mode"] == registration_routes.UPLOAD_RETRY_MODE_REUSE_SAVED_ACCOUNT
    assert task_result["upload_summary"]["success"] is True
    assert runtime_status["status"] == "completed"
    assert runtime_status["attempt"] == 2
    assert runtime_status["email"] == account_email


def test_run_sync_registration_task_preserves_saved_account_when_upload_retry_exhausted(monkeypatch):
    manager = _build_db_manager("registration_runtime_upload_reuse_failed.db")
    task_uuid = "runtime-upload-reuse-failed"
    account_email = "upload-reuse-failed@example.com"

    with manager.session_scope() as session:
        crud.create_registration_task(session, task_uuid=task_uuid)

    @contextmanager
    def fake_get_db():
        session = manager.SessionLocal()
        try:
            yield session
        finally:
            session.close()

    attempts = {"execute": 0, "upload": 0}

    def fake_execute(**kwargs):
        attempts["execute"] += 1
        db = kwargs["db"]
        account = crud.get_account_by_email(db, account_email)
        if not account:
            crud.create_account(
                db,
                email=account_email,
                email_service="freemail",
            )
        return DummyRegistrationResult(account_email), "freemail"

    def fake_upload(db, account_email: str, account_id: int, log_callback, **kwargs):
        attempts["upload"] += 1
        account = crud.get_account_by_email(db, account_email)
        assert account is not None
        assert account_id == account.id
        return {
            "success": False,
            "error_summary": "TM(primary): upstream 500",
            "failed_platforms": ["tm"],
            "platforms": {
                "tm": {
                    "enabled": True,
                    "success_count": 0,
                    "failed_count": 1,
                    "skipped_count": 0,
                    "services": [],
                }
            },
        }

    monkeypatch.setattr(registration_routes, "get_db", fake_get_db)
    monkeypatch.setattr(registration_routes, "get_proxy_for_registration", lambda db: (None, None))
    monkeypatch.setattr(registration_routes, "_get_max_registration_attempts", lambda: 2)
    monkeypatch.setattr(registration_routes, "_execute_single_registration_attempt", fake_execute)
    monkeypatch.setattr(registration_routes, "_perform_registration_uploads", fake_upload)
    monkeypatch.setattr(registration_routes, "_wait_for_retry_or_cancel", lambda *args, **kwargs: True)

    registration_routes._run_sync_registration_task(
        task_uuid=task_uuid,
        email_service_type="freemail",
        proxy=None,
        email_service_config=None,
        auto_upload_tm=True,
    )

    with manager.session_scope() as session:
        task = crud.get_registration_task(session, task_uuid)
        account = crud.get_account_by_email(session, account_email)
        account_count = crud.get_accounts_count(session)
        task_status = task.status
        task_error = task.error_message
        task_result = task.result
        saved_account_id = account.id if account else None

    runtime_status = task_manager_module.task_manager.get_status(task_uuid)
    assert attempts["execute"] == 1
    assert attempts["upload"] == 2
    assert account is not None
    assert account_count == 1
    assert task_status == "failed"
    assert "账号已注册，自动上传失败且重试已耗尽" in task_error
    assert task_result["saved_account_email"] == account_email
    assert task_result["saved_account_id"] == saved_account_id
    assert task_result["upload_summary"]["failed_platforms"] == ["tm"]
    assert runtime_status["status"] == "failed"
    assert "账号已注册，自动上传失败且重试已耗尽" in runtime_status["last_error"]


def test_email_registration_stats_route_returns_pagination_and_latest_first(monkeypatch):
    manager = _build_db_manager("registration_runtime_email_stats_route.db")

    with manager.session_scope() as session:
        crud.record_email_registration_attempt(
            session,
            email_address="older@example.com",
            email_service="freemail",
            status="failed",
            error_message="older failure",
            occurred_at=utcnow_naive() - timedelta(minutes=10),
        )
        crud.record_email_registration_attempt(
            session,
            email_address="newer@example.com",
            email_service="freemail",
            status="add_phone",
            error_message="need phone",
            occurred_at=utcnow_naive(),
        )

    @contextmanager
    def fake_get_db():
        session = manager.SessionLocal()
        try:
            yield session
        finally:
            session.close()

    monkeypatch.setattr(registration_routes, "get_db", fake_get_db)

    first_page = asyncio.run(registration_routes.get_email_registration_stats(limit=1, offset=0))
    second_page = asyncio.run(registration_routes.get_email_registration_stats(limit=1, offset=1))

    assert first_page.total == 2
    assert len(first_page.items) == 1
    assert first_page.items[0].email == "newer@example.com"
    assert first_page.items[0].add_phone_count == 1
    assert first_page.items[0].failure_count == 0
    assert second_page.items[0].email == "older@example.com"


def test_email_domain_registration_stats_route_aggregates_by_domain(monkeypatch):
    manager = _build_db_manager("registration_runtime_email_domain_stats_route.db")
    now = utcnow_naive()

    with manager.session_scope() as session:
        crud.record_email_registration_attempt(
            session,
            email_address="success@shared.example.com",
            email_service="freemail",
            status="success",
            occurred_at=now - timedelta(minutes=5),
        )
        crud.record_email_registration_attempt(
            session,
            email_address="failed@shared.example.com",
            email_service="freemail",
            status="failed",
            error_message="domain failure",
            occurred_at=now - timedelta(minutes=1),
        )
        crud.record_email_registration_attempt(
            session,
            email_address="phone@shared.example.com",
            email_service="freemail",
            status="add_phone",
            error_message="need phone",
            occurred_at=now,
        )
        crud.record_email_registration_attempt(
            session,
            email_address="older@other.example.com",
            email_service="freemail",
            status="failed",
            error_message="older domain failure",
            occurred_at=now - timedelta(minutes=10),
        )

    @contextmanager
    def fake_get_db():
        session = manager.SessionLocal()
        try:
            yield session
        finally:
            session.close()

    monkeypatch.setattr(registration_routes, "get_db", fake_get_db)

    first_page = asyncio.run(registration_routes.get_email_domain_registration_stats(limit=1, offset=0))
    second_page = asyncio.run(registration_routes.get_email_domain_registration_stats(limit=1, offset=1))

    assert first_page.total == 2
    assert len(first_page.items) == 1
    assert first_page.items[0].email_domain == "shared.example.com"
    assert first_page.items[0].total_attempts == 3
    assert first_page.items[0].success_count == 1
    assert first_page.items[0].failure_count == 1
    assert first_page.items[0].add_phone_count == 1
    assert first_page.summary.total_domains == 2
    assert first_page.summary.total_attempts == 4
    assert first_page.summary.success_count == 1
    assert first_page.summary.failure_count == 2
    assert first_page.summary.add_phone_count == 1
    assert first_page.summary.success_rate == 25.0
    assert first_page.summary.top_domain == "shared.example.com"
    assert first_page.summary.top_domain_attempts == 3
    assert second_page.items[0].email_domain == "other.example.com"
    assert second_page.items[0].total_attempts == 1
    assert second_page.items[0].failure_count == 1


def test_start_registration_rejects_subdomain_only_for_unsupported_service():
    request = registration_routes.RegistrationTaskCreate(
        email_service_type="outlook",
        subdomain_only=True,
    )

    with pytest.raises(registration_routes.HTTPException) as exc_info:
        asyncio.run(registration_routes._start_single_registration_internal(request))

    assert exc_info.value.status_code == 400
    assert "仅子域名" in str(exc_info.value.detail)


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


def test_execute_single_registration_attempt_uses_legacy_protocol_engine(monkeypatch):
    manager = _build_db_manager("registration_flow_legacy.db")
    task_uuid = "registration-flow-legacy"
    captured = {}

    class FakeLegacyEngine:
        def __init__(self, **kwargs):
            captured["engine"] = "legacy"
            captured["kwargs"] = kwargs

        def run(self):
            return DummyRegistrationResult("legacy@example.com")

        def save_to_database(self, result):
            return True

    with manager.session_scope() as session:
        crud.create_registration_task(session, task_uuid=task_uuid)

        monkeypatch.setattr(
            registration_routes,
            "resolve_email_service_for_registration",
            lambda **kwargs: SimpleNamespace(
                email_service_id=None,
                service_type=registration_routes.EmailServiceType.FREEMAIL,
                config={},
                service_name="fake-freemail",
            ),
        )
        monkeypatch.setattr(
            registration_routes.EmailServiceFactory,
            "create",
            lambda *args, **kwargs: SimpleNamespace(service_type=registration_routes.EmailServiceType.FREEMAIL),
        )
        monkeypatch.setattr(registration_routes, "RegistrationEngine", FakeLegacyEngine)
        monkeypatch.setattr(registration_routes, "EnhancedProtocolRegistrationEngine", lambda **kwargs: None)
        monkeypatch.setattr(registration_routes, "update_proxy_usage", lambda *args, **kwargs: None)

        result, service_type = registration_routes._execute_single_registration_attempt(
            db=session,
            task_uuid=task_uuid,
            email_service_type="freemail",
            email_service_id=None,
            email_service_config=None,
            actual_proxy_url=None,
            proxy_id=None,
            selection_index=0,
            random_email_service=False,
            random_outlook_account=False,
            random_domain=False,
            subdomain_only=False,
            selected_email_addresses=[],
            selected_domains=[],
            skipped_email_addresses=[],
            log_callback=lambda *_args, **_kwargs: None,
            registration_mode="protocol",
            registration_type="child",
        )

    assert captured["engine"] == "legacy"
    assert result.email == "legacy@example.com"
    assert service_type == "freemail"


def test_execute_single_registration_attempt_uses_enhanced_protocol_engine(monkeypatch):
    manager = _build_db_manager("registration_flow_enhanced.db")
    task_uuid = "registration-flow-enhanced"
    captured = {}

    class FakeEnhancedEngine:
        def __init__(self, **kwargs):
            captured["engine"] = "enhanced"
            captured["kwargs"] = kwargs

        def run(self):
            return DummyRegistrationResult("enhanced@example.com")

        def save_to_database(self, result):
            return True

    with manager.session_scope() as session:
        crud.create_registration_task(session, task_uuid=task_uuid)

        monkeypatch.setattr(
            registration_routes,
            "resolve_email_service_for_registration",
            lambda **kwargs: SimpleNamespace(
                email_service_id=None,
                service_type=registration_routes.EmailServiceType.FREEMAIL,
                config={},
                service_name="fake-freemail",
            ),
        )
        monkeypatch.setattr(
            registration_routes.EmailServiceFactory,
            "create",
            lambda *args, **kwargs: SimpleNamespace(service_type=registration_routes.EmailServiceType.FREEMAIL),
        )
        monkeypatch.setattr(registration_routes, "EnhancedProtocolRegistrationEngine", FakeEnhancedEngine)
        monkeypatch.setattr(registration_routes, "update_proxy_usage", lambda *args, **kwargs: None)

        result, service_type = registration_routes._execute_single_registration_attempt(
            db=session,
            task_uuid=task_uuid,
            email_service_type="freemail",
            email_service_id=None,
            email_service_config=None,
            actual_proxy_url=None,
            proxy_id=None,
            selection_index=0,
            random_email_service=False,
            random_outlook_account=False,
            random_domain=False,
            subdomain_only=False,
            selected_email_addresses=[],
            selected_domains=[],
            skipped_email_addresses=[],
            log_callback=lambda *_args, **_kwargs: None,
            registration_mode="protocol",
            protocol_variant="enhanced",
            registration_type="child",
        )

    assert captured["engine"] == "enhanced"
    assert result.email == "enhanced@example.com"
    assert service_type == "freemail"


def test_execute_single_registration_attempt_uses_adaptive_protocol_engine(monkeypatch):
    manager = _build_db_manager("registration_flow_adaptive.db")
    task_uuid = "registration-flow-adaptive"
    captured = {}

    class FakeAdaptiveEngine:
        def __init__(self, **kwargs):
            captured["engine"] = "adaptive"
            captured["kwargs"] = kwargs

        def run(self):
            return DummyRegistrationResult("adaptive@example.com")

        def save_to_database(self, result):
            return True

    with manager.session_scope() as session:
        crud.create_registration_task(session, task_uuid=task_uuid)

        monkeypatch.setattr(
            registration_routes,
            "resolve_email_service_for_registration",
            lambda **kwargs: SimpleNamespace(
                email_service_id=None,
                service_type=registration_routes.EmailServiceType.FREEMAIL,
                config={},
                service_name="fake-freemail",
            ),
        )
        monkeypatch.setattr(
            registration_routes.EmailServiceFactory,
            "create",
            lambda *args, **kwargs: SimpleNamespace(service_type=registration_routes.EmailServiceType.FREEMAIL),
        )
        monkeypatch.setattr(registration_routes, "AdaptiveProtocolRegistrationEngine", FakeAdaptiveEngine)
        monkeypatch.setattr(registration_routes, "update_proxy_usage", lambda *args, **kwargs: None)

        result, service_type = registration_routes._execute_single_registration_attempt(
            db=session,
            task_uuid=task_uuid,
            email_service_type="freemail",
            email_service_id=None,
            email_service_config=None,
            actual_proxy_url=None,
            proxy_id=None,
            selection_index=0,
            random_email_service=False,
            random_outlook_account=False,
            random_domain=False,
            subdomain_only=False,
            selected_email_addresses=[],
            selected_domains=[],
            skipped_email_addresses=[],
            log_callback=lambda *_args, **_kwargs: None,
            registration_mode="protocol",
            protocol_variant="adaptive",
            registration_type="child",
        )

    assert captured["engine"] == "adaptive"
    assert result.email == "adaptive@example.com"
    assert service_type == "freemail"


def test_execute_single_registration_attempt_uses_browser_engine(monkeypatch):
    import src.core.browser_register as browser_register_module

    manager = _build_db_manager("registration_flow_browser.db")
    task_uuid = "registration-flow-browser"
    captured = {}

    class FakeBrowserEngine:
        def __init__(self, **kwargs):
            captured["engine"] = "browser"
            captured["kwargs"] = kwargs

        def run(self):
            return DummyRegistrationResult("browser@example.com")

        def save_to_database(self, result):
            return True

    with manager.session_scope() as session:
        crud.create_registration_task(session, task_uuid=task_uuid)

        monkeypatch.setattr(
            registration_routes,
            "resolve_email_service_for_registration",
            lambda **kwargs: SimpleNamespace(
                email_service_id=None,
                service_type=registration_routes.EmailServiceType.FREEMAIL,
                config={},
                service_name="fake-freemail",
            ),
        )
        monkeypatch.setattr(
            registration_routes.EmailServiceFactory,
            "create",
            lambda *args, **kwargs: SimpleNamespace(service_type=registration_routes.EmailServiceType.FREEMAIL),
        )
        monkeypatch.setattr(browser_register_module, "BrowserRegistrationEngine", FakeBrowserEngine)
        monkeypatch.setattr(registration_routes, "update_proxy_usage", lambda *args, **kwargs: None)

        result, service_type = registration_routes._execute_single_registration_attempt(
            db=session,
            task_uuid=task_uuid,
            email_service_type="freemail",
            email_service_id=None,
            email_service_config=None,
            actual_proxy_url=None,
            proxy_id=None,
            selection_index=0,
            random_email_service=False,
            random_outlook_account=False,
            random_domain=False,
            subdomain_only=False,
            selected_email_addresses=[],
            selected_domains=[],
            skipped_email_addresses=[],
            log_callback=lambda *_args, **_kwargs: None,
            registration_mode="browser",
            protocol_variant="enhanced",
            registration_type="child",
        )

    assert captured["engine"] == "browser"
    assert result.email == "browser@example.com"
    assert service_type == "freemail"


def test_dispatch_registration_config_defaults_protocol_variant_to_legacy(monkeypatch):
    captured = {}

    async def fake_start_single(request, background_tasks=None):
        captured["registration_mode"] = request.registration_mode
        captured["protocol_variant"] = request.protocol_variant
        return SimpleNamespace(task_uuid="task-uuid", status="pending")

    monkeypatch.setattr(registration_routes, "_validate_registration_request", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(registration_routes, "_start_single_registration_internal", fake_start_single)

    result = asyncio.run(
        registration_routes.dispatch_registration_config(
            {
                "email_service_type": "freemail",
            }
        )
    )

    assert result["kind"] == "single"
    assert captured["registration_mode"] == "protocol"
    assert captured["protocol_variant"] == "legacy"
