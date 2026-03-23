"""
OpenAI Team 邀请与成员会话相关能力。
"""

import base64
import json
import logging
import re
from datetime import datetime
from typing import Any, Dict, List, Optional, Tuple

from curl_cffi import requests as cffi_requests
import requests as std_requests

from ...database.models import Account
from .account_sensitive_info import SENSITIVE_SESSION_PAYLOAD_KEY

logger = logging.getLogger(__name__)

BASE_URL = "https://chatgpt.com"
BACKEND_API_BASE = f"{BASE_URL}/backend-api"
ACCOUNT_CHECK_URL = f"{BACKEND_API_BASE}/accounts/check/v4-2023-04-27"
DEFAULT_BROWSER_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/120.0.0.0 Safari/537.36"
)
SESSION_COOKIE_NAMES = (
    "__Secure-next-auth.session-token",
    "next-auth.session-token",
    "__Secure-authjs.session-token",
    "authjs.session-token",
)
PROXY_SESSION_RETRY_MARKERS = (
    "TLS connect error",
    "OPENSSL_internal:invalid library",
    "Proxy CONNECT",
    "CONNECT tunnel failed",
    "SSL connect error",
)


def _build_proxies(proxy: Optional[str]) -> Optional[dict]:
    if proxy:
        return {"http": proxy, "https": proxy}
    return None


def _should_retry_with_proxy_session(exc: Exception) -> bool:
    message = str(exc or "")
    return any(marker in message for marker in PROXY_SESSION_RETRY_MARKERS)


def _request_with_proxy_fallback(
    method: str,
    url: str,
    *,
    proxy: Optional[str] = None,
    **kwargs,
):
    def _stdlib_request(*, proxy_url: Optional[str], use_env_proxy: bool):
        stdlib_kwargs = dict(kwargs)
        stdlib_kwargs.pop("impersonate", None)
        stdlib_kwargs.setdefault("timeout", 30)
        stdlib_kwargs.setdefault("allow_redirects", True)
        headers = dict(stdlib_kwargs.get("headers") or {})
        headers.setdefault("User-Agent", DEFAULT_BROWSER_UA)
        stdlib_kwargs["headers"] = headers

        session = std_requests.Session()
        session.trust_env = use_env_proxy
        if proxy_url:
            session.proxies.update(_build_proxies(proxy_url) or {})
        try:
            return session.request(method.upper(), url, **stdlib_kwargs)
        finally:
            session.close()

    request_callable = getattr(cffi_requests, method.lower(), None)
    if request_callable is None:
        raise ValueError(f"Unsupported method: {method}")

    primary_kwargs = dict(kwargs)
    primary_kwargs.setdefault("timeout", 30)
    primary_kwargs.setdefault("impersonate", "chrome110")
    if proxy:
        primary_kwargs.setdefault("proxies", _build_proxies(proxy))

    try:
        return request_callable(url, **primary_kwargs)
    except Exception as exc:
        if not proxy or not _should_retry_with_proxy_session(exc):
            raise

        logger.warning(
            "Team API request hit proxy TLS/connect error, retrying with session proxy mode: %s %s - %s",
            method.upper(),
            url,
            exc,
        )

        session_kwargs = dict(kwargs)
        session_kwargs.setdefault("timeout", 30)
        session = cffi_requests.Session(impersonate="chrome110", proxy=proxy)
        try:
            return session.request(method.upper(), url, **session_kwargs)
        except Exception as session_exc:
            if not _should_retry_with_proxy_session(session_exc):
                raise

            logger.warning(
                "Team API session proxy mode also failed, retrying with stdlib requests: %s %s - %s",
                method.upper(),
                url,
                session_exc,
            )
            try:
                return _stdlib_request(proxy_url=proxy, use_env_proxy=False)
            except Exception as stdlib_exc:
                if proxy:
                    logger.warning(
                        "Team API stdlib proxy mode also failed; explicit proxy is locked, so direct fallback is disabled: %s %s - %s",
                        method.upper(),
                        url,
                        stdlib_exc,
                    )
                    raise
                logger.warning(
                    "Team API stdlib proxy mode also failed, retrying direct with trust_env=False: %s %s - %s",
                    method.upper(),
                    url,
                    stdlib_exc,
                )
                return _stdlib_request(proxy_url=None, use_env_proxy=False)
        finally:
            try:
                session.close()
            except Exception:
                pass


def _build_bearer_headers(access_token: str, team_account_id: Optional[str] = None) -> Dict[str, str]:
    headers = {
        "Authorization": f"Bearer {access_token}",
        "Accept": "application/json",
    }
    if team_account_id:
        headers["chatgpt-account-id"] = team_account_id
    return headers


def _parse_cookie_string(cookie_string: str) -> List[Dict[str, str]]:
    cookies: List[Dict[str, str]] = []
    for part in (cookie_string or "").split(";"):
        part = part.strip()
        if "=" not in part:
            continue
        name, _, value = part.partition("=")
        cookies.append(
            {
                "name": name.strip(),
                "value": value.strip(),
                "domain": ".chatgpt.com",
                "path": "/",
            }
        )
    return cookies


def iter_cookie_name_value_pairs(cookie_store: Any) -> List[Tuple[str, str]]:
    pairs: List[Tuple[str, str]] = []
    seen = set()
    if not cookie_store:
        return pairs

    try:
        raw_items = list(cookie_store.items())
    except Exception:
        raw_items = []

    for item in raw_items:
        try:
            name, value = item
        except Exception:
            continue
        normalized = (str(name or "").strip(), str(value or "").strip())
        if not normalized[0] or not normalized[1] or normalized in seen:
            continue
        seen.add(normalized)
        pairs.append(normalized)

    jar = getattr(cookie_store, "jar", None)
    if jar is None and hasattr(cookie_store, "__iter__"):
        jar = cookie_store

    if jar is not None:
        try:
            for cookie in jar:
                normalized = (
                    str(getattr(cookie, "name", "") or "").strip(),
                    str(getattr(cookie, "value", "") or "").strip(),
                )
                if not normalized[0] or not normalized[1] or normalized in seen:
                    continue
                seen.add(normalized)
                pairs.append(normalized)
        except Exception:
            pass

    return pairs


def list_cookie_names(cookie_store: Any) -> List[str]:
    names: List[str] = []
    seen = set()
    for name, _ in iter_cookie_name_value_pairs(cookie_store):
        if name in seen:
            continue
        seen.add(name)
        names.append(name)
    return names


def serialize_cookie_store(cookie_store: Any) -> str:
    return "; ".join(f"{name}={value}" for name, value in iter_cookie_name_value_pairs(cookie_store))


def _resolve_session_cookie_from_mapping(cookie_mapping: Dict[str, str]) -> Optional[Dict[str, Any]]:
    for cookie_name in SESSION_COOKIE_NAMES:
        direct_value = str(cookie_mapping.get(cookie_name) or "").strip()
        if direct_value:
            return {"name": cookie_name, "value": direct_value, "chunked": False}

        chunk_prefix = f"{cookie_name}."
        chunks: List[Tuple[int, str]] = []
        for name, value in cookie_mapping.items():
            if not name.startswith(chunk_prefix):
                continue
            suffix = name[len(chunk_prefix):]
            if suffix.isdigit() and value:
                chunks.append((int(suffix), str(value)))
        if chunks:
            chunks.sort(key=lambda item: item[0])
            return {
                "name": cookie_name,
                "value": "".join(chunk for _, chunk in chunks),
                "chunked": True,
            }
    return None


def resolve_session_cookie_from_cookie_store(cookie_store: Any) -> Optional[Dict[str, Any]]:
    return _resolve_session_cookie_from_mapping(dict(iter_cookie_name_value_pairs(cookie_store)))


def resolve_session_cookie(account: Account) -> Optional[Dict[str, Any]]:
    cookie_mapping = {
        cookie["name"]: cookie["value"]
        for cookie in _parse_cookie_string(account.cookies or "")
        if cookie["name"] and cookie["value"]
    }
    cookie_match = _resolve_session_cookie_from_mapping(cookie_mapping)
    if cookie_match:
        return cookie_match

    direct_token = str(account.session_token or "").strip()
    if direct_token:
        return {"name": SESSION_COOKIE_NAMES[0], "value": direct_token, "chunked": False}

    extra_data = account.extra_data if isinstance(account.extra_data, dict) else {}
    sensitive_payload = extra_data.get(SENSITIVE_SESSION_PAYLOAD_KEY)
    if isinstance(sensitive_payload, dict):
        payload_token = str(sensitive_payload.get("sessionToken") or "").strip()
        if payload_token:
            return {"name": SESSION_COOKIE_NAMES[0], "value": payload_token, "chunked": False}

    return None


def resolve_session_token(account: Account) -> Optional[str]:
    session_cookie = resolve_session_cookie(account)
    if session_cookie:
        return str(session_cookie["value"])
    return None


def build_session_cookie_header(session_token: str, cookie_name: Optional[str] = None) -> str:
    return f"{cookie_name or SESSION_COOKIE_NAMES[0]}={session_token}"


def _decode_jwt_payload(token: Optional[str]) -> Dict[str, Any]:
    raw = (token or "").strip()
    if raw.count(".") < 2:
        return {}

    payload_segment = raw.split(".")[1]
    pad = "=" * ((4 - (len(payload_segment) % 4)) % 4)
    try:
        payload = base64.urlsafe_b64decode((payload_segment + pad).encode("ascii"))
        return json.loads(payload.decode("utf-8"))
    except Exception:
        return {}


def discover_team_account(admin_account: Account, proxy: Optional[str] = None) -> Dict[str, Any]:
    """发现主账号当前可用的 Team account。"""
    if not admin_account.access_token:
        return {"success": False, "error": "主账号缺少 access_token"}

    try:
        response = _request_with_proxy_fallback(
            "GET",
            ACCOUNT_CHECK_URL,
            headers=_build_bearer_headers(admin_account.access_token),
            proxy=proxy,
        )
        response.raise_for_status()
        data = response.json()
    except Exception as exc:
        return {"success": False, "error": f"查询 Team account 失败: {exc}"}

    accounts = data.get("accounts", {}) or {}
    team_accounts: List[Dict[str, Any]] = []
    for account_id, info in accounts.items():
        account = info.get("account", {}) or {}
        entitlement = info.get("entitlement", {}) or {}
        if account.get("plan_type") != "team":
            continue
        team_accounts.append(
            {
                "team_account_id": str(account_id),
                "team_workspace_id": account.get("workspace_id") or account.get("id"),
                "name": account.get("name") or "",
                "role": account.get("account_user_role") or "",
                "subscription_plan": entitlement.get("subscription_plan") or "",
                "has_active_subscription": bool(entitlement.get("has_active_subscription")),
                "expires_at": entitlement.get("expires_at"),
                "raw": info,
            }
        )

    if not team_accounts:
        return {"success": False, "error": "未发现 Team account", "accounts": []}

    preferred = next((item for item in team_accounts if item["has_active_subscription"]), team_accounts[0])
    return {"success": True, "account": preferred, "accounts": team_accounts}


def send_team_invitation(
    admin_account: Account,
    team_account_id: str,
    invitee_email: str,
    proxy: Optional[str] = None,
) -> Tuple[bool, str]:
    """发送 Team 邀请。"""
    if not admin_account.access_token:
        return False, "主账号缺少 access_token"

    try:
        response = _request_with_proxy_fallback(
            "POST",
            f"{BACKEND_API_BASE}/accounts/{team_account_id}/invites",
            headers={
                **_build_bearer_headers(admin_account.access_token, team_account_id),
                "Content-Type": "application/json",
            },
            json={
                "email_addresses": [invitee_email],
                "role": "standard-user",
                "resend_emails": True,
            },
            proxy=proxy,
        )
        if response.status_code in (200, 201):
            return True, "邀请发送成功"
        return False, f"邀请发送失败: HTTP {response.status_code} {response.text[:200]}"
    except Exception as exc:
        return False, f"邀请发送异常: {exc}"


def list_team_members(
    admin_account: Account,
    team_account_id: str,
    proxy: Optional[str] = None,
) -> Dict[str, Any]:
    """获取 Team 成员列表。"""
    if not admin_account.access_token:
        return {"success": False, "error": "主账号缺少 access_token", "members": []}

    members: List[Dict[str, Any]] = []
    limit = 100
    offset = 0
    try:
        while True:
            response = _request_with_proxy_fallback(
                "GET",
                f"{BACKEND_API_BASE}/accounts/{team_account_id}/users?limit={limit}&offset={offset}",
                headers=_build_bearer_headers(admin_account.access_token, team_account_id),
                proxy=proxy,
            )
            response.raise_for_status()
            data = response.json()
            items = data.get("items", []) or []
            total = int(data.get("total", len(items)))
            members.extend(items)
            if len(members) >= total or not items:
                break
            offset += limit
    except Exception as exc:
        return {"success": False, "error": f"获取 Team 成员失败: {exc}", "members": []}

    return {"success": True, "members": members, "total": len(members)}


def list_team_invites(
    admin_account: Account,
    team_account_id: str,
    proxy: Optional[str] = None,
) -> Dict[str, Any]:
    """获取 Team 邀请列表。"""
    if not admin_account.access_token:
        return {"success": False, "error": "主账号缺少 access_token", "items": []}

    try:
        response = _request_with_proxy_fallback(
            "GET",
            f"{BACKEND_API_BASE}/accounts/{team_account_id}/invites",
            headers=_build_bearer_headers(admin_account.access_token, team_account_id),
            proxy=proxy,
        )
        response.raise_for_status()
        data = response.json()
        items = data.get("items", []) or []
        return {"success": True, "items": items, "total": len(items)}
    except Exception as exc:
        return {"success": False, "error": f"获取邀请列表失败: {exc}", "items": []}


def extract_invitation_link(email_body: str) -> Optional[str]:
    """从邮件正文中提取 ChatGPT Team 邀请链接。"""
    content = str(email_body or "")
    patterns = [
        r"https://chatgpt\.com/[^\s\"'<>]*invite[^\s\"'<>]*",
        r"https://chatgpt\.com/[^\s\"'<>]*join[^\s\"'<>]*",
        r"https://chatgpt\.com/[^\s\"'<>]*workspace[^\s\"'<>]*",
    ]
    for pattern in patterns:
        match = re.search(pattern, content, re.IGNORECASE)
        if match:
            return match.group(0).rstrip(").,")
    return None


def accept_team_invitation_by_link(
    member_account: Account,
    invitation_url: str,
    proxy: Optional[str] = None,
) -> Tuple[bool, str]:
    """使用成员账号访问邀请链接完成加入。"""
    if not invitation_url:
        return False, "邀请链接为空"

    session = cffi_requests.Session(impersonate="chrome110", proxy=proxy)
    try:
        session_cookie = resolve_session_cookie(member_account)
        if session_cookie:
            session.cookies.set(
                session_cookie["name"],
                session_cookie["value"],
                domain=".chatgpt.com",
                path="/",
            )
        for cookie in _parse_cookie_string(member_account.cookies or ""):
            session.cookies.set(cookie["name"], cookie["value"], domain=cookie["domain"], path=cookie["path"])

        response = session.get(
            invitation_url,
            allow_redirects=True,
            timeout=30,
            headers={"Referer": BASE_URL},
        )
        final_url = getattr(response, "url", invitation_url)
        if "error" in str(final_url).lower():
            return False, f"接受邀请失败: {final_url}"
        if response.status_code not in (200, 201, 302):
            return False, f"接受邀请失败: HTTP {response.status_code}"
        return True, "接受邀请成功"
    except Exception as exc:
        return False, f"接受邀请异常: {exc}"
    finally:
        try:
            session.close()
        except Exception:
            pass


def refresh_member_team_token(
    member_account: Account,
    team_account_id: str,
    proxy: Optional[str] = None,
) -> Dict[str, Any]:
    """切换成员会话到 Team workspace，并刷新新的 access/session token。"""
    session_cookie = resolve_session_cookie(member_account)
    if not session_cookie:
        return {"success": False, "error": "账号缺少可用 session_token，请重新登录后重试"}

    url = (
        f"{BASE_URL}/api/auth/session"
        f"?exchange_workspace_token=true&workspace_id={team_account_id}&reason=setCurrentAccount"
    )
    try:
        response = _request_with_proxy_fallback(
            "GET",
            url,
            headers={
                "Accept": "application/json",
                "Cookie": build_session_cookie_header(session_cookie["value"], session_cookie["name"]),
                "Referer": BASE_URL,
            },
            proxy=proxy,
        )
        response.raise_for_status()
        data = response.json()
    except Exception as exc:
        return {"success": False, "error": f"刷新 Team token 失败: {exc}"}

    access_token = data.get("accessToken")
    session_token = data.get("sessionToken") or session_cookie["value"]
    expires_at = data.get("expires")
    if not access_token:
        return {"success": False, "error": "刷新结果缺少 accessToken"}

    access_claims = _decode_jwt_payload(access_token)
    auth_claims = access_claims.get("https://api.openai.com/auth") or {}
    refreshed_account_id = str(
        auth_claims.get("chatgpt_account_id")
        or (data.get("account") or {}).get("id")
        or team_account_id
    ).strip()
    refreshed_user_id = str(
        auth_claims.get("chatgpt_user_id")
        or auth_claims.get("user_id")
        or ""
    ).strip()

    parsed_expires_at = None
    if expires_at:
        try:
            parsed_expires_at = datetime.fromisoformat(str(expires_at).replace("Z", "+00:00"))
        except Exception:
            parsed_expires_at = None

    return {
        "success": True,
        "access_token": access_token,
        "session_token": session_token,
        "expires_at": parsed_expires_at,
        "account_id": refreshed_account_id,
        "user_id": refreshed_user_id,
        "raw": data,
    }
