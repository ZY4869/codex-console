from src.core.grok import nsfw_service as nsfw_module
from src.core.grok import signup_client as signup_client_module
from src.core.grok import user_agreement_service as agreement_module
from src.core.grok.nsfw_service import NsfwService
from src.core.grok.signup_client import DEFAULT_STATE_TREE, discover_signup_bootstrap
from src.core.grok.user_agreement_service import UserAgreementService


class FakeResponse:
    def __init__(self, *, status_code=200, headers=None, content=b"", text=""):
        self.status_code = status_code
        self.headers = headers or {}
        self.content = content
        self.text = text

    def raise_for_status(self):
        if self.status_code >= 400:
            raise RuntimeError(f"HTTP {self.status_code}")


def test_discover_signup_bootstrap_extracts_action_id_from_scripts(monkeypatch):
    requests_seen = []
    signup_html = """
    <html>
        <body>
            <div data-sitekey="0x4AAAAAAAhr9JGVDZbrZOo0"></div>
            <script src="/_next/static/chunks/a.js"></script>
            <script src="/_next/static/chunks/b.js"></script>
        </body>
    </html>
    """
    script_payloads = {
        "https://accounts.x.ai/_next/static/chunks/a.js": "console.log('noop');",
        "https://accounts.x.ai/_next/static/chunks/b.js": (
            "const payload={emailValidationCode:'x',turnstileToken:'y',tosAcceptedVersion:'z'};"
            "const action='7f8c18544add07ec70d3b96137e2df4586def41ecd';"
        ),
    }

    class FakeSession:
        def __init__(self, *args, **kwargs):
            pass

        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc, tb):
            return False

        def get(self, url, timeout=30):
            requests_seen.append(url)
            if url == "https://accounts.x.ai/sign-up":
                return FakeResponse(text=signup_html)
            return FakeResponse(text=script_payloads[url])

    monkeypatch.setattr(signup_client_module.cffi_requests, "Session", lambda *args, **kwargs: FakeSession())

    bootstrap = discover_signup_bootstrap(signup_url="https://accounts.x.ai/sign-up")

    assert bootstrap.signup_url == "https://accounts.x.ai/sign-up"
    assert bootstrap.site_key == "0x4AAAAAAAhr9JGVDZbrZOo0"
    assert bootstrap.action_id == "7f8c18544add07ec70d3b96137e2df4586def41ecd"
    assert bootstrap.state_tree == DEFAULT_STATE_TREE
    assert requests_seen[0] == "https://accounts.x.ai/sign-up"


def test_discover_signup_bootstrap_retries_transient_failure(monkeypatch):
    attempts = {"count": 0}
    signup_html = """
    <html>
        <body>
            <div data-sitekey="0x4AAAAAAAhr9JGVDZbrZOo0"></div>
            <script src="/_next/static/chunks/a.js"></script>
        </body>
    </html>
    """

    class FakeSession:
        def __init__(self, *args, **kwargs):
            pass

        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc, tb):
            return False

        def get(self, url, timeout=30):
            attempts["count"] += 1
            if attempts["count"] == 1:
                raise RuntimeError("temporary connect fail")
            if url == "https://accounts.x.ai/sign-up":
                return FakeResponse(text=signup_html)
            return FakeResponse(text="emailValidationCode turnstileToken tosAcceptedVersion 7f8c18544add07ec70d3b96137e2df4586def41ecd")

    monkeypatch.setattr(signup_client_module.cffi_requests, "Session", lambda *args, **kwargs: FakeSession())
    monkeypatch.setattr(signup_client_module.time, "sleep", lambda _: None)

    bootstrap = discover_signup_bootstrap(signup_url="https://accounts.x.ai/sign-up")

    assert bootstrap.action_id == "7f8c18544add07ec70d3b96137e2df4586def41ecd"
    assert attempts["count"] >= 2


def test_user_agreement_service_uses_default_grpc_endpoint(monkeypatch):
    captured = {}

    def fake_post(url, **kwargs):
        captured["url"] = url
        captured["kwargs"] = kwargs
        return FakeResponse(status_code=200, headers={"grpc-status": "0"}, content=b"\x01")

    monkeypatch.setattr(agreement_module.cffi_requests, "post", fake_post)

    result = UserAgreementService().accept(
        task_config={},
        sso_token="sso-main",
        sso_rw_token="sso-rw",
        impersonate="chrome120",
        user_agent="ua/test",
        proxy_url="http://127.0.0.1:8888",
    )

    assert result["success"] is True
    assert captured["url"] == "https://accounts.x.ai/auth_mgmt.AuthManagement/SetTosAcceptedVersion"
    assert captured["kwargs"]["cookies"]["sso"] == "sso-main"
    assert captured["kwargs"]["cookies"]["sso-rw"] == "sso-rw"
    assert captured["kwargs"]["headers"]["content-type"] == "application/grpc-web+proto"
    assert captured["kwargs"]["headers"]["user-agent"] == "ua/test"
    assert captured["kwargs"]["data"] == b"\x00\x00\x00\x00\x02\x10\x01"
    assert captured["kwargs"]["proxies"] == {"http": "http://127.0.0.1:8888", "https": "http://127.0.0.1:8888"}


def test_nsfw_service_uses_default_grpc_endpoint(monkeypatch):
    calls = []

    def fake_post(url, **kwargs):
        calls.append((url, kwargs))
        if len(calls) == 1:
            return FakeResponse(status_code=200, headers={"grpc-status": "0"}, content=b"\x01")
        return FakeResponse(status_code=200, headers={}, content=b"\x02")

    monkeypatch.setattr(nsfw_module.cffi_requests, "post", fake_post)

    result = NsfwService().enable(
        task_config={},
        sso_token="sso-main",
        sso_rw_token="sso-rw",
        impersonate="chrome120",
        user_agent="ua/test",
        proxy_url="http://127.0.0.1:8888",
        extra_cookies={"cf_clearance": "cookie-value"},
    )

    assert result["success"] is True
    assert len(calls) == 2
    assert calls[0][0] == "https://grok.com/auth_mgmt.AuthManagement/UpdateUserFeatureControls"
    assert calls[0][1]["cookies"]["cf_clearance"] == "cookie-value"
    assert calls[0][1]["cookies"]["sso"] == "sso-main"
    assert calls[1][0] == "https://grok.com/auth_mgmt.AuthManagement/UpdateUserFeatureControls"
    assert "cf_clearance=cookie-value" in calls[1][1]["headers"]["cookie"]
    assert "sso=sso-main" in calls[1][1]["headers"]["cookie"]
