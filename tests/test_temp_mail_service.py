from src.services.temp_mail import TempMailService


class FakeResponse:
    def __init__(self, status_code=200, payload=None, text=""):
        self.status_code = status_code
        self._payload = payload
        self.text = text

    def json(self):
        return self._payload


class FakeHTTPClient:
    def __init__(self, responses):
        self.responses = list(responses)
        self.calls = []

    def request(self, method, url, **kwargs):
        self.calls.append({
            "method": method,
            "url": url,
            "kwargs": kwargs,
        })
        if not self.responses:
            raise AssertionError(f"未准备响应: {method} {url}")
        return self.responses.pop(0)


def test_create_email_honors_requested_name_and_domain():
    service = TempMailService({
        "base_url": "https://mail.example.com",
        "admin_password": "secret",
        "domain": "kan69.fun",
        "enable_prefix": True,
    })
    fake_client = FakeHTTPClient([
        FakeResponse(
            payload={
                "address": "fixeduser@kan69.fun",
                "jwt": "jwt-123",
            }
        ),
    ])
    service.http_client = fake_client

    email_info = service.create_email({"name": "fixeduser", "domain": "kan69.fun"})

    assert email_info["email"] == "fixeduser@kan69.fun"
    assert email_info["service_id"] == "fixeduser@kan69.fun"
    request = fake_client.calls[0]
    assert request["method"] == "POST"
    assert request["kwargs"]["json"] == {
        "enablePrefix": True,
        "name": "fixeduser",
        "domain": "kan69.fun",
    }
