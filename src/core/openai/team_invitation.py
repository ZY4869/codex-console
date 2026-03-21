"""
OpenAI Team 邀请与成员管理相关能力。
"""

import logging
import re
from datetime import datetime
from typing import Any, Dict, List, Optional, Tuple

from curl_cffi import requests as cffi_requests

from ...database.models import Account

logger = logging.getLogger(__name__)

BASE_URL = "https://chatgpt.com"
BACKEND_API_BASE = f"{BASE_URL}/backend-api"
ACCOUNT_CHECK_URL = f"{BACKEND_API_BASE}/accounts/check/v4-2023-04-27"


def _build_proxies(proxy: Optional[str]) -> Optional[dict]:
    if proxy:
        return {"http": proxy, "https": proxy}
    return None


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
        cookies.append({
            "name": name.strip(),
            "value": value.strip(),
            "domain": ".chatgpt.com",
            "path": "/",
        })
    return cookies


def discover_team_account(admin_account: Account, proxy: Optional[str] = None) -> Dict[str, Any]:
    """发现主账号当前可用的 Team account。"""
    if not admin_account.access_token:
        return {"success": False, "error": "主账号缺少 access_token"}

    try:
        response = cffi_requests.get(
            ACCOUNT_CHECK_URL,
            headers=_build_bearer_headers(admin_account.access_token),
            proxies=_build_proxies(proxy),
            timeout=30,
            impersonate="chrome110",
        )
        response.raise_for_status()
        data = response.json()
    except Exception as e:
        return {"success": False, "error": f"查询 Team account 失败: {e}"}

    accounts = data.get("accounts", {}) or {}
    team_accounts: List[Dict[str, Any]] = []
    for account_id, info in accounts.items():
        account = info.get("account", {}) or {}
        entitlement = info.get("entitlement", {}) or {}
        if account.get("plan_type") != "team":
            continue
        team_accounts.append({
            "team_account_id": str(account_id),
            "team_workspace_id": account.get("workspace_id") or account.get("id"),
            "name": account.get("name") or "",
            "role": account.get("account_user_role") or "",
            "subscription_plan": entitlement.get("subscription_plan") or "",
            "has_active_subscription": bool(entitlement.get("has_active_subscription")),
            "expires_at": entitlement.get("expires_at"),
            "raw": info,
        })

    if not team_accounts:
        return {"success": False, "error": "未发现 Team account", "accounts": []}

    # 优先选活跃订阅，其次第一个 team account。
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
        response = cffi_requests.post(
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
            proxies=_build_proxies(proxy),
            timeout=30,
            impersonate="chrome110",
        )
        if response.status_code in (200, 201):
            return True, "邀请发送成功"
        return False, f"邀请发送失败: HTTP {response.status_code} {response.text[:200]}"
    except Exception as e:
        return False, f"邀请发送异常: {e}"


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
            response = cffi_requests.get(
                f"{BACKEND_API_BASE}/accounts/{team_account_id}/users?limit={limit}&offset={offset}",
                headers=_build_bearer_headers(admin_account.access_token, team_account_id),
                proxies=_build_proxies(proxy),
                timeout=30,
                impersonate="chrome110",
            )
            response.raise_for_status()
            data = response.json()
            items = data.get("items", []) or []
            total = int(data.get("total", len(items)))
            members.extend(items)
            if len(members) >= total or not items:
                break
            offset += limit
    except Exception as e:
        return {"success": False, "error": f"获取 Team 成员失败: {e}", "members": []}

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
        response = cffi_requests.get(
            f"{BACKEND_API_BASE}/accounts/{team_account_id}/invites",
            headers=_build_bearer_headers(admin_account.access_token, team_account_id),
            proxies=_build_proxies(proxy),
            timeout=30,
            impersonate="chrome110",
        )
        response.raise_for_status()
        data = response.json()
        return {"success": True, "items": data.get("items", []) or [], "total": len(data.get("items", []) or [])}
    except Exception as e:
        return {"success": False, "error": f"获取邀请列表失败: {e}", "items": []}


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
        if member_account.session_token:
            session.cookies.set(
                "__Secure-next-auth.session-token",
                member_account.session_token,
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
    except Exception as e:
        return False, f"接受邀请异常: {e}"
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
    """将成员会话切换到 Team workspace 并换取新 AT/ST。"""
    if not member_account.session_token:
        return {"success": False, "error": "成员账号缺少 session_token"}

    url = (
        f"{BASE_URL}/api/auth/session"
        f"?exchange_workspace_token=true&workspace_id={team_account_id}&reason=setCurrentAccount"
    )
    try:
        response = cffi_requests.get(
            url,
            headers={
                "Accept": "application/json",
                "Cookie": f"__Secure-next-auth.session-token={member_account.session_token}",
                "Referer": BASE_URL,
            },
            proxies=_build_proxies(proxy),
            timeout=30,
            impersonate="chrome110",
        )
        response.raise_for_status()
        data = response.json()
    except Exception as e:
        return {"success": False, "error": f"刷新 Team token 失败: {e}"}

    access_token = data.get("accessToken")
    session_token = data.get("sessionToken") or member_account.session_token
    expires_at = data.get("expires")
    if not access_token:
        return {"success": False, "error": "刷新结果缺少 accessToken"}

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
        "raw": data,
    }
