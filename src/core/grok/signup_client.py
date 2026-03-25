"""
Grok 注册协议客户端。
"""

from __future__ import annotations

import base64
import random
import re
import struct
import time
from dataclasses import dataclass
from typing import Dict, Iterable, Optional
from urllib.parse import urljoin, urlsplit

from bs4 import BeautifulSoup
from curl_cffi import CurlHttpVersion, requests as cffi_requests

DEFAULT_IMPERSONATE = "chrome120"
# 强制 HTTP/1.1，规避 nghttp2 HPACK COMPRESSION_ERROR (error 9)
FORCED_HTTP_VERSION = CurlHttpVersion.V1_1
DEFAULT_SITE_URL = "https://accounts.x.ai"
DEFAULT_SIGNUP_URL = f"{DEFAULT_SITE_URL}/sign-up"
DEFAULT_SIGNUP_REFERER = f"{DEFAULT_SIGNUP_URL}?redirect=grok-com"
DEFAULT_TURNSTILE_SITE_KEY = "0x4AAAAAAAhr9JGVDZbrZOo0"
DEFAULT_STATE_TREE = (
    "%5B%22%22%2C%7B%22children%22%3A%5B%22(app)%22%2C%7B%22children%22%3A%5B%22(auth)%22%2C"
    "%7B%22children%22%3A%5B%22sign-up%22%2C%7B%22children%22%3A%5B%22__PAGE__%22%2C%7B%7D%2C"
    "%22%2Fsign-up%22%2C%22refresh%22%5D%7D%5D%7D%2Cnull%2Cnull%5D%7D%2Cnull%2Cnull%5D%7D%2C"
    "null%2Cnull%2Ctrue%5D"
)
ACTION_ID_PATTERN = re.compile(r"\b7f[a-fA-F0-9]{20,}\b")
SITE_KEY_PATTERN = re.compile(r"0x4[A-Za-z0-9_-]+")
SET_COOKIE_URL_PATTERN = re.compile(r'(https://[^",\s]+set-cookie\?q=\S+?)(?:\d+:|")')
SCRIPT_HINTS = ("emailValidationCode", "turnstileToken", "tosAcceptedVersion")

CHROME_PROFILES = (
    ("chrome110", "110.0.0.0", "chrome"),
    ("chrome119", "119.0.0.0", "chrome"),
    ("chrome120", "120.0.0.0", "chrome"),
    ("edge99", "99.0.1150.36", "edge"),
    ("edge101", "101.0.1210.47", "edge"),
)
NETWORK_RETRY_ATTEMPTS = 3
NETWORK_RETRY_DELAY_SECONDS = 1.0


class GrokSignupProtocolError(RuntimeError):
    pass


@dataclass(frozen=True)
class BrowserProfile:
    impersonate: str
    user_agent: str


@dataclass(frozen=True)
class GrokSignupBootstrap:
    site_url: str
    signup_url: str
    site_key: str
    state_tree: str
    action_id: str


@dataclass(frozen=True)
class GrokSignupResult:
    sso_token: str
    sso_rw_token: str
    grok_sso_token: str
    grok_sso_rw_token: str
    grok_cookies: Dict[str, str]
    impersonate: str
    user_agent: str


def build_proxy_mapping(proxy_url: Optional[str]) -> Dict[str, str]:
    value = str(proxy_url or "").strip()
    if not value:
        return {}
    return {"http": value, "https": value}


def get_random_browser_profile() -> BrowserProfile:
    impersonate, version, brand = random.choice(CHROME_PROFILES)
    chrome_major = version.split(".")[0]
    chrome_version = f"{chrome_major}.0.0.0"
    if brand == "edge":
        user_agent = (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
            "AppleWebKit/537.36 (KHTML, like Gecko) "
            f"Chrome/{chrome_version} Safari/537.36 Edg/{version}"
        )
    else:
        user_agent = (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
            "AppleWebKit/537.36 (KHTML, like Gecko) "
            f"Chrome/{version} Safari/537.36"
        )
    return BrowserProfile(impersonate=impersonate, user_agent=user_agent)


def encode_grpc_string(field_id: int, value: str) -> bytes:
    key = (field_id << 3) | 2
    payload = struct.pack("B", key) + struct.pack("B", len(value.encode("utf-8"))) + value.encode("utf-8")
    return b"\x00" + struct.pack(">I", len(payload)) + payload


def encode_grpc_verify_code(email: str, code: str) -> bytes:
    payload = (
        struct.pack("B", (1 << 3) | 2)
        + struct.pack("B", len(email.encode("utf-8")))
        + email.encode("utf-8")
        + struct.pack("B", (2 << 3) | 2)
        + struct.pack("B", len(code.encode("utf-8")))
        + code.encode("utf-8")
    )
    return b"\x00" + struct.pack(">I", len(payload)) + payload


def normalize_email_validation_code(code: str) -> str:
    return re.sub(r"[^A-Za-z0-9]", "", str(code or "")).upper()


def discover_signup_bootstrap(proxy_url: Optional[str] = None, signup_url: str = DEFAULT_SIGNUP_URL) -> GrokSignupBootstrap:
    proxies = build_proxy_mapping(proxy_url)
    with cffi_requests.Session(impersonate=DEFAULT_IMPERSONATE, proxies=proxies) as session:
        response = _request_with_retry(lambda: session.get(signup_url, timeout=30, http_version=FORCED_HTTP_VERSION))
        response.raise_for_status()
        html = response.text

        site_key = _extract_site_key(html) or DEFAULT_TURNSTILE_SITE_KEY
        action_id = _extract_action_id(session, signup_url, html)
        state_tree = _extract_state_tree(html) or DEFAULT_STATE_TREE
        split = urlsplit(signup_url)
        site_url = f"{split.scheme}://{split.netloc}"
        return GrokSignupBootstrap(
            site_url=site_url,
            signup_url=signup_url,
            site_key=site_key,
            state_tree=state_tree,
            action_id=action_id,
        )


def extract_set_cookie_url(payload: str) -> Optional[str]:
    match = SET_COOKIE_URL_PATTERN.search(payload or "")
    return match.group(1) if match else None


def follow_set_cookie_chain(first_url: str, impersonate: str, proxies: Dict[str, str]) -> Dict[str, str]:
    cookies: Dict[str, str] = {}
    with cffi_requests.Session(impersonate=impersonate, proxies=proxies, http_version=FORCED_HTTP_VERSION) as session:
        _visit_cookie_url(session, first_url, cookies)
        jwt_token = first_url.split("set-cookie?q=", 1)[1] if "set-cookie?q=" in first_url else ""
        for nested_url in _extract_nested_set_cookie_urls(jwt_token):
            _visit_cookie_url(session, nested_url, cookies)

        grok_cookies = {}
        for item in session.cookies.jar:
            if item.domain and "grok.com" in item.domain:
                grok_cookies[item.name] = item.value
        cookies["_grok_all"] = grok_cookies
    return cookies


class GrokSignupClient:
    def __init__(self, *, proxy_url: Optional[str], bootstrap: GrokSignupBootstrap):
        self.bootstrap = bootstrap
        self.proxies = build_proxy_mapping(proxy_url)
        profile = get_random_browser_profile()
        self.impersonate = profile.impersonate
        self.user_agent = profile.user_agent
        self.session = cffi_requests.Session(impersonate=self.impersonate, proxies=self.proxies, http_version=FORCED_HTTP_VERSION)
        try:
            _request_with_retry(lambda: self.session.get(self.bootstrap.site_url, timeout=10), attempts=2, delay_seconds=0.5)
        except Exception:
            pass

    def close(self):
        self.session.close()

    def send_verification_code(self, email: str) -> Dict[str, str]:
        response = _request_with_retry(
            lambda: self.session.post(
                f"{self.bootstrap.site_url}/auth_mgmt.AuthManagement/CreateEmailValidationCode",
                data=encode_grpc_string(1, email),
                headers=self._grpc_headers(),
                timeout=15,
            )
        )
        self._raise_grpc_error(response, "send verification code")
        return {"status": "sent"}

    def verify_code(self, email: str, code: str) -> Dict[str, str]:
        normalized_code = normalize_email_validation_code(code)
        response = _request_with_retry(
            lambda: self.session.post(
                f"{self.bootstrap.site_url}/auth_mgmt.AuthManagement/VerifyEmailValidationCode",
                data=encode_grpc_verify_code(email, normalized_code),
                headers=self._grpc_headers(),
                timeout=15,
            )
        )
        self._raise_grpc_error(response, "verify email code")
        return {"status": "verified"}

    def sign_up(self, email: str, code: str, captcha_token: str) -> GrokSignupResult:
        normalized_code = normalize_email_validation_code(code)
        headers = {
            "accept": "text/x-component",
            "content-type": "text/plain;charset=UTF-8",
            "origin": self.bootstrap.site_url,
            "referer": self.bootstrap.signup_url,
            "user-agent": self.user_agent,
            "next-router-state-tree": self.bootstrap.state_tree,
            "next-action": self.bootstrap.action_id,
        }
        cf_cookie = self.session.cookies.get("__cf_bm", "")
        if cf_cookie:
            headers["cookie"] = f"__cf_bm={cf_cookie}"

        payload = [
            {
                "emailValidationCode": normalized_code,
                "createUserAndSessionRequest": {
                    "email": email,
                    "givenName": _random_name(),
                    "familyName": _random_name(),
                    "clearTextPassword": _random_password(),
                    "tosAcceptedVersion": "$undefined",
                },
                "turnstileToken": captcha_token,
                "promptOnDuplicateEmail": True,
            }
        ]
        response = _request_with_retry(
            lambda: self.session.post(self.bootstrap.signup_url, json=payload, headers=headers, timeout=60),
            delay_seconds=2.0,
        )
        if response.status_code != 200:
            raise GrokSignupProtocolError(f"sign-up failed with HTTP {response.status_code}.")

        set_cookie_url = extract_set_cookie_url(response.text)
        if not set_cookie_url:
            raise GrokSignupProtocolError("sign-up succeeded but set-cookie URL was not found.")

        cookies = follow_set_cookie_chain(set_cookie_url, self.impersonate, self.proxies)
        sso_token = str(cookies.get("sso") or "").strip()
        if not sso_token:
            raise GrokSignupProtocolError("sign-up succeeded but SSO token was not found.")
        sso_rw_token = str(cookies.get("sso-rw") or sso_token).strip()
        grok_cookies = dict(cookies.get("_grok_all") or {})
        return GrokSignupResult(
            sso_token=sso_token,
            sso_rw_token=sso_rw_token,
            grok_sso_token=str(cookies.get("sso@.grok.com") or sso_token).strip(),
            grok_sso_rw_token=str(cookies.get("sso-rw@.grok.com") or cookies.get("sso@.grok.com") or sso_token).strip(),
            grok_cookies=grok_cookies,
            impersonate=self.impersonate,
            user_agent=self.user_agent,
        )

    def _grpc_headers(self) -> Dict[str, str]:
        return {
            "content-type": "application/grpc-web+proto",
            "x-grpc-web": "1",
            "x-user-agent": "connect-es/2.1.1",
            "origin": self.bootstrap.site_url,
            "referer": f"{self.bootstrap.signup_url}?redirect=grok-com",
        }

    @staticmethod
    def _raise_grpc_error(response, action: str):
        grpc_status = response.headers.get("grpc-status")
        if response.status_code == 200 and grpc_status in (None, "0"):
            return
        detail = grpc_status or response.text[:200]
        raise GrokSignupProtocolError(f"Failed to {action}: HTTP {response.status_code}, grpc={detail}.")


def _extract_site_key(html: str) -> Optional[str]:
    match = SITE_KEY_PATTERN.search(html or "")
    return match.group(0) if match else None


def _extract_state_tree(html: str) -> Optional[str]:
    match = re.search(r'next-router-state-tree":"([^"]+)"', html or "")
    return match.group(1) if match else None


def _extract_action_id(session, signup_url: str, html: str) -> str:
    direct_match = ACTION_ID_PATTERN.search(html or "")
    if direct_match:
        return direct_match.group(0)

    script_urls = _get_signup_script_urls(signup_url, html)
    prioritized = []
    fallback = []
    for script_url in script_urls:
        content = _request_with_retry(lambda url=script_url: session.get(url, timeout=30, http_version=FORCED_HTTP_VERSION)).text
        if all(hint in content for hint in SCRIPT_HINTS):
            prioritized.append(content)
        else:
            fallback.append(content)

    for content in prioritized + fallback:
        match = ACTION_ID_PATTERN.search(content)
        if match:
            return match.group(0)
    raise GrokSignupProtocolError("Failed to discover sign-up action id from accounts.x.ai.")


def _get_signup_script_urls(signup_url: str, html: str) -> Iterable[str]:
    soup = BeautifulSoup(html, "html.parser")
    return [urljoin(signup_url, script["src"]) for script in soup.find_all("script", src=True) if "_next/static" in script["src"]]


def _visit_cookie_url(session, url: str, cookies: Dict[str, str]):
    try:
        _request_with_retry(lambda: session.get(url, allow_redirects=True, timeout=30, http_version=FORCED_HTTP_VERSION), attempts=2)
    except Exception:
        return
    for item in session.cookies.jar:
        if item.name in {"sso", "sso-rw"}:
            cookies[f"{item.name}@{item.domain}"] = item.value
            cookies[item.name] = item.value


def _extract_nested_set_cookie_urls(jwt_token: str):
    current = jwt_token
    for _ in range(5):
        if not current:
            return
        try:
            payload = _decode_jwt_payload(current)
        except Exception:
            return
        success_url = str((payload.get("config") or {}).get("success_url") or "")
        if "set-cookie?q=" not in success_url:
            return
        yield success_url
        current = success_url.split("set-cookie?q=", 1)[1]


def _decode_jwt_payload(jwt_token: str) -> Dict[str, object]:
    payload = jwt_token.split(".")[1]
    padding = "=" * (-len(payload) % 4)
    decoded = base64.urlsafe_b64decode(payload + padding)
    import json

    return json.loads(decoded)


def _random_name() -> str:
    letters = "abcdefghijklmnopqrstuvwxyz"
    length = random.randint(4, 6)
    return random.choice(letters).upper() + "".join(random.choice(letters) for _ in range(length - 1))


def _random_password(length: int = 15) -> str:
    alphabet = "abcdefghijklmnopqrstuvwxyz0123456789"
    return "".join(random.choice(alphabet) for _ in range(length))


def _request_with_retry(callback, *, attempts: int = NETWORK_RETRY_ATTEMPTS, delay_seconds: float = NETWORK_RETRY_DELAY_SECONDS):
    last_error = None
    for attempt in range(1, max(1, attempts) + 1):
        try:
            return callback()
        except Exception as exc:  # noqa: PERF203
            last_error = exc
            if attempt >= attempts:
                raise
            time.sleep(delay_seconds)
    if last_error:
        raise last_error
    raise RuntimeError("request retry exhausted")
