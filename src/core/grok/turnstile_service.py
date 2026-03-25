"""
Turnstile solver adapters.
"""

from __future__ import annotations

import time
from typing import Optional

from curl_cffi import requests as cffi_requests

from .signup_client import FORCED_HTTP_VERSION
from .turnstile_local_solver import LocalTurnstileSolverClient, TurnstileServiceError


class TurnstileService:
    def __init__(self):
        self.local_solver = LocalTurnstileSolverClient()

    def solve(
        self,
        *,
        mode: str,
        sitekey: str,
        page_url: str,
        action: Optional[str] = None,
        yescaptcha_key: Optional[str] = None,
        solver_url: Optional[str] = None,
    ) -> str:
        normalized = (mode or "yescaptcha").strip().lower()
        if normalized == "local":
            return self.local_solver.solve(
                sitekey=sitekey,
                page_url=page_url,
                action=action,
                solver_url=solver_url,
            )
        return self._solve_via_yescaptcha(
            sitekey=sitekey,
            page_url=page_url,
            action=action,
            yescaptcha_key=yescaptcha_key,
        )

    def _solve_via_yescaptcha(
        self,
        *,
        sitekey: str,
        page_url: str,
        action: Optional[str],
        yescaptcha_key: Optional[str],
    ) -> str:
        api_key = (yescaptcha_key or "").strip()
        if not api_key:
            raise TurnstileServiceError("YesCaptcha key is missing.")

        create_response = cffi_requests.post(
            "https://api.yescaptcha.com/createTask",
            json={
                "clientKey": api_key,
                "task": {
                    "type": "TurnstileTaskProxyless",
                    "websiteURL": page_url,
                    "websiteKey": sitekey,
                    "action": action or "",
                },
            },
            timeout=30,
            impersonate="chrome110",
            http_version=FORCED_HTTP_VERSION,
        )
        create_response.raise_for_status()
        task_id = create_response.json().get("taskId")
        if not task_id:
            raise TurnstileServiceError("YesCaptcha createTask failed.")

        deadline = time.time() + 120
        while time.time() < deadline:
            time.sleep(3)
            result_response = cffi_requests.post(
                "https://api.yescaptcha.com/getTaskResult",
                json={"clientKey": api_key, "taskId": task_id},
                timeout=30,
                impersonate="chrome110",
                http_version=FORCED_HTTP_VERSION,
            )
            result_response.raise_for_status()
            payload = result_response.json()
            status = str(payload.get("status") or "").lower()
            if status == "processing":
                continue
            if status == "ready":
                token = ((payload.get("solution") or {}).get("token")) or (
                    (payload.get("solution") or {}).get("gRecaptchaResponse")
                )
                if token:
                    return str(token)
            raise TurnstileServiceError(
                str(payload.get("errorDescription") or "YesCaptcha failed to solve Turnstile.")
            )
        raise TurnstileServiceError("Timed out while waiting for YesCaptcha result.")
