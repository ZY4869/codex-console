"""
Local Turnstile solver adapter.
"""

from __future__ import annotations

import json
import time
from typing import Any, Optional

from curl_cffi import requests as cffi_requests

from .runtime import inspect_local_solver_service


LOCAL_SOLVER_POLL_TIMEOUT_SECONDS = 120
LOCAL_SOLVER_POLL_INTERVAL_SECONDS = 2
LOCAL_SOLVER_FALLBACK_STATUSES = {404, 405, 422}


class TurnstileServiceError(RuntimeError):
    pass


class LocalTurnstileSolverClient:
    def solve(self, *, sitekey: str, page_url: str, action: Optional[str], solver_url: Optional[str]) -> str:
        base_url = (solver_url or "").strip().rstrip("/")
        if not base_url:
            raise TurnstileServiceError("Local solver URL is missing.")
        details = inspect_local_solver_service(base_url)
        if details.get("placeholder"):
            raise TurnstileServiceError(
                "The configured local solver is still the placeholder local_solver_app service. "
                "Stop the old placeholder process and start the managed TurnstileSolver."
            )

        task_token = self._try_task_api(
            base_url=base_url,
            sitekey=sitekey,
            page_url=page_url,
            action=action,
        )
        if task_token:
            return task_token
        return self._solve_via_direct_api(
            base_url=base_url,
            sitekey=sitekey,
            page_url=page_url,
            action=action,
        )

    def _try_task_api(
        self,
        *,
        base_url: str,
        sitekey: str,
        page_url: str,
        action: Optional[str],
    ) -> Optional[str]:
        response = cffi_requests.get(
            f"{base_url}/turnstile",
            params=self._build_task_params(sitekey=sitekey, page_url=page_url, action=action),
            timeout=30,
            impersonate="chrome110",
        )
        if response.status_code in LOCAL_SOLVER_FALLBACK_STATUSES:
            return None
        self._raise_on_http_error(response, "GET /turnstile")
        payload = self._read_json(response)
        token = self._extract_token(payload)
        if token:
            return token
        task_id = self._extract_task_id(payload)
        if not task_id:
            return None
        return self._poll_task_result(base_url=base_url, task_id=task_id)

    def _solve_via_direct_api(
        self,
        *,
        base_url: str,
        sitekey: str,
        page_url: str,
        action: Optional[str],
    ) -> str:
        payload = {"sitekey": sitekey, "page_url": page_url, "action": action}
        response = cffi_requests.post(
            f"{base_url}/turnstile",
            data=json.dumps(payload),
            headers={"content-type": "application/json"},
            timeout=60,
            impersonate="chrome110",
        )
        self._raise_on_http_error(response, "POST /turnstile")
        payload = self._read_json(response)
        token = self._extract_token(payload)
        if token:
            return token
        task_id = self._extract_task_id(payload)
        if task_id:
            return self._poll_task_result(base_url=base_url, task_id=task_id)
        error = self._extract_error(payload) or "Local solver did not return a token."
        raise TurnstileServiceError(error)

    def _poll_task_result(self, *, base_url: str, task_id: str) -> str:
        deadline = time.time() + LOCAL_SOLVER_POLL_TIMEOUT_SECONDS
        while time.time() < deadline:
            payload = self._fetch_task_result(base_url=base_url, task_id=task_id)
            token = self._extract_token(payload)
            if token:
                if token == "CAPTCHA_FAIL":
                    raise TurnstileServiceError("Local solver returned CAPTCHA_FAIL.")
                return token
            error = self._extract_error(payload)
            status = str(payload.get("status") or payload.get("state") or "").strip().lower()
            if error and status not in {"processing", "pending", "queued", ""}:
                raise TurnstileServiceError(error)
            time.sleep(LOCAL_SOLVER_POLL_INTERVAL_SECONDS)
        raise TurnstileServiceError("Timed out while waiting for local Turnstile solver result.")

    def _fetch_task_result(self, *, base_url: str, task_id: str) -> dict[str, Any]:
        response = cffi_requests.get(
            f"{base_url}/result",
            params={"id": task_id},
            timeout=30,
            impersonate="chrome110",
        )
        if response.status_code == 404:
            response = cffi_requests.get(
                f"{base_url}/result/{task_id}",
                timeout=30,
                impersonate="chrome110",
            )
        self._raise_on_http_error(response, "GET /result")
        return self._read_json(response)

    @staticmethod
    def _build_task_params(*, sitekey: str, page_url: str, action: Optional[str]) -> dict[str, str]:
        params = {"sitekey": sitekey, "url": page_url}
        if action:
            params["action"] = action
        return params

    @staticmethod
    def _extract_task_id(payload: dict[str, Any]) -> Optional[str]:
        for key in ("taskId", "task_id", "id", "request_id"):
            value = payload.get(key)
            if value:
                return str(value)
        return None

    @staticmethod
    def _extract_token(payload: dict[str, Any]) -> Optional[str]:
        direct = payload.get("token")
        if direct:
            return str(direct)
        solution = payload.get("solution")
        if isinstance(solution, dict):
            nested = solution.get("token") or solution.get("gRecaptchaResponse")
            if nested:
                return str(nested)
        if isinstance(solution, str) and solution:
            return solution
        return None

    @staticmethod
    def _extract_error(payload: dict[str, Any]) -> Optional[str]:
        for key in ("error", "message", "detail", "errorDescription"):
            value = payload.get(key)
            if value:
                return str(value)
        return None

    @staticmethod
    def _read_json(response) -> dict[str, Any]:
        try:
            payload = response.json()
        except Exception as exc:  # noqa: BLE001
            raise TurnstileServiceError(f"Local solver returned invalid JSON: {exc}") from exc
        if isinstance(payload, dict):
            return payload
        raise TurnstileServiceError("Local solver returned an unexpected payload.")

    def _raise_on_http_error(self, response, operation: str) -> None:
        if response.status_code < 400:
            return
        raise TurnstileServiceError(self._build_http_error_message(response, operation))

    def _build_http_error_message(self, response, operation: str) -> str:
        message = f"Local Turnstile solver {operation} failed with HTTP {response.status_code}."
        if response.status_code == 422:
            message += " The configured solver API may not match Grok local mode."
            message += " Expected either GET /turnstile?url=...&sitekey=... plus GET /result?id=...,"
            message += " or POST /turnstile returning a token."
        payload = self._try_read_json(response)
        detail = self._extract_error(payload) if payload else None
        if detail:
            message += f" Detail: {detail}"
        elif getattr(response, "text", ""):
            message += f" Detail: {response.text}"
        return message

    @staticmethod
    def _try_read_json(response) -> Optional[dict[str, Any]]:
        try:
            payload = response.json()
        except Exception:  # noqa: BLE001
            return None
        return payload if isinstance(payload, dict) else None
