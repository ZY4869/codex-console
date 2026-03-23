from contextlib import contextmanager
import sys
from types import SimpleNamespace

from src.config.constants import OPENAI_PAGE_TYPES
from src.database.models import Account
from src.web.routes import team as team_routes

team_workflow = sys.modules["src.core.team_workflow"]


class FakeEmailService:
    def __init__(self, existing_emails=None):
        self.existing_emails = list(existing_emails or [])
        self.create_calls = []

    def list_emails(self, **kwargs):
        return list(self.existing_emails)

    def create_email(self, config=None):
        payload = dict(config or {})
        self.create_calls.append(payload)
        email = f"{payload['name']}@{payload['domain']}"
        return {
            "email": email,
            "service_id": "mailbox-1",
            "id": "mailbox-1",
        }


class FakeCookies:
    def __init__(self, values):
        self._values = dict(values)

    def items(self):
        return self._values.items()


class FakeRegistrationEngine:
    def __init__(self, email_service, proxy_url=None, callback_logger=None):
        self.email_service = email_service
        self.proxy_url = proxy_url
        self.callback_logger = callback_logger
        self.logs = []
        self.email = None
        self.password = None
        self.email_info = None
        self._is_existing_account = False
        self.session = SimpleNamespace(cookies=FakeCookies({}))

    def _prepare_authorize_flow(self, label):
        return "did-1", "sentinel-1"

    def _submit_login_start(self, did, sen_token):
        return SimpleNamespace(success=True, page_type=OPENAI_PAGE_TYPES["LOGIN_PASSWORD"])

    def _submit_login_password(self):
        return SimpleNamespace(
            success=True,
            is_existing_account=True,
            page_type=OPENAI_PAGE_TYPES["EMAIL_OTP_VERIFICATION"],
        )

    def _complete_token_exchange(self, result):
        result.email = self.email
        result.account_id = "acct-after-login"
        result.workspace_id = "ws-after-login"
        result.access_token = "access-after-login"
        result.refresh_token = "refresh-after-login"
        result.id_token = "id-after-login"
        result.session_token = "session-after-login"
        result.source = "login"
        return True


def test_ensure_account_email_mailbox_reuses_existing_mailbox():
    account = Account(email="tester@kan69.fun", email_service="temp_mail", email_service_id="")
    service = FakeEmailService(
        existing_emails=[
            {"email": "tester@kan69.fun", "service_id": "existing-mailbox"},
        ]
    )

    result = team_workflow.ensure_account_email_mailbox(service, account)

    assert result["success"] is True
    assert result["created"] is False
    assert result["email_info"]["service_id"] == "existing-mailbox"
    assert service.create_calls == []


def test_recover_account_session_via_login_recreates_missing_mailbox(monkeypatch):
    account = Account(
        email="tester@kan69.fun",
        password="secret",
        email_service="temp_mail",
        email_service_id="",
    )
    fake_service = FakeEmailService(existing_emails=[])

    @contextmanager
    def fake_get_db():
        yield None

    monkeypatch.setattr(team_workflow, "get_db", fake_get_db)
    monkeypatch.setattr(team_workflow, "build_inbox_config", lambda db, service_type, email: {"base_url": "https://mail.test"})
    monkeypatch.setattr(team_workflow.EmailServiceFactory, "create", lambda service_type, config: fake_service)
    monkeypatch.setattr(team_workflow, "RegistrationEngine", FakeRegistrationEngine)

    result = team_workflow.recover_account_session_via_login(account, proxy_url="http://127.0.0.1:7890")

    assert result["success"] is True
    assert fake_service.create_calls == [{"name": "tester", "domain": "kan69.fun"}]
    assert result["session_token"] == "session-after-login"
    assert result["email_service_id"] == "mailbox-1"


def test_recover_account_session_via_login_falls_back_to_cookie_session_token(monkeypatch):
    account = Account(
        email="tester@kan69.fun",
        password="secret",
        email_service="temp_mail",
        email_service_id="",
    )
    fake_service = FakeEmailService(existing_emails=[{"email": "tester@kan69.fun", "service_id": "mailbox-1"}])

    @contextmanager
    def fake_get_db():
        yield None

    class CookieOnlyRegistrationEngine(FakeRegistrationEngine):
        def __init__(self, email_service, proxy_url=None, callback_logger=None):
            super().__init__(email_service, proxy_url=proxy_url, callback_logger=callback_logger)
            self.session = SimpleNamespace(
                cookies=FakeCookies({
                    "__Secure-next-auth.session-token": "cookie-session-token",
                    "foo": "bar",
                })
            )

        def _complete_token_exchange(self, result):
            result.email = self.email
            result.account_id = "acct-after-login"
            result.workspace_id = "ws-after-login"
            result.access_token = "access-after-login"
            result.refresh_token = "refresh-after-login"
            result.id_token = "id-after-login"
            result.session_token = ""
            result.source = "login"
            return True

    monkeypatch.setattr(team_workflow, "get_db", fake_get_db)
    monkeypatch.setattr(team_workflow, "build_inbox_config", lambda db, service_type, email: {"base_url": "https://mail.test"})
    monkeypatch.setattr(team_workflow.EmailServiceFactory, "create", lambda service_type, config: fake_service)
    monkeypatch.setattr(team_workflow, "RegistrationEngine", CookieOnlyRegistrationEngine)

    result = team_workflow.recover_account_session_via_login(account, proxy_url="http://127.0.0.1:7890")

    assert result["success"] is True
    assert result["session_token"] == "cookie-session-token"
    assert result["cookies"] == "__Secure-next-auth.session-token=cookie-session-token; foo=bar"


def test_recover_account_session_via_login_reassembles_chunked_cookie_session_token(monkeypatch):
    account = Account(
        email="tester@kan69.fun",
        password="secret",
        email_service="temp_mail",
        email_service_id="",
    )
    fake_service = FakeEmailService(existing_emails=[{"email": "tester@kan69.fun", "service_id": "mailbox-1"}])

    @contextmanager
    def fake_get_db():
        yield None

    class ChunkedCookieRegistrationEngine(FakeRegistrationEngine):
        def __init__(self, email_service, proxy_url=None, callback_logger=None):
            super().__init__(email_service, proxy_url=proxy_url, callback_logger=callback_logger)
            self.session = SimpleNamespace(
                cookies=FakeCookies({
                    "__Secure-authjs.session-token.0": "chunk-a",
                    "__Secure-authjs.session-token.1": "chunk-b",
                    "foo": "bar",
                })
            )

        def _complete_token_exchange(self, result):
            result.email = self.email
            result.account_id = "acct-after-login"
            result.workspace_id = "ws-after-login"
            result.access_token = "access-after-login"
            result.refresh_token = "refresh-after-login"
            result.id_token = "id-after-login"
            result.session_token = ""
            result.source = "login"
            return True

    monkeypatch.setattr(team_workflow, "get_db", fake_get_db)
    monkeypatch.setattr(team_workflow, "build_inbox_config", lambda db, service_type, email: {"base_url": "https://mail.test"})
    monkeypatch.setattr(team_workflow.EmailServiceFactory, "create", lambda service_type, config: fake_service)
    monkeypatch.setattr(team_workflow, "RegistrationEngine", ChunkedCookieRegistrationEngine)

    result = team_workflow.recover_account_session_via_login(account, proxy_url="http://127.0.0.1:7890")

    assert result["success"] is True
    assert result["session_token"] == "chunk-achunk-b"
    assert result["cookies"] == "__Secure-authjs.session-token.0=chunk-a; __Secure-authjs.session-token.1=chunk-b; foo=bar"


def test_recover_account_session_via_login_fails_when_no_usable_session_cookie(monkeypatch):
    account = Account(
        email="tester@kan69.fun",
        password="secret",
        email_service="temp_mail",
        email_service_id="",
    )
    fake_service = FakeEmailService(existing_emails=[{"email": "tester@kan69.fun", "service_id": "mailbox-1"}])

    @contextmanager
    def fake_get_db():
        yield None

    class NoSessionCookieRegistrationEngine(FakeRegistrationEngine):
        def __init__(self, email_service, proxy_url=None, callback_logger=None):
            super().__init__(email_service, proxy_url=proxy_url, callback_logger=callback_logger)
            self.session = SimpleNamespace(cookies=FakeCookies({"foo": "bar"}))

        def _complete_token_exchange(self, result):
            result.email = self.email
            result.account_id = "acct-after-login"
            result.workspace_id = "ws-after-login"
            result.access_token = "access-after-login"
            result.refresh_token = "refresh-after-login"
            result.id_token = "id-after-login"
            result.session_token = ""
            result.source = "login"
            return True

    monkeypatch.setattr(team_workflow, "get_db", fake_get_db)
    monkeypatch.setattr(team_workflow, "build_inbox_config", lambda db, service_type, email: {"base_url": "https://mail.test"})
    monkeypatch.setattr(team_workflow.EmailServiceFactory, "create", lambda service_type, config: fake_service)
    monkeypatch.setattr(team_workflow, "RegistrationEngine", NoSessionCookieRegistrationEngine)

    result = team_workflow.recover_account_session_via_login(account, proxy_url="http://127.0.0.1:7890")

    assert result["success"] is False
    assert "session_token" in result["error"]
    assert "foo" in result["error"]
