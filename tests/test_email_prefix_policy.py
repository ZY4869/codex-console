from src.config.constants import EmailServiceType
from src.services.base import BaseEmailService, EmailServiceError, EmailServiceFactory


class DummyCreateService(BaseEmailService):
    def __init__(self, config, name=None):
        super().__init__(EmailServiceType.TEMPMAIL, name)
        self.config = config or {}
        self.last_create_config = None

    def create_email(self, config=None):
        self.last_create_config = config
        return {
            "email": "clean123@example.com",
            "service_id": "svc-1",
        }

    def get_verification_code(self, email, email_id=None, timeout=120, pattern=None, otp_sent_at=None):
        return None

    def list_emails(self, **kwargs):
        return []

    def delete_email(self, email_id: str) -> bool:
        return True

    def check_health(self) -> bool:
        return True


class DummyInvalidRemoteService(DummyCreateService):
    def create_email(self, config=None):
        self.last_create_config = config
        return {
            "email": "bad.prefix@example.com",
            "service_id": "svc-2",
        }


class DummyOutlookSelectionService(BaseEmailService):
    def __init__(self, config, name=None):
        super().__init__(EmailServiceType.OUTLOOK, name)

    def create_email(self, config=None):
        return {
            "email": "name.with.dot@outlook.com",
            "service_id": "name.with.dot@outlook.com",
        }

    def get_verification_code(self, email, email_id=None, timeout=120, pattern=None, otp_sent_at=None):
        return None

    def list_emails(self, **kwargs):
        return []

    def delete_email(self, email_id: str) -> bool:
        return True

    def check_health(self) -> bool:
        return True


def test_factory_sanitizes_requested_email_prefix(monkeypatch):
    monkeypatch.setattr(
        "src.services.base._load_email_prefix_alnum_only_setting",
        lambda: True,
    )
    monkeypatch.setitem(EmailServiceFactory._registry, EmailServiceType.TEMPMAIL, DummyCreateService)

    service = EmailServiceFactory.create(EmailServiceType.TEMPMAIL, {})
    result = service.create_email({"name": "ab.-_12", "address": "x.y+z@test.example.com"})

    assert result["email"] == "clean123@example.com"
    assert service.last_create_config["name"] == "ab12"
    assert service.last_create_config["address"] == "xyz@test.example.com"


def test_factory_rejects_remote_email_with_special_prefix(monkeypatch):
    monkeypatch.setattr(
        "src.services.base._load_email_prefix_alnum_only_setting",
        lambda: True,
    )
    monkeypatch.setitem(EmailServiceFactory._registry, EmailServiceType.TEMPMAIL, DummyInvalidRemoteService)

    service = EmailServiceFactory.create(EmailServiceType.TEMPMAIL, {})

    try:
        service.create_email()
    except EmailServiceError as exc:
        assert "邮箱前缀策略" in str(exc)
    else:
        raise AssertionError("expected EmailServiceError for invalid remote prefix")


def test_factory_skips_prefix_validation_for_outlook_selection(monkeypatch):
    monkeypatch.setattr(
        "src.services.base._load_email_prefix_alnum_only_setting",
        lambda: True,
    )
    monkeypatch.setitem(EmailServiceFactory._registry, EmailServiceType.OUTLOOK, DummyOutlookSelectionService)

    service = EmailServiceFactory.create(EmailServiceType.OUTLOOK, {})
    payload = service.create_email()

    assert payload["email"] == "name.with.dot@outlook.com"
