import importlib
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from src.config import settings as settings_module
from src.database import session as db_session


@pytest.fixture()
def app_module(tmp_path, monkeypatch):
    db_url = f"sqlite:///{tmp_path / 'sora-removal.db'}"
    monkeypatch.setenv("APP_DATABASE_URL", db_url)
    monkeypatch.setenv("APP_DATA_DIR", str(tmp_path))
    db_session._db_manager = None
    settings_module._settings = None

    module = importlib.import_module("src.web.app")
    module = importlib.reload(module)

    yield module

    db_session._db_manager = None
    settings_module._settings = None


def test_sora_page_route_returns_404(app_module):
    with TestClient(app_module.app) as client:
        response = client.get("/sora/invite")

    assert response.status_code == 404


def test_sora_api_routes_return_404(app_module):
    with TestClient(app_module.app) as client:
        response = client.get("/api/sora-invite/overview")

    assert response.status_code == 404


def test_main_navigation_templates_do_not_link_to_sora_invite():
    templates = [
        Path("templates/index.html"),
        Path("templates/accounts.html"),
        Path("templates/email_services.html"),
        Path("templates/payment.html"),
        Path("templates/settings.html"),
        Path("templates/team.html"),
        Path("templates/team_invite.html"),
    ]

    for template_path in templates:
        template = template_path.read_text(encoding="utf-8")
        assert "/sora/invite" not in template, f"{template_path} still links to the removed Sora invite page"
