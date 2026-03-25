"""
Grok 临时邮箱适配。
"""

from __future__ import annotations

import re
import uuid
from typing import Any, Dict, Optional

from curl_cffi import requests as cffi_requests

from .signup_client import FORCED_HTTP_VERSION


class GrokEmailServiceError(RuntimeError):
    pass


class BczyEmailService:
    def __init__(self, *, api_key: Optional[str], domain: str, config: Dict[str, Any]):
        self.api_key = (api_key or "").strip()
        self.domain = (domain or "").strip() or "bczy.site"
        self.config = dict(config or {})

    def create_address(self, order_index: int) -> Dict[str, Any]:
        if self.config.get("mock_mode"):
            local = f"grok-{order_index + 1}-{uuid.uuid4().hex[:8]}"
            return {"email": f"{local}@{self.domain}"}

        create_url = str(self.config.get("email_create_url") or "").strip()
        if not create_url:
            raise GrokEmailServiceError("Email provider adapter is not configured. Set email_create_url or enable mock_mode.")

        response = cffi_requests.post(
            create_url,
            json={"domain": self.domain, "index": order_index},
            headers=self._build_headers(),
            timeout=20,
            impersonate="chrome110",
            http_version=FORCED_HTTP_VERSION,
        )
        response.raise_for_status()
        payload = response.json()
        email = payload.get("email") or payload.get("address")
        if not email:
            raise GrokEmailServiceError("Email provider did not return an address.")
        return {"email": str(email), "payload": payload}

    def poll_code(self, email: str, *, timeout_seconds: int, poll_interval: int) -> str:
        if self.config.get("mock_mode"):
            return str(self.config.get("mock_verification_code") or "123456")

        messages_url = str(self.config.get("email_messages_url") or "").strip()
        if not messages_url:
            raise GrokEmailServiceError("Email verification adapter is not configured. Set email_messages_url or enable mock_mode.")

        import time

        deadline = time.time() + max(1, timeout_seconds)
        while time.time() < deadline:
            response = cffi_requests.get(
                messages_url.format(email=email),
                headers=self._build_headers(),
                timeout=20,
                impersonate="chrome110",
                http_version=FORCED_HTTP_VERSION,
            )
            response.raise_for_status()
            code = self._extract_code(response.json())
            if code:
                return code
            time.sleep(max(1, poll_interval))
        raise GrokEmailServiceError(f"Timed out while waiting for verification code: {email}")

    def _build_headers(self) -> Dict[str, str]:
        headers: Dict[str, str] = {}
        if self.api_key:
            headers["X-API-Key"] = self.api_key
            headers["Authorization"] = f"Bearer {self.api_key}"
        return headers

    @staticmethod
    def _extract_code(payload: Any) -> Optional[str]:
        texts: list[str] = []
        if isinstance(payload, dict):
            for key in ("code", "otp", "verification_code"):
                value = payload.get(key)
                if value:
                    return str(value)
            for key in ("messages", "items", "data"):
                value = payload.get(key)
                if isinstance(value, list):
                    for item in value:
                        if isinstance(item, dict):
                            texts.extend(str(item.get(field) or "") for field in ("text", "body", "html", "content"))
                        else:
                            texts.append(str(item))
        elif isinstance(payload, list):
            texts.extend(str(item) for item in payload)

        for text in texts:
            match = re.search(r"\b(\d{4,8})\b", text)
            if match:
                return match.group(1)
        return None
