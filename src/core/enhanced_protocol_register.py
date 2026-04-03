"""Adapters for protocol-based registration engines."""

from __future__ import annotations

import logging
from datetime import datetime
from typing import Any, Callable, Dict, Optional

from .anyauto.register_flow import AnyAutoRegistrationEngine
from .openai.team_invitation import (
    resolve_session_cookie_from_cookie_store,
    serialize_cookie_store,
)
from .register import RegistrationCancelledError, RegistrationResult
from ..config.settings import get_settings
from ..database import crud
from ..database.session import get_db
from ..services import BaseEmailService

logger = logging.getLogger(__name__)


class EnhancedProtocolRegistrationEngine:
    """Expose the anyauto protocol flow through the legacy engine interface."""

    def __init__(
        self,
        email_service: BaseEmailService,
        proxy_url: Optional[str] = None,
        callback_logger: Optional[Callable[[str], None]] = None,
        task_uuid: Optional[str] = None,
        check_cancelled: Optional[Callable[[], bool]] = None,
        max_retries: int = 3,
        registration_flow_key: str = "protocol.enhanced",
        extra_config: Optional[Dict[str, Any]] = None,
    ):
        self.email_service = email_service
        self.proxy_url = proxy_url
        self.callback_logger = callback_logger or (lambda msg: logger.info(msg))
        self.task_uuid = task_uuid
        self._check_cancelled = check_cancelled or (lambda: False)
        self._cancelled = False
        self.logs: list[str] = []
        self.email_info: Optional[Dict[str, Any]] = None
        self.registration_flow_key = str(registration_flow_key or "protocol.enhanced").strip() or "protocol.enhanced"
        self.extra_config = dict(extra_config or {})
        # 默认启用 adaptive recovery：400 后探测服务端真实状态，捕捉"假性失败"
        self.extra_config.setdefault("adaptive_register_recovery", True)
        self._engine = AnyAutoRegistrationEngine(
            email_service=email_service,
            proxy_url=proxy_url,
            callback_logger=self._log,
            max_retries=max_retries,
            browser_mode="protocol",
            extra_config=self.extra_config,
            check_cancelled=self._is_cancel_requested,
        )

    def _is_cancel_requested(self) -> bool:
        try:
            external_cancelled = bool(self._check_cancelled())
        except Exception:
            external_cancelled = False
        return self._cancelled or external_cancelled

    def _raise_if_cancelled(self, reason: str = "任务已取消") -> None:
        if self._is_cancel_requested():
            raise RegistrationCancelledError(reason)

    def _log(self, message: str, level: str = "info") -> None:
        timestamp = datetime.now().strftime("%H:%M:%S")
        log_message = f"[{timestamp}] {message}"
        self.logs.append(log_message)

        if self.callback_logger:
            self.callback_logger(log_message)

        if self.task_uuid:
            try:
                with get_db() as db:
                    crud.append_task_log(db, self.task_uuid, log_message)
            except Exception as exc:
                logger.warning("记录协议任务日志失败: %s", exc)

        getattr(logger, level, logger.info)(message)

    @staticmethod
    def _format_add_phone_required_message(subject: str) -> str:
        prefix = str(subject or "").strip() or "当前账号"
        return f"{prefix}进入 add_phone 页面，需要补充手机号后才能继续授权"

    @staticmethod
    def _looks_like_add_phone(payload: Dict[str, Any]) -> bool:
        metadata = dict(payload.get("metadata") or {})
        if metadata.get("phone_verification_required") or metadata.get("token_pending"):
            return True

        text_parts = [
            payload.get("error_message"),
            metadata.get("oauth_error"),
        ]
        haystack = " ".join(str(part or "") for part in text_parts).lower()
        return any(
            marker in haystack
            for marker in ("add_phone", "add-phone", "phone verification", "phone required")
        )

    def cancel(self) -> None:
        self._cancelled = True
        inner_session = getattr(self._engine, "session", None)
        if inner_session is not None:
            try:
                inner_session.close()
            except Exception:
                pass
        self._log("任务已取消，协议注册将尽快退出", "warning")

    def run(self) -> RegistrationResult:
        result = RegistrationResult(success=False, logs=self.logs, source="register")

        try:
            self._raise_if_cancelled()
            payload = dict(self._engine.run() or {})
            self.email_info = dict(getattr(self._engine, "email_info", None) or {}) or None

            result.logs = self.logs
            result.email = str(payload.get("email") or getattr(self._engine, "email", "") or "").strip()
            result.password = str(payload.get("password") or getattr(self._engine, "password", "") or "").strip()
            result.account_id = str(payload.get("account_id") or "").strip()
            result.workspace_id = str(payload.get("workspace_id") or "").strip()
            result.access_token = str(payload.get("access_token") or "").strip()
            result.refresh_token = str(payload.get("refresh_token") or "").strip()
            result.id_token = str(payload.get("id_token") or "").strip()
            result.session_token = str(payload.get("session_token") or "").strip()
            result.cookies = str(payload.get("cookies") or "").strip()

            cookie_store = getattr(getattr(self._engine, "session", None), "cookies", None)
            if not result.cookies:
                result.cookies = serialize_cookie_store(cookie_store)
            if not result.session_token:
                session_cookie = resolve_session_cookie_from_cookie_store(cookie_store)
                if session_cookie:
                    result.session_token = str(session_cookie.get("value") or "").strip()

            metadata = dict(payload.get("metadata") or {})
            metadata.setdefault("email_service", self.email_service.service_type.value)
            metadata.setdefault("proxy_used", self.proxy_url)
            metadata.setdefault("registered_at", datetime.now().isoformat())
            metadata["registration_flow"] = self.registration_flow_key
            result.metadata = metadata

            self._raise_if_cancelled()
            if payload.get("success") and not self._looks_like_add_phone(payload):
                result.success = True
                return result

            if self._looks_like_add_phone(payload):
                result.error_code = "add_phone_required"
                result.error_message = self._format_add_phone_required_message("当前账号")
                return result

            result.error_message = str(payload.get("error_message") or "协议注册失败")
            return result
        except RegistrationCancelledError as exc:
            result.error_message = str(exc) or "任务已取消"
            return result
        except Exception as exc:
            if "任务已取消" in str(exc):
                result.error_message = str(exc) or "任务已取消"
                return result
            self._log(f"协议注册失败: {exc}", "error")
            result.error_message = str(exc)
            return result

    def save_to_database(self, result: RegistrationResult) -> bool:
        if not result.success:
            return False

        try:
            settings = get_settings()
            with get_db() as db:
                account = crud.create_account(
                    db,
                    email=result.email,
                    password=result.password,
                    client_id=settings.openai_client_id,
                    session_token=result.session_token,
                    cookies=result.cookies,
                    email_service=self.email_service.service_type.value,
                    email_service_id=(self.email_info or {}).get("service_id"),
                    account_id=result.account_id,
                    workspace_id=result.workspace_id,
                    access_token=result.access_token,
                    refresh_token=result.refresh_token,
                    id_token=result.id_token,
                    proxy_used=self.proxy_url,
                    extra_data=result.metadata,
                    source=result.source,
                )
                self._log(f"账号已存进数据库，ID: {account.id}")
                return True
        except Exception as exc:
            self._log(f"保存到数据库失败: {exc}", "error")
            return False


class AdaptiveProtocolRegistrationEngine(EnhancedProtocolRegistrationEngine):
    """Test-only protocol flow that enables same-session recovery after generic 400s."""

    def __init__(
        self,
        email_service: BaseEmailService,
        proxy_url: Optional[str] = None,
        callback_logger: Optional[Callable[[str], None]] = None,
        task_uuid: Optional[str] = None,
        check_cancelled: Optional[Callable[[], bool]] = None,
        max_retries: int = 3,
        extra_config: Optional[Dict[str, Any]] = None,
    ):
        merged_config = dict(extra_config or {})
        merged_config["adaptive_register_recovery"] = True
        super().__init__(
            email_service=email_service,
            proxy_url=proxy_url,
            callback_logger=callback_logger,
            task_uuid=task_uuid,
            check_cancelled=check_cancelled,
            max_retries=max_retries,
            registration_flow_key="protocol.adaptive",
            extra_config=merged_config,
        )
