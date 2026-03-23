import pytest

from src.core.upload import cpa_upload
from src.core.upload import sub2api_upload
from src.database import session as db_session
from src.database.init_db import initialize_database
from src.database.models import Account, Sub2ApiService
from src.database.session import get_db


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
    db_url = f"sqlite:///{tmp_path / 'team-upload-tests.db'}"
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
            session_token=overrides.get("session_token", "session-token"),
            client_id=overrides.get("client_id", "client-id"),
            account_id=overrides.get("account_id", "personal-account-id"),
            workspace_id=overrides.get("workspace_id", "personal-workspace-id"),
            email_service=overrides.get("email_service", "moe_mail"),
            subscription_type=overrides.get("subscription_type", "team"),
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
            next_name_index=overrides.get("next_name_index", 1),
            enabled=True,
            priority=0,
        )
        db.add(service)
        db.commit()
        db.refresh(service)
        return service


def test_upload_to_sub2api_overrides_team_space_fields_from_team_context(temp_database, monkeypatch):
    account = create_account("team@example.com")
    service = create_sub2api_service(next_name_index=2)
    calls = []

    def fake_post(url, **kwargs):
        calls.append({"url": url, "kwargs": kwargs})
        return FakeResponse(status_code=200, payload={"code": 0, "message": "success", "data": {}})

    monkeypatch.setattr(sub2api_upload.cffi_requests, "post", fake_post)

    success, message = sub2api_upload.upload_to_sub2api(
        [account],
        service.api_url,
        service.api_key,
        service_id=service.id,
        team_context={"team_account_id": "team-acct-1"},
    )

    assert success is True
    assert "成功上传" in message
    credentials = calls[0]["kwargs"]["json"]["data"]["accounts"][0]["credentials"]
    assert credentials["chatgpt_account_id"] == "team-acct-1"
    assert credentials["organization_id"] == "team-acct-1"


def test_generate_cpa_token_json_prefers_team_account_id_from_context():
    account = Account(
        email="team@example.com",
        access_token="access-token",
        refresh_token="refresh-token",
        id_token="id-token",
        account_id="personal-account-id",
        workspace_id="personal-workspace-id",
        email_service="moe_mail",
        status="active",
    )

    payload = cpa_upload.generate_token_json(account, team_context={"team_account_id": "team-acct-1"})

    assert payload["account_id"] == "team-acct-1"
