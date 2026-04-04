import asyncio
import json

import pytest

from src.database import crud
from src.database.init_db import initialize_database
from src.database.models import Account, Sub2ApiService
from src.database.session import get_db
from src.database import session as db_session
from src.core.upload import sub2api_groups
from src.core.upload import sub2api_naming
from src.core.upload import sub2api_upload
from src.core.upload import sub2api_payload
from src.core.upload import team_upload_guard
from src.core.upload.sub2api_payload import normalize_sub2api_template_config
from src.web.routes import accounts as accounts_routes
from src.web.routes.upload import sub2api_services as sub2api_service_routes


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
    db_url = f"sqlite:///{tmp_path / 'sub2api-tests.db'}"
    db_session._db_manager = None
    monkeypatch.setenv("APP_DATABASE_URL", db_url)
    initialize_database(db_url)
    yield
    db_session._db_manager = None


def create_account(email: str, remark: str = "", **overrides) -> Account:
    with get_db() as db:
        account = Account(
            email=email,
            password="secret",
            remark=remark,
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
            name=overrides.get("name", "Sub2API A"),
            api_url=overrides.get("api_url", "https://sub2api.example.test"),
            api_key=overrides.get("api_key", "api-key"),
            template_config=overrides.get("template_config"),
            next_name_index=overrides.get("next_name_index"),
            enabled=overrides.get("enabled", True),
            priority=overrides.get("priority", 0),
        )
        db.add(service)
        db.commit()
        db.refresh(service)
        return service


async def read_streaming_response(response) -> str:
    chunks = []
    async for chunk in response.body_iterator:
        if isinstance(chunk, bytes):
            chunks.append(chunk)
        else:
            chunks.append(chunk.encode("utf-8"))
    return b"".join(chunks).decode("utf-8")


def test_update_account_route_persists_remark(temp_database):
    account = create_account("alpha@example.com")

    response = asyncio.run(
        accounts_routes.update_account(
            account.id,
            accounts_routes.AccountUpdateRequest(remark="主号备注"),
        )
    )

    assert response.remark == "主号备注"
    with get_db() as db:
        saved = crud.get_account_by_id(db, account.id)
        assert saved.remark == "主号备注"


def test_prepare_sub2api_export_payload_applies_defaults_and_notes_fallback(temp_database):
    account_a = create_account("one@example.com", remark="备注 A")
    account_b = create_account("two@example.com", remark="")
    service = create_sub2api_service(template_config=None, next_name_index=None)

    with get_db() as db:
        _, payload = sub2api_upload.prepare_sub2api_export_payload(
            db,
            [crud.get_account_by_id(db, account_a.id), crud.get_account_by_id(db, account_b.id)],
            service_id=service.id,
        )
        db_service = crud.get_sub2api_service_by_id(db, service.id)

    assert "exported_at" in payload
    assert payload["accounts"][0]["name"] == "GPT-Free-000000001"
    assert payload["accounts"][1]["name"] == "GPT-Free-000000002"
    assert payload["accounts"][0]["notes"] == "备注 A"
    assert payload["accounts"][1]["notes"] == "two@example.com"
    assert payload["accounts"][0]["concurrency"] == 1
    assert payload["accounts"][0]["priority"] == 50
    assert db_service.next_name_index == 3


def test_upload_to_sub2api_keeps_incremented_index_after_failure(temp_database, monkeypatch):
    account = create_account("failure@example.com", remark="失败备注")
    service = create_sub2api_service(next_name_index=5)
    calls = []

    def fake_post(url, **kwargs):
        calls.append({"url": url, "kwargs": kwargs})
        return FakeResponse(status_code=500, text="server error")

    monkeypatch.setattr(sub2api_upload.cffi_requests, "post", fake_post)

    success, message = sub2api_upload.upload_to_sub2api(
        [account],
        service.api_url,
        service.api_key,
        service_id=service.id,
    )

    assert success is False
    assert "上传失败" in message
    payload = calls[0]["kwargs"]["json"]
    assert payload["data"]["accounts"][0]["name"] == "GPT-Free-000000005"
    assert payload["data"]["accounts"][0]["notes"] == "失败备注"
    assert payload["data"]["accounts"][0]["concurrency"] == 1
    with get_db() as db:
        saved_service = crud.get_sub2api_service_by_id(db, service.id)
        assert saved_service.next_name_index == 6


def test_batch_upload_to_sub2api_marks_missing_refresh_token_as_failed(temp_database, monkeypatch):
    account = create_account("missing-rt@example.com", refresh_token="")

    monkeypatch.setattr(
        sub2api_upload.cffi_requests,
        "post",
        lambda *args, **kwargs: pytest.fail("Sub2API upload should not start without refresh_token"),
    )

    results = sub2api_upload.batch_upload_to_sub2api(
        [account.id],
        "https://sub2api.example.test",
        "api-key",
    )

    assert results["success_count"] == 0
    assert results["failed_count"] == 1
    assert results["skipped_count"] == 0
    assert "refresh_token" in results["details"][0]["error"]


def test_export_route_uses_selected_service_counter(temp_database):
    account = create_account("export@example.com", remark="导出备注")
    service_a = create_sub2api_service(name="A", next_name_index=2)
    service_b = create_sub2api_service(name="B", next_name_index=9, priority=1)

    response = asyncio.run(
        accounts_routes.export_accounts_sub2api(
            accounts_routes.BatchExportRequest(ids=[account.id], service_id=service_b.id),
        )
    )
    payload = json.loads(asyncio.run(read_streaming_response(response)))

    assert payload["accounts"][0]["name"] == "GPT-Free-000000009"
    assert payload["accounts"][0]["notes"] == "导出备注"
    with get_db() as db:
        saved_a = crud.get_sub2api_service_by_id(db, service_a.id)
        saved_b = crud.get_sub2api_service_by_id(db, service_b.id)
        assert saved_a.next_name_index == 2
        assert saved_b.next_name_index == 10


def test_create_sub2api_service_route_returns_default_template_config(temp_database):
    response = asyncio.run(
        sub2api_service_routes.create_sub2api_service(
            sub2api_service_routes.Sub2ApiServiceCreate(
                name="Route Service",
                api_url="https://route.example.test",
                api_key="route-key",
            )
        )
    )

    assert response.template_config.default_concurrency == 1
    assert response.template_config.default_priority == 50
    assert response.template_config.default_group_ids == []
    assert response.next_name_index == 1


def test_prepare_sub2api_export_payload_fills_smallest_gap_from_fixed_group(temp_database, monkeypatch):
    account = create_account("aligned@example.com")
    service = create_sub2api_service(
        template_config={"default_group_ids": [88]},
        next_name_index=2,
    )

    monkeypatch.setattr(
        sub2api_payload,
        "discover_sub2api_identity_occupied_name_indices",
        lambda *args, **kwargs: {1, 2, 3, 5, 6},
    )

    with get_db() as db:
        _, payload = sub2api_upload.prepare_sub2api_export_payload(
            db,
            [crud.get_account_by_id(db, account.id)],
            service_id=service.id,
        )
        saved_service = crud.get_sub2api_service_by_id(db, service.id)

    expected_name = sub2api_payload.format_sub2api_name(normalize_sub2api_template_config(service.template_config), 4)
    assert payload["accounts"][0]["name"] == expected_name
    assert saved_service.next_name_index == 7


def test_prepare_sub2api_export_payload_reserves_multiple_gaps_from_fixed_group(temp_database, monkeypatch):
    account_a = create_account("gap-a@example.com")
    account_b = create_account("gap-b@example.com")
    service = create_sub2api_service(
        template_config={"default_group_ids": [88]},
        next_name_index=20,
    )

    monkeypatch.setattr(
        sub2api_payload,
        "discover_sub2api_identity_occupied_name_indices",
        lambda *args, **kwargs: {1, 2, 3, 5, 6},
    )

    with get_db() as db:
        _, payload = sub2api_upload.prepare_sub2api_export_payload(
            db,
            [crud.get_account_by_id(db, account_a.id), crud.get_account_by_id(db, account_b.id)],
            service_id=service.id,
        )
        saved_service = crud.get_sub2api_service_by_id(db, service.id)

    generated_names = [item["name"] for item in payload["accounts"]]
    assert generated_names == ["GPT-Free-000000004", "GPT-Free-000000007"]
    assert saved_service.next_name_index == 20


def test_prepare_sub2api_export_payload_starts_from_one_for_empty_fixed_group(temp_database, monkeypatch):
    account = create_account("empty-group@example.com")
    service = create_sub2api_service(
        template_config={"default_group_ids": [88]},
        next_name_index=9,
    )

    monkeypatch.setattr(
        sub2api_payload,
        "discover_sub2api_identity_occupied_name_indices",
        lambda *args, **kwargs: set(),
    )

    with get_db() as db:
        _, payload = sub2api_upload.prepare_sub2api_export_payload(
            db,
            [crud.get_account_by_id(db, account.id)],
            service_id=service.id,
        )
        saved_service = crud.get_sub2api_service_by_id(db, service.id)

    assert payload["accounts"][0]["name"] == "GPT-Free-000000001"
    assert saved_service.next_name_index == 9


def test_prepare_sub2api_export_payload_falls_back_to_local_counter_on_remote_error(temp_database, monkeypatch):
    account = create_account("fallback@example.com")
    service = create_sub2api_service(
        template_config={"default_group_ids": [88]},
        next_name_index=4,
    )

    monkeypatch.setattr(
        sub2api_payload,
        "discover_sub2api_identity_occupied_name_indices",
        lambda *args, **kwargs: (_ for _ in ()).throw(RuntimeError("remote unavailable")),
    )

    with get_db() as db:
        _, payload = sub2api_upload.prepare_sub2api_export_payload(
            db,
            [crud.get_account_by_id(db, account.id)],
            service_id=service.id,
        )
        saved_service = crud.get_sub2api_service_by_id(db, service.id)

    expected_name = sub2api_payload.format_sub2api_name(normalize_sub2api_template_config(service.template_config), 4)
    assert payload["accounts"][0]["name"] == expected_name
    assert saved_service.next_name_index == 5


def test_prepare_sub2api_export_payload_uses_local_counter_for_multiple_groups(temp_database, monkeypatch):
    account = create_account("multi-group@example.com")
    service = create_sub2api_service(
        template_config={"default_group_ids": [88, 99]},
        next_name_index=6,
    )

    monkeypatch.setattr(
        sub2api_payload,
        "discover_sub2api_identity_occupied_name_indices",
        lambda *args, **kwargs: (_ for _ in ()).throw(AssertionError("smart naming should not run")),
    )

    with get_db() as db:
        _, payload = sub2api_upload.prepare_sub2api_export_payload(
            db,
            [crud.get_account_by_id(db, account.id)],
            service_id=service.id,
        )
        saved_service = crud.get_sub2api_service_by_id(db, service.id)

    assert payload["accounts"][0]["name"] == "GPT-Free-000000006"
    assert saved_service.next_name_index == 7


def test_normalize_sub2api_template_config_keeps_unique_group_ids():
    config = normalize_sub2api_template_config({"default_group_ids": [3, "5", 3, 0, "bad", 9]})
    assert config["default_group_ids"] == [3, 5, 9]


def test_discover_sub2api_identity_occupied_name_indices_ignores_unmatched_names(monkeypatch):
    monkeypatch.setattr(
        sub2api_naming,
        "list_sub2api_group_account_names",
        lambda *args, **kwargs: [
            "GPT-Free-000000001",
            "GPT-Free-000000003",
            "GPT-Team-000000002",
            "GPT-Free-abc",
            "GPT-Free-000000004-extra",
        ],
    )

    occupied = sub2api_naming.discover_sub2api_identity_occupied_name_indices(
        "https://sub2api.example.test",
        "api-key",
        88,
        "free",
        9,
    )

    assert occupied == {1, 3}


def test_fetch_sub2api_groups_route_uses_saved_service_credentials(temp_database, monkeypatch):
    service = create_sub2api_service()

    monkeypatch.setattr(
        sub2api_service_routes,
        "fetch_sub2api_groups",
        lambda api_url, api_key, platform="openai": [
            {
                "id": 11,
                "name": "OpenAI Default",
                "platform": platform,
                "status": "active",
                "subscription_type": "standard",
                "rate_multiplier": 1.0,
                "account_count": 3,
                "active_account_count": 2,
                "rate_limited_account_count": 0,
            }
        ],
    )

    response = asyncio.run(
        sub2api_service_routes.fetch_sub2api_service_groups(
            sub2api_service_routes.Sub2ApiFetchGroupsRequest(service_id=service.id)
        )
    )

    assert response[0]["id"] == 11
    assert response[0]["name"] == "OpenAI Default"


def test_upload_to_sub2api_binds_default_groups_after_import(temp_database, monkeypatch):
    account = create_account("grouped@example.com", remark="分组备注")
    service = create_sub2api_service(
        template_config={"default_group_ids": [11, 12]},
        next_name_index=7,
    )
    calls = []
    looked_up = []
    bound = []

    def fake_post(url, **kwargs):
        calls.append({"url": url, "kwargs": kwargs})
        return FakeResponse(status_code=200, payload={"code": 0, "message": "success", "data": {}})

    def fake_lookup(api_url, api_key, names, platform="openai"):
        looked_up.append({"api_url": api_url, "api_key": api_key, "names": list(names), "platform": platform})
        return {name: index + 100 for index, name in enumerate(names)}

    def fake_bind(api_url, api_key, account_ids, group_ids):
        bound.append(
            {
                "api_url": api_url,
                "api_key": api_key,
                "account_ids": list(account_ids),
                "group_ids": list(group_ids),
            }
        )
        return {"success": True}

    monkeypatch.setattr(sub2api_upload.cffi_requests, "post", fake_post)
    monkeypatch.setattr(sub2api_upload, "find_sub2api_account_ids_by_names", fake_lookup)
    monkeypatch.setattr(sub2api_upload, "bind_sub2api_accounts_to_groups", fake_bind)

    success, message = sub2api_upload.upload_to_sub2api(
        [account],
        service.api_url,
        service.api_key,
        service_id=service.id,
    )

    assert success is True
    assert "生成 2 份分组副本" in message
    generated_names = [call["kwargs"]["json"]["data"]["accounts"][0]["name"] for call in calls]
    assert generated_names == ["GPT-Free-000000007", "GPT-Free-000000008"]
    assert [item["names"] for item in looked_up] == [[generated_names[0]], [generated_names[1]]]
    assert [item["group_ids"] for item in bound] == [[11], [12]]
    assert [item["account_ids"] for item in bound] == [[100], [100]]


def test_upload_to_sub2api_returns_partial_failure_when_group_bind_fails(temp_database, monkeypatch):
    account = create_account("bind-fail@example.com")
    service = create_sub2api_service(template_config={"default_group_ids": [9]}, next_name_index=3)

    def fake_post(url, **kwargs):
        return FakeResponse(status_code=200, payload={"code": 0, "message": "success", "data": {}})

    monkeypatch.setattr(sub2api_upload.cffi_requests, "post", fake_post)
    monkeypatch.setattr(
        sub2api_upload,
        "find_sub2api_account_ids_by_names",
        lambda api_url, api_key, names, platform="openai": {list(names)[0]: 88},
    )
    monkeypatch.setattr(
        sub2api_upload,
        "bind_sub2api_accounts_to_groups",
        lambda api_url, api_key, account_ids, group_ids: (_ for _ in ()).throw(RuntimeError("group bind failed")),
    )

    success, message = sub2api_upload.upload_to_sub2api(
        [account],
        service.api_url,
        service.api_key,
        service_id=service.id,
    )

    assert success is False
    assert "自动绑定分组失败" in message


def test_upload_to_sub2api_skips_group_binding_when_not_configured(temp_database, monkeypatch):
    account = create_account("nogroup@example.com")
    service = create_sub2api_service(template_config={"default_group_ids": []}, next_name_index=2)

    def fake_post(url, **kwargs):
        return FakeResponse(status_code=200, payload={"code": 0, "message": "success", "data": {}})

    monkeypatch.setattr(sub2api_upload.cffi_requests, "post", fake_post)
    monkeypatch.setattr(
        sub2api_upload,
        "find_sub2api_account_ids_by_names",
        lambda *args, **kwargs: (_ for _ in ()).throw(AssertionError("lookup should not be called")),
    )
    monkeypatch.setattr(
        sub2api_upload,
        "bind_sub2api_accounts_to_groups",
        lambda *args, **kwargs: (_ for _ in ()).throw(AssertionError("bind should not be called")),
    )

    success, message = sub2api_upload.upload_to_sub2api(
        [account],
        service.api_url,
        service.api_key,
        service_id=service.id,
    )

    assert success is True
    assert "成功上传" in message


def test_batch_upload_to_sub2api_blocks_team_multigroup_copy_before_request(temp_database, monkeypatch):
    account = create_account(
        "team-multi@example.com",
        subscription_type="team",
        account_id="team-acct-1",
        workspace_id="team-acct-1",
    )
    service = create_sub2api_service(template_config={"default_group_ids": [11, 12]})
    calls = []

    def fake_post(url, **kwargs):
        calls.append({"url": url, "kwargs": kwargs})
        return FakeResponse(status_code=200, payload={"code": 0, "message": "success", "data": {}})

    monkeypatch.setattr(sub2api_upload.cffi_requests, "post", fake_post)

    results = sub2api_upload.batch_upload_to_sub2api(
        [account.id],
        service.api_url,
        service.api_key,
        service_id=service.id,
        team_context={"team_account_id": "team-acct-1", "team_task_uuid": "team-task-1"},
    )

    assert calls == []
    assert results["success_count"] == 0
    assert results["failed_count"] == 1
    assert results["details"][0]["reason_code"] == team_upload_guard.TEAM_MULTIGROUP_COPY_BLOCKED
    assert results["details"][0]["guard_blocked"] is True


def test_batch_upload_to_sub2api_blocks_reupload_with_same_team_refresh_token(temp_database, monkeypatch):
    account = create_account(
        "team-repeat@example.com",
        subscription_type="team",
        account_id="team-acct-1",
        workspace_id="team-acct-1",
        refresh_token="repeat-refresh-token",
    )
    service = create_sub2api_service(next_name_index=4)
    calls = []

    def fake_post(url, **kwargs):
        calls.append({"url": url, "kwargs": kwargs})
        return FakeResponse(status_code=200, payload={"code": 0, "message": "success", "data": {}})

    monkeypatch.setattr(sub2api_upload.cffi_requests, "post", fake_post)

    first = sub2api_upload.batch_upload_to_sub2api(
        [account.id],
        service.api_url,
        service.api_key,
        service_id=service.id,
        team_context={"team_account_id": "team-acct-1", "team_task_uuid": "team-task-first"},
    )
    second = sub2api_upload.batch_upload_to_sub2api(
        [account.id],
        service.api_url,
        service.api_key,
        service_id=service.id,
        team_context={"team_account_id": "team-acct-1", "team_task_uuid": "team-task-second"},
    )

    assert first["success_count"] == 1
    assert len(calls) == 1
    assert second["success_count"] == 0
    assert second["failed_count"] == 1
    assert second["details"][0]["reason_code"] == team_upload_guard.TEAM_REFRESH_TOKEN_REUPLOAD_BLOCKED
    assert second["details"][0]["guard_blocked"] is True

    with get_db() as db:
        saved = crud.get_account_by_id(db, account.id)
        records = (
            saved.extra_data[team_upload_guard.TEAM_UPLOAD_GUARD_KEY][team_upload_guard.TEAM_UPLOAD_RECORDS_KEY]
        )
    assert records


def test_batch_upload_to_sub2api_marks_refresh_token_reused_as_unrecoverable(temp_database, monkeypatch):
    account = create_account(
        "team-reused@example.com",
        subscription_type="team",
        account_id="team-acct-1",
        workspace_id="team-acct-1",
    )
    service = create_sub2api_service()

    def fake_post(url, **kwargs):
        return FakeResponse(
            status_code=502,
            payload={
                "message": (
                    'token refresh retry exhausted: error: code=502 '
                    'reason="OPENAI_OAUTH_TOKEN_REFRESH_FAILED" '
                    'message="token refresh failed: status 401, body: '
                    '{"error":{"type":"invalid_request_error","code":"refresh_token_reused"}}"'
                )
            },
        )

    monkeypatch.setattr(sub2api_upload.cffi_requests, "post", fake_post)

    results = sub2api_upload.batch_upload_to_sub2api(
        [account.id],
        service.api_url,
        service.api_key,
        service_id=service.id,
        team_context={"team_account_id": "team-acct-1", "team_task_uuid": "team-task-error"},
    )

    assert results["success_count"] == 0
    assert results["failed_count"] == 1
    detail = results["details"][0]
    assert detail["reason_code"] == team_upload_guard.REFRESH_TOKEN_REUSED
    assert detail["guard_blocked"] is False
    assert "重新登录" in detail["guard_message"]
