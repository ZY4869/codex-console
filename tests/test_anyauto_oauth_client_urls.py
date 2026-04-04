from src.core.anyauto import oauth_client as oauth_client_module
from src.core.anyauto.oauth_client import OAuthClient


class DummyResponse:
    def __init__(self, url, payload=None, status_code=200, text=""):
        self.url = url
        self._payload = payload or {"page": {"type": "login_password"}}
        self.status_code = status_code
        self.text = text
        self.headers = {}

    def json(self):
        return self._payload


class RecordingSession:
    def __init__(self):
        self.headers = {}
        self.cookies = []
        self.post_calls = []

    def post(self, url, **kwargs):
        self.post_calls.append({"url": url, "kwargs": kwargs})
        return DummyResponse(url=url)


def test_oauth_client_normalizes_authorize_url_to_issuer_base():
    client = OAuthClient(
        {
            "oauth_issuer": "https://auth.openai.com/oauth/authorize",
            "oauth_client_id": "client-id",
            "oauth_redirect_uri": "http://localhost:1455/auth/callback",
        },
        verbose=False,
    )

    assert client.oauth_issuer == "https://auth.openai.com"


def test_submit_authorize_continue_uses_normalized_absolute_api_url(monkeypatch):
    monkeypatch.setattr(oauth_client_module, "build_sentinel_token", lambda *args, **kwargs: "sentinel-token")
    monkeypatch.setattr(oauth_client_module, "generate_datadog_trace", lambda: {})

    client = OAuthClient(
        {
            "oauth_issuer": "https://auth.openai.com/oauth/authorize",
            "oauth_client_id": "client-id",
            "oauth_redirect_uri": "http://localhost:1455/auth/callback",
        },
        verbose=False,
    )
    client.session = RecordingSession()

    state = client._submit_authorize_continue(
        email="tester@example.com",
        device_id="device-1",
        continue_referer="https://auth.openai.com/log-in",
    )

    assert state is not None
    assert client.session.post_calls[0]["url"] == "https://auth.openai.com/api/accounts/authorize/continue"
