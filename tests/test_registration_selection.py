import asyncio
from contextlib import contextmanager
from pathlib import Path

from src.database.models import Base, Account, EmailService
from src.database.session import DatabaseSessionManager
from src.web.routes import registration as registration_routes
from src.web.routes import registration_selection


class FakeSelectableService:
    def __init__(self, config):
        self.config = config

    def list_domains(self):
        return self.config.get("test_domains", [])

    def list_emails(self):
        return self.config.get("test_addresses", [])


def _build_db_manager(name: str) -> DatabaseSessionManager:
    runtime_dir = Path("tests_runtime")
    runtime_dir.mkdir(exist_ok=True)
    db_path = runtime_dir / name
    if db_path.exists():
        db_path.unlink()

    manager = DatabaseSessionManager(f"sqlite:///{db_path}")
    Base.metadata.create_all(bind=manager.engine)
    return manager


def test_service_options_route_returns_domains_and_email_addresses(monkeypatch):
    manager = _build_db_manager("registration_options.db")
    with manager.session_scope() as session:
        session.add(
            EmailService(
                service_type="freemail",
                name="Freemail A",
                config={
                    "base_url": "https://mail.example.test",
                    "admin_token": "token",
                    "test_domains": ["alpha.test", "beta.test"],
                    "test_addresses": [
                        {"id": "mail-1", "email": "one@alpha.test"},
                        {"id": "mail-2", "email": "two@beta.test"},
                    ],
                },
                enabled=True,
                priority=0,
            )
        )

    @contextmanager
    def fake_get_db():
        session = manager.SessionLocal()
        try:
            yield session
        finally:
            session.close()

    monkeypatch.setattr(registration_routes, "get_db", fake_get_db)
    monkeypatch.setattr(
        registration_selection.EmailServiceFactory,
        "create",
        lambda service_type, config, name=None: FakeSelectableService(config),
    )

    result = asyncio.run(
        registration_routes.get_registration_service_options(
            service_type="freemail",
            service_id=1,
        )
    )

    assert result["supports_random_domain"] is True
    assert result["supports_address_selection"] is True
    assert result["domains"] == ["alpha.test", "beta.test"]
    assert [item["email"] for item in result["email_addresses"]] == [
        "one@alpha.test",
        "two@beta.test",
    ]


def test_resolve_registration_service_applies_selected_domain_and_email(monkeypatch):
    manager = _build_db_manager("registration_selection_apply.db")
    with manager.session_scope() as session:
        session.add(
            EmailService(
                service_type="moe_mail",
                name="Moe A",
                config={
                    "base_url": "https://moe.example.test",
                    "api_key": "token",
                    "default_domain": "alpha.test",
                    "test_domains": ["alpha.test", "beta.test"],
                    "test_addresses": [
                        {"id": "mail-1", "email": "one@alpha.test"},
                        {"id": "mail-2", "email": "two@beta.test"},
                    ],
                },
                enabled=True,
                priority=0,
            )
        )

    monkeypatch.setattr(
        registration_selection.EmailServiceFactory,
        "create",
        lambda service_type, config, name=None: FakeSelectableService(config),
    )

    with manager.session_scope() as session:
        resolved = registration_selection.resolve_email_service_for_registration(
            db=session,
            service_type=registration_selection.EmailServiceType.MOE_MAIL,
            requested_service_id=1,
            fallback_config=None,
            proxy_url=None,
            selection=registration_selection.RegistrationSelectionRequest(
                selected_domains=["beta.test"],
                selected_email_addresses=["two@beta.test"],
            ),
        )

    assert resolved.email_service_id == 1
    assert resolved.config["default_domain"] == "beta.test"
    assert resolved.config["domain"] == "beta.test"
    assert resolved.config["existing_email"] == "two@beta.test"
    assert resolved.config["existing_email_id"] == "mail-2"


def test_selected_domains_without_random_cycle_by_selection_index(monkeypatch):
    manager = _build_db_manager("registration_domain_cycle.db")
    with manager.session_scope() as session:
        session.add(
            EmailService(
                service_type="freemail",
                name="Freemail Cycle",
                config={
                    "base_url": "https://mail.example.test",
                    "admin_token": "token",
                    "domain": "alpha.test",
                    "test_domains": ["alpha.test", "beta.test"],
                },
                enabled=True,
                priority=0,
            )
        )

    monkeypatch.setattr(
        registration_selection.EmailServiceFactory,
        "create",
        lambda service_type, config, name=None: FakeSelectableService(config),
    )

    with manager.session_scope() as session:
        first = registration_selection.resolve_email_service_for_registration(
            db=session,
            service_type=registration_selection.EmailServiceType.FREEMAIL,
            requested_service_id=1,
            fallback_config=None,
            proxy_url=None,
            selection=registration_selection.RegistrationSelectionRequest(
                selected_domains=["beta.test", "alpha.test"],
                selection_index=0,
            ),
        )
        second = registration_selection.resolve_email_service_for_registration(
            db=session,
            service_type=registration_selection.EmailServiceType.FREEMAIL,
            requested_service_id=1,
            fallback_config=None,
            proxy_url=None,
            selection=registration_selection.RegistrationSelectionRequest(
                selected_domains=["beta.test", "alpha.test"],
                selection_index=1,
            ),
        )

    assert first.config["domain"] == "beta.test"
    assert second.config["domain"] == "alpha.test"


def test_random_email_service_uses_random_choice(monkeypatch):
    manager = _build_db_manager("registration_random_service.db")
    with manager.session_scope() as session:
        session.add_all(
            [
                EmailService(
                    service_type="freemail",
                    name="Freemail A",
                    config={"base_url": "https://a.test", "admin_token": "token"},
                    enabled=True,
                    priority=0,
                ),
                EmailService(
                    service_type="freemail",
                    name="Freemail B",
                    config={"base_url": "https://b.test", "admin_token": "token"},
                    enabled=True,
                    priority=1,
                ),
            ]
        )

    monkeypatch.setattr(
        registration_selection.EmailServiceFactory,
        "create",
        lambda service_type, config, name=None: FakeSelectableService(config),
    )
    monkeypatch.setattr(registration_selection.random, "choice", lambda items: items[-1])

    with manager.session_scope() as session:
        resolved = registration_selection.resolve_email_service_for_registration(
            db=session,
            service_type=registration_selection.EmailServiceType.FREEMAIL,
            requested_service_id=None,
            fallback_config=None,
            proxy_url=None,
            selection=registration_selection.RegistrationSelectionRequest(
                random_email_service=True,
            ),
        )

    assert resolved.email_service_id == 2
    assert resolved.service_name == "Freemail B"


def test_random_outlook_account_prefers_unregistered_candidates(monkeypatch):
    manager = _build_db_manager("registration_random_outlook.db")
    with manager.session_scope() as session:
        session.add_all(
            [
                EmailService(
                    service_type="outlook",
                    name="first@outlook.com",
                    config={"email": "first@outlook.com", "password": "secret"},
                    enabled=True,
                    priority=0,
                ),
                EmailService(
                    service_type="outlook",
                    name="second@outlook.com",
                    config={"email": "second@outlook.com", "password": "secret"},
                    enabled=True,
                    priority=1,
                ),
                Account(
                    email="first@outlook.com",
                    email_service="outlook",
                    status="active",
                ),
            ]
        )

    monkeypatch.setattr(registration_selection.random, "choice", lambda items: items[0])

    with manager.session_scope() as session:
        resolved = registration_selection.resolve_email_service_for_registration(
            db=session,
            service_type=registration_selection.EmailServiceType.OUTLOOK,
            requested_service_id=None,
            fallback_config=None,
            proxy_url=None,
            selection=registration_selection.RegistrationSelectionRequest(
                random_outlook_account=True,
            ),
        )

    assert resolved.email_service_id == 2
    assert resolved.config["existing_email"] == "second@outlook.com"
