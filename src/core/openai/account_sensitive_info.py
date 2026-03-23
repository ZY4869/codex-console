"""
账号敏感会话信息构造器。
"""

from __future__ import annotations

import base64
import json
import logging
from datetime import datetime, timezone
from typing import Any, Dict, Optional

from curl_cffi import requests as cffi_requests

from ...database.models import Account

logger = logging.getLogger(__name__)

WARNING_BANNER = (
    "!!!!!!!!!!!!!!!!!!!! DO NOT SHARE ANY PART OF THE INFORMATION YOU SEE HERE. "
    "THIS INFORMATION IS SENSITIVE AND CAN GRANT ACCESS TO YOUR ACCOUNT. "
    "SHARING THIS INFORMATION IS LIKE SHARING YOUR PASSWORD. !!!!!!!!!!!!!!!!!!!!"
)
SESSION_URL = "https://chatgpt.com/api/auth/session"
DEFAULT_RUM_VIEW_TAGS = {"light_account": {"fetched": False}}
SENSITIVE_SESSION_PAYLOAD_KEY = "sensitive_session_payload"
SENSITIVE_SESSION_PAYLOAD_UPDATED_AT_KEY = "sensitive_session_payload_updated_at"


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


def _coalesce(*values: Any) -> Any:
    for value in values:
        if value is not None and value != "":
            return value
    return None


def _nested_get(data: Any, *path: str) -> Any:
    current = data
    for key in path:
        if not isinstance(current, dict):
            return None
        current = current.get(key)
    return current


def _normalize_bool(value: Any, default: bool = False) -> bool:
    return default if value is None else bool(value)


def _to_iso_z(value: Any) -> Optional[str]:
    if not value:
        return None
    if isinstance(value, str):
        return value
    if not isinstance(value, datetime):
        return None
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    else:
        value = value.astimezone(timezone.utc)
    return value.isoformat().replace("+00:00", "Z")


def _exp_to_iso_z(value: Any) -> Optional[str]:
    try:
        ts = int(value)
    except (TypeError, ValueError):
        return None
    return datetime.fromtimestamp(ts, tz=timezone.utc).isoformat().replace("+00:00", "Z")


def _infer_idp_from_sub(sub: Optional[str]) -> Optional[str]:
    raw = (sub or "").strip()
    if not raw or "|" not in raw:
        return None
    return raw.split("|", 1)[0]


def fetch_remote_session_payload(session_token: str, proxy_url: Optional[str] = None) -> Optional[Dict[str, Any]]:
    if not session_token:
        return None

    session = cffi_requests.Session(impersonate="chrome120", proxy=proxy_url)
    try:
        session.cookies.set(
            "__Secure-next-auth.session-token",
            session_token,
            domain=".chatgpt.com",
            path="/",
        )
        response = session.get(
            SESSION_URL,
            headers={
                "accept": "application/json",
                "user-agent": (
                    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                    "AppleWebKit/537.36 (KHTML, like Gecko) "
                    "Chrome/120.0.0.0 Safari/537.36"
                ),
            },
            timeout=30,
        )
        if response.status_code != 200:
            logger.info("读取账号敏感会话信息失败，HTTP %s", response.status_code)
            return None
        data = response.json()
        return data if isinstance(data, dict) else None
    except Exception as exc:
        logger.info("读取账号敏感会话信息失败: %s", exc)
        return None
    finally:
        try:
            session.close()
        except Exception:
            pass


def build_account_sensitive_session_payload(
    account: Account,
    proxy_url: Optional[str] = None,
) -> Dict[str, Any]:
    remote = fetch_remote_session_payload(account.session_token, proxy_url=proxy_url) if account.session_token else None
    remote_user = remote.get("user") if isinstance(remote, dict) and isinstance(remote.get("user"), dict) else {}
    remote_account = remote.get("account") if isinstance(remote, dict) and isinstance(remote.get("account"), dict) else {}
    extra_data = account.extra_data or {}

    access_claims = _decode_jwt_payload(account.access_token)
    id_claims = _decode_jwt_payload(account.id_token)
    access_auth = _nested_get(access_claims, "https://api.openai.com/auth") or {}
    access_profile = _nested_get(access_claims, "https://api.openai.com/profile") or {}
    id_auth = _nested_get(id_claims, "https://api.openai.com/auth") or {}

    user_email = _coalesce(
        remote_user.get("email"),
        access_profile.get("email"),
        id_claims.get("email"),
        extra_data.get("email"),
        account.email,
    )
    picture = _coalesce(
        remote_user.get("picture"),
        remote_user.get("image"),
        id_claims.get("picture"),
        extra_data.get("picture"),
        extra_data.get("image"),
    )
    plan_type = _coalesce(
        remote_account.get("planType"),
        remote_account.get("plan_type"),
        access_auth.get("chatgpt_plan_type"),
        id_auth.get("chatgpt_plan_type"),
        account.subscription_type,
        "free",
    )
    structure = _coalesce(
        remote_account.get("structure"),
        "team" if plan_type == "team" or account.subscription_type == "team" else "personal",
    )

    return {
        "WARNING_BANNER": WARNING_BANNER,
        "user": {
            "id": _coalesce(
                remote_user.get("id"),
                access_auth.get("chatgpt_user_id"),
                access_auth.get("user_id"),
                id_auth.get("chatgpt_user_id"),
                id_auth.get("user_id"),
                extra_data.get("user_id"),
                id_claims.get("sub"),
            ),
            "name": _coalesce(
                remote_user.get("name"),
                id_claims.get("name"),
                extra_data.get("name"),
                user_email.split("@", 1)[0] if user_email else None,
            ),
            "email": user_email,
            "image": picture,
            "picture": _coalesce(remote_user.get("picture"), picture),
            "idp": _coalesce(
                remote_user.get("idp"),
                id_claims.get("auth_provider"),
                _infer_idp_from_sub(id_claims.get("sub")),
            ),
            "iat": _coalesce(remote_user.get("iat"), id_claims.get("iat"), access_claims.get("iat")),
            "mfa": _normalize_bool(_coalesce(remote_user.get("mfa"), extra_data.get("mfa")), False),
        },
        "expires": _coalesce(
            remote.get("expires") if isinstance(remote, dict) else None,
            _to_iso_z(account.expires_at),
            _exp_to_iso_z(access_claims.get("exp")),
            _exp_to_iso_z(id_claims.get("exp")),
        ),
        "account": {
            "id": _coalesce(
                remote_account.get("id"),
                access_auth.get("chatgpt_account_id"),
                id_auth.get("chatgpt_account_id"),
                account.account_id,
                account.workspace_id,
            ),
            "planType": plan_type,
            "structure": structure,
            "isConversationClassifierEnabledForWorkspace": _normalize_bool(
                remote_account.get("isConversationClassifierEnabledForWorkspace"),
                False,
            ),
            "isFinservEnabledWorkspace": _normalize_bool(remote_account.get("isFinservEnabledWorkspace"), False),
            "isFedrampCompliantWorkspace": _normalize_bool(remote_account.get("isFedrampCompliantWorkspace"), False),
            "isDelinquent": _normalize_bool(remote_account.get("isDelinquent"), False),
            "residencyRegion": _coalesce(
                remote_account.get("residencyRegion"),
                access_auth.get("chatgpt_residency_region"),
                id_auth.get("chatgpt_residency_region"),
                "no_constraint",
            ),
            "computeResidency": _coalesce(
                remote_account.get("computeResidency"),
                access_auth.get("chatgpt_compute_residency"),
                id_auth.get("chatgpt_compute_residency"),
                "no_constraint",
            ),
        },
        "accessToken": _coalesce(
            remote.get("accessToken") if isinstance(remote, dict) else None,
            account.access_token,
        ),
        "authProvider": _coalesce(
            remote.get("authProvider") if isinstance(remote, dict) else None,
            "openai",
        ),
        "sessionToken": _coalesce(
            remote.get("sessionToken") if isinstance(remote, dict) else None,
            account.session_token,
        ),
        "rumViewTags": (
            remote.get("rumViewTags")
            if isinstance(remote, dict) and isinstance(remote.get("rumViewTags"), dict)
            else DEFAULT_RUM_VIEW_TAGS.copy()
        ),
    }


def persist_account_sensitive_session_payload(db, account: Account, payload: Dict[str, Any]) -> Account:
    extra_data = account.extra_data.copy() if account.extra_data else {}
    extra_data[SENSITIVE_SESSION_PAYLOAD_KEY] = payload
    extra_data[SENSITIVE_SESSION_PAYLOAD_UPDATED_AT_KEY] = (
        datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
    )
    account.extra_data = extra_data
    db.commit()
    db.refresh(account)
    return account
