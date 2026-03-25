"""
Grok 用户协议接受适配。
"""

from __future__ import annotations

from typing import Any, Dict

from curl_cffi import requests as cffi_requests

from .signup_client import DEFAULT_IMPERSONATE, FORCED_HTTP_VERSION, build_proxy_mapping


class UserAgreementServiceError(RuntimeError):
    pass


class UserAgreementService:
    def accept(
        self,
        *,
        task_config: Dict[str, Any],
        sso_token: str,
        sso_rw_token: str | None = None,
        impersonate: str | None = None,
        user_agent: str | None = None,
        proxy_url: str | None = None,
    ) -> Dict[str, Any]:
        if task_config.get("mock_mode"):
            return {"success": True, "mode": "mock"}

        token = str(sso_token or "").strip()
        if not token:
            raise UserAgreementServiceError("sso_token is required for user agreement.")

        response = cffi_requests.post(
            str(task_config.get("user_agreement_url") or "https://accounts.x.ai/auth_mgmt.AuthManagement/SetTosAcceptedVersion").strip(),
            headers={
                "content-type": "application/grpc-web+proto",
                "origin": "https://accounts.x.ai",
                "referer": "https://accounts.x.ai/accept-tos",
                "x-grpc-web": "1",
                **({"user-agent": user_agent} if user_agent else {}),
            },
            cookies={
                "sso": token,
                "sso-rw": str(sso_rw_token or token).strip(),
            },
            data=b"\x00\x00\x00\x00\x02\x10\x01",
            timeout=15,
            impersonate=impersonate or DEFAULT_IMPERSONATE,
            proxies=build_proxy_mapping(proxy_url),
            http_version=FORCED_HTTP_VERSION,
        )
        grpc_status = response.headers.get("grpc-status")
        if response.status_code == 200 and grpc_status in (None, "0"):
            return {
                "success": True,
                "status_code": response.status_code,
                "grpc_status": grpc_status,
                "hex_reply": response.content.hex(),
            }
        raise UserAgreementServiceError(f"Failed to accept user agreement: HTTP {response.status_code}, grpc={grpc_status or 'unknown'}.")
