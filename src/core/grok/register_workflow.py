"""
Grok 注册工作流。
"""

from __future__ import annotations

import secrets
import threading
import uuid
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime
from typing import Any, Dict, Optional

from ...database import grok_crud
from ...database.grok_models import GrokRegisterAccount, GrokRegisterTask
from ...database.session import get_db
from ...web.task_manager import task_manager
from .managed_email_service import create_grok_email_service
from .nsfw_service import NsfwService, NsfwServiceError
from .signup_client import (
    DEFAULT_IMPERSONATE,
    DEFAULT_SIGNUP_URL,
    GrokSignupBootstrap,
    GrokSignupClient,
    GrokSignupResult,
    discover_signup_bootstrap,
)
from .turnstile_service import TurnstileService
from .user_agreement_service import UserAgreementService


RUNNING_GROK_STATUSES = {"pending", "running"}


class GrokWorkflowError(RuntimeError):
    pass


class GrokWorkflowCancelledError(GrokWorkflowError):
    pass


class GrokWorkflowConfigurationError(GrokWorkflowError):
    pass


def _mask_token(value: Optional[str]) -> Optional[str]:
    if not value:
        return None
    text = str(value)
    if len(text) <= 10:
        return text
    return f"{text[:6]}...{text[-4:]}"


def build_grok_response(task: GrokRegisterTask, runtime_status: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    runtime_status = runtime_status or {}
    accounts = sorted(task.accounts or [], key=lambda item: item.order_index)
    account_payload = [
        {
            "id": account.id,
            "order_index": account.order_index,
            "email": account.email,
            "status": account.status,
            "step": account.step,
            "sso_token_preview": _mask_token(account.sso_token),
            "nsfw_enabled": bool(account.nsfw_enabled),
            "error_message": account.error_message,
            "created_at": account.created_at.isoformat() if account.created_at else None,
            "completed_at": account.completed_at.isoformat() if account.completed_at else None,
        }
        for account in accounts
    ]
    stats = {
        "total": len(accounts),
        "pending": sum(1 for item in accounts if item.status == "pending"),
        "running": sum(1 for item in accounts if item.status == "running"),
        "completed": sum(1 for item in accounts if item.status == "completed"),
        "failed": sum(1 for item in accounts if item.status == "failed"),
        "cancelled": sum(1 for item in accounts if item.status == "cancelled"),
    }
    return {
        "id": task.id,
        "task_uuid": task.task_uuid,
        "status": task.status,
        "target_count": task.target_count,
        "thread_count": task.thread_count,
        "success_count": task.success_count,
        "failed_count": task.failed_count,
        "proxy": task.proxy,
        "email_domain": task.email_domain,
        "captcha_mode": task.captcha_mode,
        "config": {
            "solver_url": (task.config or {}).get("solver_url"),
            "flaresolverr_url": (task.config or {}).get("flaresolverr_url"),
            "email_service_type": (task.config or {}).get("email_service_type") or "auto",
            "email_service_id": (task.config or {}).get("email_service_id"),
            "has_bczy_api_key": bool((task.config or {}).get("bczy_api_key")),
            "has_yescaptcha_key": bool((task.config or {}).get("yescaptcha_key")),
        },
        "logs": task.logs,
        "error_message": task.error_message,
        "result": task.result or {},
        "created_at": task.created_at.isoformat() if task.created_at else None,
        "started_at": task.started_at.isoformat() if task.started_at else None,
        "completed_at": task.completed_at.isoformat() if task.completed_at else None,
        "accounts": account_payload,
        "stats": stats,
        "runtime_message": runtime_status.get("runtime_message"),
        "cancel_available": task.status in RUNNING_GROK_STATUSES,
    }


class GrokRegisterOrchestrator:
    def __init__(self, task_uuid: str):
        self.task_uuid = task_uuid
        self.turnstile_service = TurnstileService()
        self.user_agreement_service = UserAgreementService()
        self.nsfw_service = NsfwService()
        self._thread_state = threading.local()
        self._signup_bootstrap: GrokSignupBootstrap | None = None

    def run(self):
        try:
            self._set_task_status("running", started_at=datetime.utcnow(), error_message=None, completed_at=None)
            task = self._get_task(required=True)
            self._ensure_accounts(task)
            if not (task.config or {}).get("mock_mode"):
                signup_url = str((task.config or {}).get("signup_page_url") or DEFAULT_SIGNUP_URL).strip() or DEFAULT_SIGNUP_URL
                self._signup_bootstrap = discover_signup_bootstrap(proxy_url=task.proxy, signup_url=signup_url)
            accounts = self._get_task(required=True).accounts
            with ThreadPoolExecutor(max_workers=max(1, min(task.thread_count, len(accounts) or 1))) as executor:
                futures = [executor.submit(self._register_single, account.id) for account in accounts]
                for future in as_completed(futures):
                    future.result()
            self._refresh_counters()
            self._finalize_task()
        except GrokWorkflowCancelledError:
            self._cancel_task("Task cancelled.")
        except Exception as exc:  # noqa: PERF203
            self._fail_task(str(exc))

    def cancel(self):
        task_manager.cancel_task(self.task_uuid)

    def _register_single(self, account_id: int):
        self._raise_if_cancelled()
        account = self._reload_account(account_id)
        if not account or account.status == "completed":
            return
        signup_client = None
        try:
            self._update_account(account_id, status="running", step="prepare", error_message=None, completed_at=None)
            task = self._get_task(required=True)
            config = task.config or {}
            email_service = create_grok_email_service(task)
            email = self._run_step(account_id, "create_email", lambda: email_service.create_address(account.order_index)["email"])
            self._update_account(account_id, email=email)
            if not config.get("mock_mode"):
                signup_client = self._run_step(account_id, "prepare_signup", lambda: self._create_signup_client(task))
                self._thread_state.signup_client = signup_client
            challenge = self._run_step(account_id, "send_code", lambda: self._send_verification_code(email, config))
            code = self._run_step(
                account_id,
                "poll_code",
                lambda: email_service.poll_code(
                    email,
                    timeout_seconds=int(config.get("email_code_timeout") or 120),
                    poll_interval=int(config.get("email_code_poll_interval") or 3),
                ),
            )
            verification = self._run_step(account_id, "verify_code", lambda: self._verify_code(email, code, challenge, config))
            captcha_token = self._run_step(
                account_id,
                "solve_turnstile",
                lambda: (
                    "mock-turnstile-token"
                    if config.get("mock_mode")
                    else self.turnstile_service.solve(
                        mode=task.captcha_mode,
                        sitekey=self._resolve_turnstile_site_key(config),
                        page_url=self._resolve_signup_page_url(config),
                        action=config.get("turnstile_action"),
                        yescaptcha_key=config.get("yescaptcha_key"),
                        solver_url=config.get("solver_url"),
                    )
                ),
            )
            signup_output = self._run_step(account_id, "sign_up", lambda: self._sign_up(email, code, captcha_token, verification, config))
            signup_result = self._normalize_signup_result(signup_output)
            self._update_account(account_id, sso_token=signup_result.sso_token)
            self._run_step(
                account_id,
                "accept_agreement",
                lambda: self.user_agreement_service.accept(
                    task_config=config,
                    sso_token=signup_result.sso_token,
                    sso_rw_token=signup_result.sso_rw_token,
                    impersonate=signup_result.impersonate,
                    user_agent=signup_result.user_agent,
                    proxy_url=task.proxy,
                ),
            )
            self._handle_nsfw_step(account_id, config, task.proxy, signup_result)
            self._update_account(account_id, status="completed", step="completed", completed_at=datetime.utcnow(), error_message=None)
            self._log(f"Account #{account.order_index + 1} completed: {email}")
        except GrokWorkflowCancelledError:
            self._update_account(account_id, status="cancelled", step="cancelled", completed_at=datetime.utcnow())
            raise
        except Exception as exc:  # noqa: PERF203
            self._update_account(
                account_id,
                status="failed",
                step="failed",
                error_message=str(exc),
                completed_at=datetime.utcnow(),
            )
            self._log(f"Account #{account.order_index + 1} failed: {exc}")
        finally:
            if signup_client:
                signup_client.close()
            if getattr(self._thread_state, "signup_client", None) is signup_client:
                self._thread_state.signup_client = None
            self._refresh_counters()

    def _run_step(self, account_id: int, step: str, callback):
        self._raise_if_cancelled()
        self._update_account(account_id, status="running", step=step)
        self._sync_status_snapshot(runtime_message=f"Running {step}")
        return callback()

    def _handle_nsfw_step(self, account_id: int, config: Dict[str, Any], proxy_url: Optional[str], signup_result: GrokSignupResult):
        try:
            self._run_step(
                account_id,
                "enable_nsfw",
                lambda: self.nsfw_service.enable(
                    task_config=config,
                    sso_token=signup_result.grok_sso_token,
                    sso_rw_token=signup_result.grok_sso_rw_token,
                    impersonate=signup_result.impersonate,
                    user_agent=signup_result.user_agent,
                    proxy_url=proxy_url,
                    extra_cookies=signup_result.grok_cookies,
                ),
            )
            self._update_account(account_id, nsfw_enabled=True)
        except NsfwServiceError as exc:
            self._update_account(account_id, nsfw_enabled=False)
            self._log(f"Account #{account_id} skipped NSFW enable: {exc}")

    def _send_verification_code(self, email: str, config: Dict[str, Any]) -> Dict[str, Any]:
        if config.get("mock_mode"):
            return {"challenge_id": f"mock-{uuid.uuid4().hex}"}
        return self._get_signup_client().send_verification_code(email)

    def _verify_code(self, email: str, code: str, challenge: Dict[str, Any], config: Dict[str, Any]) -> Dict[str, Any]:
        if config.get("mock_mode"):
            return {"verified": True, "verification_id": f"mock-{uuid.uuid4().hex}"}
        return self._get_signup_client().verify_code(email, code)

    def _sign_up(
        self,
        email: str,
        code: str,
        captcha_token: str,
        verification: Dict[str, Any],
        config: Dict[str, Any],
    ) -> Any:
        if config.get("mock_mode"):
            return f"sso_{secrets.token_urlsafe(24)}"
        return self._get_signup_client().sign_up(email, code, captcha_token)

    @staticmethod
    def _follow_set_cookie_chain(response) -> Optional[str]:
        return None

    def _create_signup_client(self, task: GrokRegisterTask) -> GrokSignupClient:
        bootstrap = self._signup_bootstrap
        if not bootstrap:
            signup_url = str((task.config or {}).get("signup_page_url") or DEFAULT_SIGNUP_URL).strip() or DEFAULT_SIGNUP_URL
            bootstrap = discover_signup_bootstrap(proxy_url=task.proxy, signup_url=signup_url)
            self._signup_bootstrap = bootstrap
        return GrokSignupClient(proxy_url=task.proxy, bootstrap=bootstrap)

    def _get_signup_client(self) -> GrokSignupClient:
        client = getattr(self._thread_state, "signup_client", None)
        if client:
            return client
        raise GrokWorkflowConfigurationError("Grok sign-up client is not initialized.")

    def _resolve_turnstile_site_key(self, config: Dict[str, Any]) -> str:
        value = str(config.get("turnstile_site_key") or (self._signup_bootstrap.site_key if self._signup_bootstrap else "")).strip()
        if value:
            return value
        raise GrokWorkflowConfigurationError("turnstile site key is not available.")

    def _resolve_signup_page_url(self, config: Dict[str, Any]) -> str:
        value = str(
            config.get("signup_page_url")
            or config.get("signup_url")
            or (self._signup_bootstrap.signup_url if self._signup_bootstrap else "")
        ).strip()
        if value:
            return value
        raise GrokWorkflowConfigurationError("signup page url is not available.")

    def _normalize_signup_result(self, signup_output: Any) -> GrokSignupResult:
        if isinstance(signup_output, GrokSignupResult):
            return signup_output

        token = str(signup_output or "").strip()
        if not token:
            raise GrokWorkflowError("Sign-up succeeded but no SSO token was found.")

        client = getattr(self._thread_state, "signup_client", None)
        impersonate = client.impersonate if client else DEFAULT_IMPERSONATE
        user_agent = client.user_agent if client else ""
        return GrokSignupResult(
            sso_token=token,
            sso_rw_token=token,
            grok_sso_token=token,
            grok_sso_rw_token=token,
            grok_cookies={},
            impersonate=impersonate,
            user_agent=user_agent,
        )

    def _ensure_accounts(self, task: GrokRegisterTask):
        if task.accounts:
            return
        with get_db() as db:
            for index in range(task.target_count):
                grok_crud.create_grok_account(db, grok_task_id=task.id, order_index=index)

    def _refresh_counters(self):
        task = self._get_task(required=False)
        if not task:
            return
        success_count = sum(1 for item in task.accounts if item.status == "completed")
        failed_count = sum(1 for item in task.accounts if item.status == "failed")
        self._update_task_fields(success_count=success_count, failed_count=failed_count)
        self._sync_status_snapshot()

    def _finalize_task(self):
        task = self._get_task(required=True)
        if task_manager.is_cancelled(self.task_uuid):
            raise GrokWorkflowCancelledError(self.task_uuid)
        result = {
            "success_count": task.success_count,
            "failed_count": task.failed_count,
            "total_count": len(task.accounts),
        }
        status = "completed" if task.success_count > 0 or task.failed_count == 0 else "failed"
        error_message = None if status == "completed" else "All Grok registrations failed."
        self._set_task_status(status, completed_at=datetime.utcnow(), error_message=error_message, result=result)

    def _get_task(self, *, required: bool) -> Optional[GrokRegisterTask]:
        with get_db() as db:
            task = grok_crud.get_grok_task(db, self.task_uuid)
            if task:
                task.accounts
                return task
        if required:
            raise GrokWorkflowError("Grok task not found.")
        return None

    def _reload_account(self, account_id: int) -> Optional[GrokRegisterAccount]:
        with get_db() as db:
            return grok_crud.get_grok_account(db, account_id)

    def _set_task_status(self, status: str, **kwargs):
        self._update_task_fields(status=status, **kwargs)
        self._sync_status_snapshot()

    def _update_task_fields(self, **kwargs):
        with get_db() as db:
            grok_crud.update_grok_task(db, self.task_uuid, **kwargs)

    def _update_account(self, account_id: int, **kwargs):
        with get_db() as db:
            grok_crud.update_grok_account(db, account_id, **kwargs)

    def _log(self, message: str):
        payload = f"[{datetime.now().strftime('%H:%M:%S')}] {message}"
        task_manager.add_log(self.task_uuid, payload)
        with get_db() as db:
            grok_crud.append_grok_task_log(db, self.task_uuid, payload)

    def _sync_status_snapshot(self, runtime_message: Optional[str] = None):
        task = self._get_task(required=False)
        if not task:
            return
        runtime_status = task_manager.get_status(self.task_uuid) or {}
        if runtime_message is not None:
            runtime_status["runtime_message"] = runtime_message
        snapshot = build_grok_response(task, runtime_status)
        task_manager.update_status(
            self.task_uuid,
            snapshot["status"],
            snapshot=snapshot,
            runtime_message=snapshot.get("runtime_message"),
        )

    def _raise_if_cancelled(self):
        if task_manager.is_cancelled(self.task_uuid):
            raise GrokWorkflowCancelledError(self.task_uuid)

    def _cancel_task(self, message: str):
        with get_db() as db:
            task = grok_crud.get_grok_task(db, self.task_uuid)
            if not task:
                return
            for account in task.accounts:
                if account.status not in {"completed", "failed"}:
                    account.status = "cancelled"
                    account.step = "cancelled"
                    account.completed_at = datetime.utcnow()
            task.status = "cancelled"
            task.completed_at = datetime.utcnow()
            task.error_message = message
            db.commit()
        self._log(message)
        self._sync_status_snapshot(runtime_message=message)

    def _fail_task(self, message: str):
        with get_db() as db:
            task = grok_crud.get_grok_task(db, self.task_uuid)
            if not task:
                return
            task.status = "failed"
            task.error_message = message
            task.completed_at = datetime.utcnow()
            db.commit()
        self._log(f"Task failed: {message}")
        self._sync_status_snapshot(runtime_message=message)
