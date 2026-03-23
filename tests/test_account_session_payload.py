import asyncio
import base64
import json
from datetime import datetime

import pytest

from src.core.openai import account_sensitive_info
from src.database import session as db_session
from src.database.init_db import initialize_database
from src.database.models import Account
from src.database.session import get_db
from src.web.routes import accounts as accounts_routes


@pytest.fixture()
def temp_database(tmp_path, monkeypatch):
    db_url = f"sqlite:///{tmp_path / 'account-session-tests.db'}"
    db_session._db_manager = None
    monkeypatch.setenv("APP_DATABASE_URL", db_url)
    initialize_database(db_url)
    yield
    db_session._db_manager = None


def _jwt(payload: dict) -> str:
    header = {"alg": "none", "typ": "JWT"}

    def _encode(data: dict) -> str:
        raw = json.dumps(data, separators=(",", ":"), ensure_ascii=False).encode("utf-8")
        return base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")

    return f"{_encode(header)}.{_encode(payload)}.sig"


def create_account() -> int:
    access_token = _jwt(
        {
            "iat": 1760000000,
            "exp": 1770000000,
            "https://api.openai.com/auth": {
                "chatgpt_account_id": "acct-from-access",
                "chatgpt_user_id": "user-from-access",
                "chatgpt_plan_type": "team",
                "chatgpt_compute_residency": "no_constraint",
            },
            "https://api.openai.com/profile": {
                "email": "token@example.com",
                "email_verified": True,
            },
        }
    )
    id_token = _jwt(
        {
            "sub": "google-oauth2|1234567890",
            "name": "Token User",
            "email": "id@example.com",
            "picture": "https://example.com/avatar.png",
            "iat": 1750000000,
        }
    )

    with get_db() as db:
        account = Account(
            email="local@example.com",
            password="secret",
            access_token=access_token,
            refresh_token="refresh-token",
            id_token=id_token,
            session_token="session-token",
            client_id="client-id",
            account_id="db-account-id",
            workspace_id="db-workspace-id",
            email_service="moe_mail",
            proxy_used="http://127.0.0.1:8080",
            expires_at=datetime(2026, 6, 21, 14, 10, 21),
            subscription_type="team",
            status="active",
        )
        db.add(account)
        db.commit()
        db.refresh(account)
        return account.id


def test_build_account_session_payload_falls_back_to_local_tokens(monkeypatch):
    monkeypatch.setattr(account_sensitive_info, "fetch_remote_session_payload", lambda *args, **kwargs: None)
    account = Account(
        email="local@example.com",
        access_token=_jwt(
            {
                "iat": 1760000000,
                "exp": 1770000000,
                "https://api.openai.com/auth": {
                    "chatgpt_account_id": "acct-1",
                    "chatgpt_user_id": "user-1",
                    "chatgpt_plan_type": "free",
                    "chatgpt_compute_residency": "no_constraint",
                },
                "https://api.openai.com/profile": {"email": "profile@example.com"},
            }
        ),
        id_token=_jwt(
            {
                "sub": "google-oauth2|abc",
                "name": "Local Name",
                "picture": "https://example.com/p.png",
                "iat": 1750000000,
            }
        ),
        session_token="session-token",
        account_id="acct-db",
        workspace_id="ws-db",
        subscription_type="team",
        expires_at=datetime(2026, 6, 21, 14, 10, 21),
    )

    payload = account_sensitive_info.build_account_sensitive_session_payload(account)

    assert payload["WARNING_BANNER"].startswith("!!!!!!!!!!!!!!!!!!!!")
    assert payload["user"]["id"] == "user-1"
    assert payload["user"]["name"] == "Local Name"
    assert payload["user"]["email"] == "profile@example.com"
    assert payload["user"]["idp"] == "google-oauth2"
    assert payload["account"]["id"] == "acct-1"
    assert payload["account"]["planType"] == "free"
    assert payload["account"]["structure"] == "team"
    assert payload["accessToken"] == account.access_token
    assert payload["sessionToken"] == "session-token"
    assert payload["rumViewTags"]["light_account"]["fetched"] is False


def test_route_session_payload_prefers_remote_session(temp_database, monkeypatch):
    account_id = create_account()
    monkeypatch.setattr(
        account_sensitive_info,
        "fetch_remote_session_payload",
        lambda *args, **kwargs: {
            "user": {
                "id": "remote-user",
                "name": "Remote Name",
                "email": "remote@example.com",
                "image": "https://example.com/remote.png",
                "picture": "https://example.com/remote.png",
                "idp": "google-oauth2",
                "iat": 1768379159,
                "mfa": False,
            },
            "expires": "2026-06-21T14:10:21.582Z",
            "account": {
                "id": "remote-account",
                "planType": "free",
                "structure": "personal",
                "isConversationClassifierEnabledForWorkspace": True,
            },
            "accessToken": "remote-access-token",
            "authProvider": "openai",
            "sessionToken": "remote-session-token",
            "rumViewTags": {"light_account": {"fetched": True}},
        },
    )

    payload = asyncio.run(accounts_routes.get_account_session_payload(account_id))

    assert payload["user"]["name"] == "Remote Name"
    assert payload["user"]["email"] == "remote@example.com"
    assert payload["account"]["id"] == "remote-account"
    assert payload["account"]["planType"] == "free"
    assert payload["accessToken"] == "remote-access-token"
    assert payload["sessionToken"] == "remote-session-token"
    assert payload["rumViewTags"]["light_account"]["fetched"] is True


def test_refresh_route_persists_sensitive_session_payload(temp_database, monkeypatch):
    account_id = create_account()
    monkeypatch.setattr(account_sensitive_info, "fetch_remote_session_payload", lambda *args, **kwargs: None)

    response = asyncio.run(accounts_routes.refresh_account_session_payload(account_id))

    assert response["success"] is True
    assert response["payload"]["accessToken"]
    assert response["updated_at"]

    with get_db() as db:
        account = db.query(Account).filter(Account.id == account_id).first()
        extra_data = account.extra_data or {}
        assert extra_data[account_sensitive_info.SENSITIVE_SESSION_PAYLOAD_KEY]["accessToken"] == account.access_token
        assert extra_data[account_sensitive_info.SENSITIVE_SESSION_PAYLOAD_UPDATED_AT_KEY] == response["updated_at"]
