"""
Grok NSFW 开关适配。
"""

from __future__ import annotations

import struct
from typing import Any, Dict

from curl_cffi import requests as cffi_requests

from .signup_client import DEFAULT_IMPERSONATE, FORCED_HTTP_VERSION, build_proxy_mapping


class NsfwServiceError(RuntimeError):
    pass


class NsfwService:
    def enable(
        self,
        *,
        task_config: Dict[str, Any],
        sso_token: str,
        sso_rw_token: str | None = None,
        impersonate: str | None = None,
        user_agent: str | None = None,
        proxy_url: str | None = None,
        extra_cookies: Dict[str, str] | None = None,
    ) -> Dict[str, Any]:
        if task_config.get("mock_mode"):
            return {"success": True, "mode": "mock"}

        token = str(sso_token or "").strip()
        if not token:
            raise NsfwServiceError("sso_token is required for NSFW.")

        url = str(task_config.get("nsfw_url") or "https://grok.com/auth_mgmt.AuthManagement/UpdateUserFeatureControls").strip()
        proxies = build_proxy_mapping(proxy_url)
        cookies = dict(extra_cookies or {})
        cookies["sso"] = token
        cookies["sso-rw"] = str(sso_rw_token or token).strip()
        request_headers = {
            "content-type": "application/grpc-web+proto",
            "origin": "https://grok.com",
            "referer": "https://grok.com/?_s=data",
            "x-grpc-web": "1",
        }
        if user_agent:
            request_headers["user-agent"] = user_agent

        enable_payload = b"\x00\x00\x00\x00\x20\x0a\x02\x10\x01\x12\x1a\x0a\x18always_show_nsfw_content"
        response = cffi_requests.post(
            url,
            headers=request_headers,
            cookies=cookies,
            data=enable_payload,
            timeout=15,
            impersonate=impersonate or DEFAULT_IMPERSONATE,
            proxies=proxies,
            http_version=FORCED_HTTP_VERSION,
        )
        grpc_status = response.headers.get("grpc-status")
        if response.status_code != 200 or grpc_status not in (None, "0"):
            raise NsfwServiceError(f"Failed to enable NSFW: HTTP {response.status_code}, grpc={grpc_status or 'unknown'}.")

        unhinged_headers = {
            "accept": "*/*",
            "content-type": "application/grpc-web+proto",
            "origin": "https://grok.com",
            "referer": "https://grok.com/",
            "x-grpc-web": "1",
            "x-user-agent": "connect-es/2.1.1",
        }
        if user_agent:
            unhinged_headers["user-agent"] = user_agent
        cookie_header = [f"{key}={value}" for key, value in cookies.items()]
        if cookie_header:
            unhinged_headers["cookie"] = "; ".join(cookie_header)

        payload = bytes([0x08, 0x01, 0x10, 0x01])
        framed_payload = b"\x00" + struct.pack(">I", len(payload)) + payload
        second = cffi_requests.post(
            url,
            headers=unhinged_headers,
            data=framed_payload,
            timeout=30,
            impersonate=impersonate or DEFAULT_IMPERSONATE,
            proxies=proxies,
            http_version=FORCED_HTTP_VERSION,
        )
        if second.status_code != 200:
            raise NsfwServiceError(f"Failed to enable unhinged mode: HTTP {second.status_code}.")

        return {
            "success": True,
            "status_code": second.status_code,
            "hex_reply": response.content.hex(),
        }
