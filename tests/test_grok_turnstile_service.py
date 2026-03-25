import pytest

from src.core.grok import turnstile_local_solver as local_solver_module
from src.core.grok import turnstile_service as turnstile_module
from src.core.grok.turnstile_service import TurnstileService, TurnstileServiceError


class FakeResponse:
    def __init__(self, *, status_code=200, payload=None, text=""):
        self.status_code = status_code
        self._payload = payload if payload is not None else {}
        self.text = text

    def json(self):
        return self._payload


def test_turnstile_service_local_supports_task_result_flow(monkeypatch):
    calls = {"get": []}
    poll_count = {"value": 0}
    monkeypatch.setattr(
        local_solver_module,
        "inspect_local_solver_service",
        lambda url: {"healthy": True, "placeholder": False, "url": url},
    )

    def fake_get(url, **kwargs):
        calls["get"].append((url, kwargs))
        if url.endswith("/turnstile"):
            return FakeResponse(payload={"taskId": "task-123"})
        poll_count["value"] += 1
        if poll_count["value"] == 1:
            return FakeResponse(payload={"status": "processing"})
        return FakeResponse(payload={"solution": {"token": "task-token"}})

    monkeypatch.setattr(local_solver_module.cffi_requests, "get", fake_get)
    monkeypatch.setattr(local_solver_module.time, "sleep", lambda _: None)

    token = TurnstileService().solve(
        mode="local",
        sitekey="site-key",
        page_url="https://accounts.x.ai/sign-up",
        action="signup",
        solver_url="http://127.0.0.1:5072",
    )

    assert token == "task-token"
    assert calls["get"][0][0] == "http://127.0.0.1:5072/turnstile"
    assert calls["get"][0][1]["params"] == {
        "sitekey": "site-key",
        "url": "https://accounts.x.ai/sign-up",
        "action": "signup",
    }
    assert calls["get"][1][0] == "http://127.0.0.1:5072/result"
    assert calls["get"][1][1]["params"] == {"id": "task-123"}


def test_turnstile_service_local_falls_back_to_direct_post(monkeypatch):
    calls = {"get": 0, "post": 0}
    monkeypatch.setattr(
        local_solver_module,
        "inspect_local_solver_service",
        lambda url: {"healthy": True, "placeholder": False, "url": url},
    )

    def fake_get(url, **kwargs):
        calls["get"] += 1
        return FakeResponse(status_code=404, payload={"detail": "not found"})

    def fake_post(url, **kwargs):
        calls["post"] += 1
        assert url == "http://127.0.0.1:5072/turnstile"
        assert '"sitekey": "site-key"' in kwargs["data"]
        assert kwargs["headers"]["content-type"] == "application/json"
        return FakeResponse(payload={"token": "direct-token"})

    monkeypatch.setattr(local_solver_module.cffi_requests, "get", fake_get)
    monkeypatch.setattr(local_solver_module.cffi_requests, "post", fake_post)

    token = TurnstileService().solve(
        mode="local",
        sitekey="site-key",
        page_url="https://accounts.x.ai/sign-up",
        solver_url="http://127.0.0.1:5072",
    )

    assert token == "direct-token"
    assert calls["get"] == 1
    assert calls["post"] == 1


def test_turnstile_service_local_422_has_solver_hint(monkeypatch):
    monkeypatch.setattr(
        local_solver_module,
        "inspect_local_solver_service",
        lambda url: {"healthy": True, "placeholder": False, "url": url},
    )

    def fake_get(url, **kwargs):
        return FakeResponse(status_code=404, payload={"detail": "not found"})

    def fake_post(url, **kwargs):
        return FakeResponse(status_code=422, payload={"detail": "missing url query"})

    monkeypatch.setattr(local_solver_module.cffi_requests, "get", fake_get)
    monkeypatch.setattr(local_solver_module.cffi_requests, "post", fake_post)

    with pytest.raises(TurnstileServiceError) as exc_info:
        TurnstileService().solve(
            mode="local",
            sitekey="site-key",
            page_url="https://accounts.x.ai/sign-up",
            solver_url="http://127.0.0.1:5072",
        )

    message = str(exc_info.value)
    assert "HTTP 422" in message
    assert "solver API may not match" in message
    assert "POST /turnstile" in message


def test_turnstile_service_local_rejects_placeholder_solver(monkeypatch):
    monkeypatch.setattr(
        local_solver_module,
        "inspect_local_solver_service",
        lambda url: {"healthy": False, "placeholder": True, "url": url},
    )

    with pytest.raises(TurnstileServiceError) as exc_info:
        TurnstileService().solve(
            mode="local",
            sitekey="site-key",
            page_url="https://accounts.x.ai/sign-up",
            solver_url="http://127.0.0.1:5072",
        )

    assert "placeholder" in str(exc_info.value).lower()
