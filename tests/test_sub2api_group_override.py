import pytest

from src.database.init_db import initialize_database
from src.database.models import Account, Sub2ApiService
from src.database.session import get_db
from src.database import session as db_session
from src.core.upload import sub2api_upload


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
    db_url = f"sqlite:///{tmp_path / 'sub2api-group-override.db'}"
    db_session._db_manager = None
    monkeypatch.setenv("APP_DATABASE_URL", db_url)
    initialize_database(db_url)
    yield
    db_session._db_manager = None


def create_account(email: str) -> Account:
    with get_db() as db:
        account = Account(
            email=email,
            password="secret",
            access_token="access-token",
            refresh_token="refresh-token",
            client_id="client-id",
            account_id="account-id",
            workspace_id="workspace-id",
            email_service="moe_mail",
            status="active",
        )
        db.add(account)
        db.commit()
        db.refresh(account)
        return account


def create_sub2api_service() -> Sub2ApiService:
    with get_db() as db:
        service = Sub2ApiService(
            name="Sub2API",
            api_url="https://sub2api.example.test",
            api_key="api-key",
            template_config={"default_group_ids": [11]},
            next_name_index=2,
            enabled=True,
            priority=0,
        )
        db.add(service)
        db.commit()
        db.refresh(service)
        return service


def test_upload_to_sub2api_uses_group_override_over_default_groups(temp_database, monkeypatch):
    account = create_account("override-group@example.com")
    service = create_sub2api_service()
    bound_groups = []

    monkeypatch.setattr(
        sub2api_upload.cffi_requests,
        "post",
        lambda url, **kwargs: FakeResponse(status_code=200, payload={"code": 0, "message": "success", "data": {}}),
    )
    monkeypatch.setattr(
        sub2api_upload,
        "find_sub2api_account_ids_by_names",
        lambda api_url, api_key, names, platform="openai": {list(names)[0]: 77},
    )
    monkeypatch.setattr(
        sub2api_upload,
        "bind_sub2api_accounts_to_groups",
        lambda api_url, api_key, account_ids, group_ids: bound_groups.append(list(group_ids)) or {"success": True},
    )

    success, _ = sub2api_upload.upload_to_sub2api(
        [account],
        service.api_url,
        service.api_key,
        service_id=service.id,
        group_ids_override=[21, 22],
    )

    assert success is True
    assert bound_groups == [[21], [22]]
