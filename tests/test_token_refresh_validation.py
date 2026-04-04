from contextlib import contextmanager

from src.config.constants import AccountStatus
from src.core.openai import token_refresh
from src.core.openai.token_refresh import TokenRefreshResult
from src.database.models import Account


def test_validate_account_token_retries_refresh_before_marking_failed(monkeypatch):
    account = Account(
        id=1,
        email="refreshable@example.com",
        access_token="stale-access-token",
        refresh_token="",
        session_token="session-token",
        status=AccountStatus.ACTIVE.value,
    )
    updates = []

    @contextmanager
    def fake_get_db():
        yield object()

    def fake_get_account_by_id(_db, account_id):
        assert account_id == 1
        return account

    def fake_update_account(_db, account_id, **kwargs):
        assert account_id == 1
        updates.append(kwargs)
        for key, value in kwargs.items():
            setattr(account, key, value)

    class FakeManager:
        def __init__(self, proxy_url=None):
            self.proxy_url = proxy_url

        def validate_token(self, access_token, timeout_seconds=30):
            if access_token == "stale-access-token":
                return False, "Token 无效（401）"
            if access_token == "fresh-access-token":
                return True, None
            raise AssertionError(f"unexpected token: {access_token}")

        def refresh_account(self, current_account):
            assert current_account is account
            return TokenRefreshResult(
                success=True,
                access_token="fresh-access-token",
            )

    monkeypatch.setattr(token_refresh, "get_db", fake_get_db)
    monkeypatch.setattr(token_refresh.crud, "get_account_by_id", fake_get_account_by_id)
    monkeypatch.setattr(token_refresh.crud, "update_account", fake_update_account)
    monkeypatch.setattr(token_refresh, "TokenRefreshManager", FakeManager)

    is_valid, error = token_refresh.validate_account_token(1, proxy_url="http://127.0.0.1:7890", timeout_seconds=5)

    assert is_valid is True
    assert error is None
    assert account.access_token == "fresh-access-token"
    assert updates[0]["access_token"] == "fresh-access-token"
    assert "last_refresh" in updates[0]
    assert all("status" not in item for item in updates)
