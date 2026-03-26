import asyncio

import pytest

from src.core.upload import cpa_upload, sub2api_upload, team_manager_upload
from src.core.upload.platform_upload_dedupe import (
    PLATFORM_DUPLICATE_REASON,
    load_platform_upload_record,
    save_platform_upload_record,
)
from src.database import crud
from src.database import session as db_session
from src.database.init_db import initialize_database
from src.database.models import Account, Sub2ApiService
from src.database.session import get_db
from src.web.routes import accounts as accounts_routes


class FakeResponse:
    def __init__(self, status_code=200, payload=None, text=""):
        self.status_code = status_code
        self._payload = payload
        self.text = text

    def json(self):
        if self._payload is None:
            raise ValueError("no json payload")
        return self._payload


@pytest.fixture()
def temp_database(tmp_path, monkeypatch):
    db_url = f"sqlite:///{tmp_path / 'account-upload-dedupe.db'}"
    db_session._db_manager = None
    monkeypatch.setenv("APP_DATABASE_URL", db_url)
    initialize_database(db_url)
    yield
    db_session._db_manager = None


def create_account(email: str, **overrides) -> Account:
    with get_db() as db:
        account = Account(
            email=email,
            password="secret",
            access_token=overrides.get("access_token", "access-token"),
            refresh_token=overrides.get("refresh_token", "refresh-token"),
            id_token=overrides.get("id_token", "id-token"),
            client_id=overrides.get("client_id", "client-id"),
            account_id=overrides.get("account_id", "account-id"),
            workspace_id=overrides.get("workspace_id", "workspace-id"),
            email_service=overrides.get("email_service", "moe_mail"),
            subscription_type=overrides.get("subscription_type"),
            status="active",
        )
        db.add(account)
        db.commit()
        db.refresh(account)
        return account


def create_sub2api_service(**overrides) -> Sub2ApiService:
    with get_db() as db:
        service = Sub2ApiService(
            name=overrides.get("name", "Sub2API"),
            api_url=overrides.get("api_url", "https://sub2api.example.test"),
            api_key=overrides.get("api_key", "api-key"),
            template_config=overrides.get("template_config"),
            next_name_index=overrides.get("next_name_index", 1),
            enabled=overrides.get("enabled", True),
            priority=overrides.get("priority", 0),
        )
        db.add(service)
        db.commit()
        db.refresh(service)
        return service


def create_cpa_service(**overrides):
    with get_db() as db:
        return crud.create_cpa_service(
            db,
            name=overrides.get("name", "CPA"),
            api_url=overrides.get("api_url", "https://cpa.example.test"),
            api_token=overrides.get("api_token", "token"),
            enabled=overrides.get("enabled", True),
            priority=overrides.get("priority", 0),
        )


def create_tm_service(**overrides):
    with get_db() as db:
        return crud.create_tm_service(
            db,
            name=overrides.get("name", "TM"),
            api_url=overrides.get("api_url", "https://tm.example.test"),
            api_key=overrides.get("api_key", "tm-key"),
            enabled=overrides.get("enabled", True),
            priority=overrides.get("priority", 0),
        )


def test_batch_upload_to_cpa_skips_remote_duplicate(temp_database, monkeypatch):
    account = create_account("duplicate@example.com")
    post_calls = []

    monkeypatch.setattr(
        cpa_upload.cffi_requests,
        "get",
        lambda url, **kwargs: FakeResponse(
            status_code=200,
            payload={"files": [{"name": "duplicate@example.com.json"}]},
        ),
    )
    monkeypatch.setattr(
        cpa_upload.cffi_requests,
        "post",
        lambda *args, **kwargs: post_calls.append((args, kwargs)) or FakeResponse(status_code=201),
    )

    results = cpa_upload.batch_upload_to_cpa(
        [account.id],
        api_url="https://cpa.example.test",
        api_token="token",
        dedupe=True,
    )

    assert results["success_count"] == 0
    assert results["failed_count"] == 0
    assert results["skipped_count"] == 1
    assert post_calls == []
    detail = results["details"][0]
    assert detail["reason_code"] == PLATFORM_DUPLICATE_REASON
    assert detail["duplicate_source"] == "remote"


def test_batch_upload_to_cpa_falls_back_to_local_record(temp_database, monkeypatch):
    account = create_account("local@example.com")
    service = create_cpa_service()

    with get_db() as db:
        saved = crud.get_account_by_id(db, account.id)
        save_platform_upload_record(
            db,
            saved,
            "cpa",
            service_id=service.id,
            api_url=service.api_url,
            url_normalizer=cpa_upload._normalize_cpa_auth_files_url,
            metadata={"filename": "local@example.com.json"},
        )

    monkeypatch.setattr(
        cpa_upload.cffi_requests,
        "get",
        lambda *args, **kwargs: (_ for _ in ()).throw(RuntimeError("auth-files unavailable")),
    )
    monkeypatch.setattr(
        cpa_upload.cffi_requests,
        "post",
        lambda *args, **kwargs: pytest.fail("CPA duplicate should skip upload"),
    )

    results = cpa_upload.batch_upload_to_cpa(
        [account.id],
        api_url=service.api_url,
        api_token=service.api_token,
        service_id=service.id,
        dedupe=True,
    )

    assert results["skipped_count"] == 1
    detail = results["details"][0]
    assert detail["reason_code"] == PLATFORM_DUPLICATE_REASON
    assert detail["duplicate_source"] == "local_record"


def test_batch_upload_to_cpa_records_local_upload_success(temp_database, monkeypatch):
    account = create_account("fresh@example.com")

    monkeypatch.setattr(
        cpa_upload.cffi_requests,
        "get",
        lambda url, **kwargs: FakeResponse(status_code=200, payload={"files": []}),
    )
    monkeypatch.setattr(
        cpa_upload.cffi_requests,
        "post",
        lambda url, **kwargs: FakeResponse(status_code=201),
    )

    results = cpa_upload.batch_upload_to_cpa(
        [account.id],
        api_url="https://cpa.example.test",
        api_token="token",
        dedupe=True,
    )

    assert results["success_count"] == 1
    with get_db() as db:
        saved = crud.get_account_by_id(db, account.id)
        assert saved.cpa_uploaded is True
        record = load_platform_upload_record(
            saved,
            "cpa",
            api_url="https://cpa.example.test",
            url_normalizer=cpa_upload._normalize_cpa_auth_files_url,
        )
        assert record["filename"] == "fresh@example.com.json"


def test_batch_upload_to_sub2api_skips_remote_duplicate_without_advancing_counter(temp_database, monkeypatch):
    account = create_account("dup-sub2api@example.com", account_id="acct-dup", workspace_id="org-dup")
    service = create_sub2api_service(next_name_index=5)

    monkeypatch.setattr(
        sub2api_upload,
        "search_sub2api_accounts",
        lambda *args, **kwargs: [{"credentials": {"chatgpt_account_id": "acct-dup"}}],
    )
    monkeypatch.setattr(
        sub2api_upload.cffi_requests,
        "post",
        lambda *args, **kwargs: pytest.fail("Sub2API duplicate should skip upload"),
    )

    results = sub2api_upload.batch_upload_to_sub2api(
        [account.id],
        service.api_url,
        service.api_key,
        service_id=service.id,
        dedupe=True,
    )

    assert results["success_count"] == 0
    assert results["failed_count"] == 0
    assert results["skipped_count"] == 1
    detail = results["details"][0]
    assert detail["reason_code"] == PLATFORM_DUPLICATE_REASON
    assert detail["duplicate_source"] == "remote"
    with get_db() as db:
        saved_service = crud.get_sub2api_service_by_id(db, service.id)
        assert saved_service.next_name_index == 5


def test_batch_upload_to_sub2api_falls_back_to_local_record_on_remote_error(temp_database, monkeypatch):
    account = create_account("local-sub2api@example.com")
    service = create_sub2api_service(next_name_index=4)

    with get_db() as db:
        saved = crud.get_account_by_id(db, account.id)
        save_platform_upload_record(
            db,
            saved,
            "sub2api",
            service_id=service.id,
            api_url=service.api_url,
            metadata={"generated_names": ["GPT-Free-000000004"]},
        )

    monkeypatch.setattr(
        sub2api_upload,
        "search_sub2api_accounts",
        lambda *args, **kwargs: (_ for _ in ()).throw(RuntimeError("remote search failed")),
    )
    monkeypatch.setattr(
        sub2api_upload.cffi_requests,
        "post",
        lambda *args, **kwargs: pytest.fail("Sub2API duplicate should skip upload"),
    )

    results = sub2api_upload.batch_upload_to_sub2api(
        [account.id],
        service.api_url,
        service.api_key,
        service_id=service.id,
        dedupe=True,
    )

    assert results["skipped_count"] == 1
    detail = results["details"][0]
    assert detail["reason_code"] == PLATFORM_DUPLICATE_REASON
    assert detail["duplicate_source"] == "local_record"


def test_upload_account_to_sub2api_returns_skipped_payload_for_duplicate(temp_database, monkeypatch):
    account = create_account("route-sub2api@example.com", account_id="acct-route")
    service = create_sub2api_service()

    monkeypatch.setattr(
        sub2api_upload,
        "search_sub2api_accounts",
        lambda *args, **kwargs: [{"credentials": {"chatgpt_account_id": "acct-route"}}],
    )

    response = asyncio.run(
        accounts_routes.upload_account_to_sub2api(
            account.id,
            accounts_routes.Sub2ApiUploadRequest(service_id=service.id),
        )
    )

    assert response["success"] is True
    assert response["skipped"] is True
    assert response["reason_code"] == PLATFORM_DUPLICATE_REASON
    assert response["duplicate_source"] == "remote"


def test_batch_upload_to_team_manager_uses_local_record_dedupe(temp_database, monkeypatch):
    account = create_account("tm-local@example.com")
    service = create_tm_service()

    with get_db() as db:
        saved = crud.get_account_by_id(db, account.id)
        save_platform_upload_record(
            db,
            saved,
            "tm",
            service_id=service.id,
            api_url=service.api_url,
        )

    monkeypatch.setattr(
        team_manager_upload.cffi_requests,
        "post",
        lambda *args, **kwargs: pytest.fail("Team Manager duplicate should skip upload"),
    )

    results = team_manager_upload.batch_upload_to_team_manager(
        [account.id],
        service.api_url,
        service.api_key,
        service_id=service.id,
        dedupe=True,
    )

    assert results["success_count"] == 0
    assert results["failed_count"] == 0
    assert results["skipped_count"] == 1
    detail = results["details"][0]
    assert detail["reason_code"] == PLATFORM_DUPLICATE_REASON
    assert detail["duplicate_source"] == "local_record"


def test_upload_account_to_tm_records_local_upload_success(temp_database, monkeypatch):
    account = create_account("tm-upload@example.com")
    service = create_tm_service()

    monkeypatch.setattr(
        team_manager_upload.cffi_requests,
        "post",
        lambda *args, **kwargs: FakeResponse(status_code=201),
    )

    response = asyncio.run(
        accounts_routes.upload_account_to_tm(
            account.id,
            accounts_routes.UploadTMRequest(service_id=service.id),
        )
    )

    assert response["success"] is True
    with get_db() as db:
        saved = crud.get_account_by_id(db, account.id)
        record = load_platform_upload_record(
            saved,
            "tm",
            service_id=service.id,
            api_url=service.api_url,
        )
        assert record is not None
