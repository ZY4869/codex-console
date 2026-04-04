"""
注册流程引擎
从 main.py 中提取并重构的注册流程
"""

import asyncio
import re
import json
import time
import logging
import secrets
import string
from urllib.parse import quote, urljoin, urlsplit, urlunsplit
from typing import Optional, Dict, Any, Tuple, Callable
from dataclasses import dataclass
from datetime import datetime

from curl_cffi import requests as cffi_requests

from .anyauto.utils import (
    FlowState,
    build_browser_headers,
    describe_flow_state,
    extract_flow_state,
    generate_datadog_trace,
    seed_oai_device_cookie,
)
from .openai.team_invitation import (
    _decode_jwt_payload,
    build_session_cookie_header,
    list_cookie_names,
    resolve_session_cookie_from_cookie_store,
    serialize_cookie_store,
)
from .openai.oauth import OAuthManager, OAuthStart
from .http_client import OpenAIHTTPClient, HTTPClientError
from ..services import EmailServiceFactory, BaseEmailService, EmailServiceType
from ..database import crud
from ..database.session import get_db
from ..config.constants import (
    OPENAI_API_ENDPOINTS,
    OPENAI_PAGE_TYPES,
    generate_random_user_info,
    OTP_CODE_PATTERN,
    DEFAULT_PASSWORD_LENGTH,
    PASSWORD_SPECIAL_CHARSET,
    PASSWORD_CHARSET,
    AccountStatus,
    TaskStatus,
)
from ..config.settings import get_settings


logger = logging.getLogger(__name__)


class RegistrationCancelledError(asyncio.CancelledError):
    """注册任务收到取消请求时抛出的协作式取消异常。"""


@dataclass
class RegistrationResult:
    """注册结果"""
    success: bool
    email: str = ""
    password: str = ""  # 注册密码
    account_id: str = ""
    workspace_id: str = ""
    access_token: str = ""
    refresh_token: str = ""
    id_token: str = ""
    session_token: str = ""  # 会话令牌
    cookies: str = ""
    error_code: str = ""
    error_message: str = ""
    logs: list = None
    metadata: dict = None
    source: str = "register"  # 'register' 或 'login'，区分账号来源

    def to_dict(self) -> Dict[str, Any]:
        """转换为字典"""
        return {
            "success": self.success,
            "email": self.email,
            "password": self.password,
            "account_id": self.account_id,
            "workspace_id": self.workspace_id,
            "access_token": self.access_token[:20] + "..." if self.access_token else "",
            "refresh_token": self.refresh_token[:20] + "..." if self.refresh_token else "",
            "id_token": self.id_token[:20] + "..." if self.id_token else "",
            "session_token": self.session_token[:20] + "..." if self.session_token else "",
            "cookies": "[stored]" if self.cookies else "",
            "error_code": self.error_code,
            "error_message": self.error_message,
            "logs": self.logs or [],
            "metadata": self.metadata or {},
            "source": self.source,
        }


MISSING_REFRESH_TOKEN_ERROR_CODE = "missing_refresh_token"


def has_required_refresh_token(refresh_token: Optional[str]) -> bool:
    return bool(str(refresh_token or "").strip())


def build_missing_refresh_token_error(subject: str = "当前账号") -> str:
    prefix = str(subject or "").strip() or "当前账号"
    return f"{prefix}未拿到 refresh_token (RT)，按严格策略判定为失败"


def enforce_refresh_token_requirement(
    result: RegistrationResult,
    *,
    subject: str = "当前账号",
) -> bool:
    if has_required_refresh_token(getattr(result, "refresh_token", "")):
        return True

    result.success = False
    result.error_code = MISSING_REFRESH_TOKEN_ERROR_CODE
    result.error_message = build_missing_refresh_token_error(subject)
    return False


@dataclass
class SignupFormResult:
    """提交注册表单的结果"""
    success: bool
    page_type: str = ""  # 响应中的 page.type 字段
    is_existing_account: bool = False  # 是否为已注册账号
    response_data: Dict[str, Any] = None  # 完整的响应数据
    error_message: str = ""


@dataclass
class WorkspaceSelectionResult:
    """Workspace 选择结果"""
    continue_url: str = ""
    error_message: str = ""
    status_code: int = 0
    error_code: str = ""
    error_detail: str = ""

    @property
    def success(self) -> bool:
        return bool(self.continue_url)


@dataclass
class WorkspaceLookupResult:
    """Workspace Cookie 解析结果"""
    workspace_id: str = ""
    error_message: str = ""
    reason_code: str = ""

    @property
    def success(self) -> bool:
        return bool(self.workspace_id)


class RegistrationEngine:
    """
    注册引擎
    负责协调邮箱服务、OAuth 流程和 OpenAI API 调用
    """

    def __init__(
        self,
        email_service: BaseEmailService,
        proxy_url: Optional[str] = None,
        callback_logger: Optional[Callable[[str], None]] = None,
        task_uuid: Optional[str] = None,
        check_cancelled: Optional[Callable[[], bool]] = None,
    ):
        """
        初始化注册引擎

        Args:
            email_service: 邮箱服务实例
            proxy_url: 代理 URL
            callback_logger: 日志回调函数
            task_uuid: 任务 UUID（用于数据库记录）
        """
        self.email_service = email_service
        self.proxy_url = proxy_url
        self.callback_logger = callback_logger or (lambda msg: logger.info(msg))
        self.task_uuid = task_uuid
        self._check_cancelled = check_cancelled or (lambda: False)

        # 创建 HTTP 客户端
        self.http_client = OpenAIHTTPClient(proxy_url=proxy_url)

        # 创建 OAuth 管理器
        settings = get_settings()
        self.oauth_manager = OAuthManager(
            client_id=settings.openai_client_id,
            auth_url=settings.openai_auth_url,
            token_url=settings.openai_token_url,
            redirect_uri=settings.openai_redirect_uri,
            scope=settings.openai_scope,
            proxy_url=proxy_url  # 传递代理配置
        )

        # 状态变量
        self.email: Optional[str] = None
        self.password: Optional[str] = None  # 注册密码
        self.email_info: Optional[Dict[str, Any]] = None
        self.oauth_start: Optional[OAuthStart] = None
        self.session: Optional[cffi_requests.Session] = None
        self.session_token: Optional[str] = None  # 会话令牌
        self.logs: list = []
        self._otp_sent_at: Optional[float] = None  # OTP 发送时间戳
        self._is_existing_account: bool = False  # 是否为已注册账号（用于自动登录）
        self._consent_skip_otp: bool = False  # Codex consent 流程跳过 OTP
        self._target_workspace_id: Optional[str] = None  # 指定目标 workspace（用于 Team 直接选择）
        self._token_acquisition_requires_login: bool = False  # 新注册账号需要二次登录拿 token

        self._password_generated_for_registration: bool = False
        self._registration_conflict_detected: bool = False
        self._registration_conflict_message: str = ""
        self._recovered_workspace_id: Optional[str] = None
        self._session_exchange_token_info: Optional[Dict[str, Any]] = None
        self._last_register_password_error: Optional[str] = None
        self._last_register_password_context: Dict[str, Any] = {}

    def _is_cancel_requested(self) -> bool:
        try:
            return bool(self._check_cancelled())
        except Exception:
            return False

    def _raise_if_cancelled(self, reason: str = "任务已取消") -> None:
        if self._is_cancel_requested():
            raise RegistrationCancelledError(reason)

    def _sleep_interruptible(self, seconds: float) -> None:
        remaining = max(0.0, float(seconds or 0.0))
        while remaining > 0:
            self._raise_if_cancelled("任务在等待阶段被取消")
            chunk = min(0.2, remaining)
            time.sleep(chunk)
            remaining -= chunk

    def _log(self, message: str, level: str = "info"):
        """记录日志"""
        timestamp = datetime.now().strftime("%H:%M:%S")
        log_message = f"[{timestamp}] {message}"

        # 添加到日志列表
        self.logs.append(log_message)

        # 调用回调函数
        if self.callback_logger:
            self.callback_logger(log_message)

        # 记录到数据库（如果有关联任务）
        if self.task_uuid:
            try:
                with get_db() as db:
                    crud.append_task_log(db, self.task_uuid, log_message)
            except Exception as e:
                logger.warning(f"记录任务日志失败: {e}")

        # 根据级别记录到日志系统
        if level == "error":
            logger.error(message)
        elif level == "warning":
            logger.warning(message)
        else:
            logger.info(message)

    def _build_auth_headers(
        self,
        referer: str,
        *,
        target_url: Optional[str] = None,
        accept: str = "application/json",
        content_type: Optional[str] = "application/json",
        add_datadog_trace: bool = False,
        extra_headers: Optional[Dict[str, str]] = None,
    ) -> Dict[str, str]:
        """构建接近真实浏览器的 auth.openai.com 请求头。"""
        merged_headers = self._get_merged_browser_headers()
        user_agent = merged_headers.get("user-agent") or "Mozilla/5.0"
        sec_ch_ua = merged_headers.get("sec-ch-ua") or self._build_default_sec_ch_ua(user_agent)
        accept_language = merged_headers.get("accept-language") or "en-US,en;q=0.9"
        request_url = str(target_url or referer or "https://auth.openai.com").strip()

        headers = build_browser_headers(
            url=request_url,
            user_agent=user_agent,
            sec_ch_ua=sec_ch_ua or None,
            accept=accept,
            accept_language=accept_language,
            referer=referer,
            origin="https://auth.openai.com",
            content_type=content_type,
            fetch_site="same-origin",
            extra_headers=extra_headers,
        )

        for header_name in ("Accept-Encoding", "Connection"):
            header_value = self._get_header_value(merged_headers, header_name)
            if header_value and header_name not in headers:
                headers[header_name] = header_value

        if add_datadog_trace:
            headers.update(generate_datadog_trace())
        return headers

    def _get_merged_browser_headers(self) -> Dict[str, str]:
        merged_headers: Dict[str, str] = {}
        default_headers = getattr(self.http_client, "default_headers", {}) or {}
        session_headers = getattr(self.session, "headers", {}) or {}
        merged_headers.update({str(key).lower(): str(value) for key, value in dict(default_headers).items()})
        merged_headers.update({str(key).lower(): str(value) for key, value in dict(session_headers).items()})
        return merged_headers

    def _get_header_value(self, headers: Dict[str, str], name: str) -> str:
        return str((headers or {}).get(str(name or "").strip().lower()) or "")

    def _build_default_sec_ch_ua(self, user_agent: str) -> str:
        match = re.search(r"Chrome/(\d+)", str(user_agent or ""))
        if not match:
            return ""
        major = match.group(1)
        return f'"Chromium";v="{major}", "Google Chrome";v="{major}", "Not.A/Brand";v="99"'

    def _has_browser_like_headers(self, headers: Dict[str, str]) -> bool:
        required = {
            "origin",
            "referer",
            "sec-fetch-site",
            "accept-language",
            "user-agent",
            "traceparent",
            "x-datadog-trace-id",
        }
        lowered = {str(key).lower() for key in (headers or {}).keys()}
        return required.issubset(lowered)

    def _extract_response_flow_state(self, response, fallback_url: str) -> FlowState:
        current_url = str(getattr(response, "url", "") or fallback_url)
        try:
            payload = response.json() or {}
        except Exception:
            payload = {}
        return extract_flow_state(
            data=payload,
            current_url=current_url,
            auth_base="https://auth.openai.com",
        )

    def _build_register_password_context(self, response, headers: Dict[str, str]) -> Dict[str, Any]:
        error_details = self._extract_openai_error_details(response)
        flow_state = self._extract_response_flow_state(
            response,
            fallback_url="https://auth.openai.com/create-account/password",
        )
        return {
            "status_code": int(getattr(response, "status_code", 0) or 0),
            "error_code": error_details["code"],
            "error_type": error_details["type"],
            "error_message": error_details["message"],
            "flow_state": flow_state,
            "cookie_names": list_cookie_names(getattr(self.session, "cookies", None)),
            "browser_headers_enabled": self._has_browser_like_headers(headers),
        }

    def _probe_register_password_flow_state(self) -> FlowState:
        probe_url = "https://auth.openai.com/create-account/password"
        response = self.session.get(
            probe_url,
            headers=self._build_auth_headers(
                probe_url,
                target_url=probe_url,
                accept="text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
                content_type=None,
            ),
            allow_redirects=True,
            timeout=30,
        )
        flow_state = self._extract_response_flow_state(response, fallback_url=probe_url)
        self._log(f"密码注册失败后刷新 auth step: {describe_flow_state(flow_state)}")
        return flow_state

    def _handle_register_password_flow_state(self, flow_state: FlowState) -> Optional[bool]:
        page_type = str(flow_state.page_type or "").strip()
        if not page_type or page_type == OPENAI_PAGE_TYPES["PASSWORD_REGISTRATION"]:
            return None

        if page_type == OPENAI_PAGE_TYPES["EMAIL_OTP_VERIFICATION"]:
            self._otp_sent_at = self._otp_sent_at or time.time()
            self._is_existing_account = True
            self._log("密码注册接口返回泛化 400，但授权步骤已前进到邮箱验证码页，切换到已有账号分支", "warning")
            return True

        if page_type == OPENAI_PAGE_TYPES["CODEX_CONSENT"]:
            self._consent_skip_otp = True
            self._is_existing_account = True
            self._log("密码注册接口返回泛化 400，但授权步骤已前进到 Codex 授权页，切换到授权分支", "warning")
            return True

        if page_type == OPENAI_PAGE_TYPES["LOGIN_PASSWORD"]:
            self._registration_conflict_detected = True
            self._registration_conflict_message = "密码注册失败后当前授权步骤已漂移到登录密码页，疑似该邮箱已存在账号"
            self._log(self._registration_conflict_message, "warning")
            return False

        self._log(f"密码注册失败后当前授权步骤漂移到未预期页面: {page_type}", "warning")
        return False

    def _should_retry_register_password(self, context: Dict[str, Any]) -> bool:
        error_text = " ".join(
            str(part or "").strip()
            for part in (
                context.get("error_code"),
                context.get("error_type"),
                context.get("error_message"),
                self._last_register_password_error,
            )
            if str(part or "").strip()
        ).lower()
        retryable_markers = (
            "failed to create account",
            "create account",
            "invalid_request_error",
            "http 400",
        )
        return int(context.get("status_code") or 0) == 400 and any(marker in error_text for marker in retryable_markers)

    def _generate_password(self, length: int = DEFAULT_PASSWORD_LENGTH) -> str:
        """生成随机密码"""
        length = max(8, int(length or DEFAULT_PASSWORD_LENGTH))
        password_chars = [
            secrets.choice(string.ascii_lowercase),
            secrets.choice(string.ascii_uppercase),
            secrets.choice(string.digits),
            secrets.choice(PASSWORD_SPECIAL_CHARSET),
        ]
        password_chars.extend(secrets.choice(PASSWORD_CHARSET) for _ in range(length - len(password_chars)))
        secrets.SystemRandom().shuffle(password_chars)
        return ''.join(password_chars)

    def _check_ip_location(self) -> Tuple[bool, Optional[str]]:
        """检查 IP 地理位置"""
        self._raise_if_cancelled("任务已取消，跳过 IP 地理位置检查")
        try:
            return self.http_client.check_ip_location()
        except Exception as e:
            self._log(f"检查 IP 地理位置失败: {e}", "error")
            return False, None

    def _create_email(self) -> bool:
        """创建邮箱"""
        self._raise_if_cancelled("任务已取消，跳过邮箱创建")
        try:
            self._log(f"正在创建 {self.email_service.service_type.value} 邮箱，先给新账号整个收件箱...")
            self.email_info = self.email_service.create_email()

            if not self.email_info or "email" not in self.email_info:
                self._log("创建邮箱失败: 返回信息不完整", "error")
                return False

            self.email = self.email_info["email"]
            self._log(f"邮箱已就位，地址新鲜出炉: {self.email}")
            return True

        except Exception as e:
            self._log(f"创建邮箱失败: {e}", "error")
            return False

    def _start_oauth(self) -> bool:
        """开始 OAuth 流程"""
        self._raise_if_cancelled("任务已取消，跳过 OAuth 初始化")
        try:
            self._log("开始 OAuth 授权流程，去门口刷个脸...")
            self.oauth_start = self.oauth_manager.start_oauth()
            self._log(f"OAuth URL 已备好，通道已经打开: {self.oauth_start.auth_url[:80]}...")
            return True
        except Exception as e:
            self._log(f"生成 OAuth URL 失败: {e}", "error")
            return False

    def _init_session(self) -> bool:
        """初始化会话"""
        self._raise_if_cancelled("任务已取消，跳过会话初始化")
        try:
            self.session = self.http_client.session
            return True
        except Exception as e:
            self._log(f"初始化会话失败: {e}", "error")
            return False

    def _get_device_id(self) -> Optional[str]:
        """获取 Device ID"""
        self._raise_if_cancelled("任务已取消，停止获取 Device ID")
        if not self.oauth_start:
            return None

        max_attempts = 3
        for attempt in range(1, max_attempts + 1):
            self._raise_if_cancelled("任务已取消，停止获取 Device ID")
            try:
                if not self.session:
                    self.session = self.http_client.session

                response = self.session.get(
                    self.oauth_start.auth_url,
                    timeout=20
                )
                did = self.session.cookies.get("oai-did")

                if did:
                    self._log(f"Device ID: {did}")
                    return did

                self._log(
                    f"获取 Device ID 失败: 未返回 oai-did Cookie (HTTP {response.status_code}, 第 {attempt}/{max_attempts} 次)",
                    "warning" if attempt < max_attempts else "error"
                )
            except Exception as e:
                self._log(
                    f"获取 Device ID 失败: {e} (第 {attempt}/{max_attempts} 次)",
                    "warning" if attempt < max_attempts else "error"
                )

            if attempt < max_attempts:
                self._sleep_interruptible(attempt)
                self.http_client.close()
                self.session = self.http_client.session

        return None

    def _check_sentinel(self, did: str) -> Optional[str]:
        """检查 Sentinel 拦截"""
        try:
            sen_token = self.http_client.check_sentinel(did)
            if sen_token:
                self._log(f"Sentinel token 获取成功")
                return sen_token
            self._log("Sentinel 检查失败: 未获取到 token", "warning")
            return None

        except Exception as e:
            self._log(f"Sentinel 检查异常: {e}", "warning")
            return None

    def _submit_auth_start(
        self,
        did: str,
        sen_token: Optional[str],
        *,
        screen_hint: str,
        referer: str,
        log_label: str,
        record_existing_account: bool = True,
        allow_invalid_step_retry: bool = True,
    ) -> SignupFormResult:
        """
        提交授权入口表单

        Returns:
            SignupFormResult: 提交结果，包含账号状态判断
        """
        try:
            request_body = json.dumps({
                "username": {
                    "value": self.email,
                    "kind": "email",
                },
                "screen_hint": screen_hint,
            })

            headers = self._build_auth_headers(referer)

            if sen_token:
                sentinel = json.dumps({
                    "p": "",
                    "t": "",
                    "c": sen_token,
                    "id": did,
                    "flow": "authorize_continue",
                })
                headers["openai-sentinel-token"] = sentinel

            response = self.session.post(
                OPENAI_API_ENDPOINTS["signup"],
                headers=headers,
                data=request_body,
            )

            self._log(f"{log_label}状态: {response.status_code}")

            if response.status_code != 200:
                error_code, error_message = self._extract_openai_error(response)
                if allow_invalid_step_retry and error_code == "invalid_auth_step":
                    self._log(
                        f"{log_label} 命中了失效授权步骤，正在重建授权上下文后重试一次",
                        "warning",
                    )
                    self._reset_auth_flow()
                    retry_did, retry_sen_token = self._prepare_authorize_flow(f"{log_label} 重试")
                    if not retry_did:
                        return SignupFormResult(
                            success=False,
                            error_message="授权步骤失效，重建授权流程时获取 Device ID 失败",
                        )
                    if not retry_sen_token:
                        return SignupFormResult(
                            success=False,
                            error_message="授权步骤失效，重建授权流程时 Sentinel 校验失败",
                        )
                    return self._submit_auth_start(
                        retry_did,
                        retry_sen_token,
                        screen_hint=screen_hint,
                        referer=referer,
                        log_label=log_label,
                        record_existing_account=record_existing_account,
                        allow_invalid_step_retry=False,
                    )

                if error_code or error_message:
                    self._log(
                        f"{log_label} 返回错误码: {error_code or 'unknown'}, 消息: {error_message or 'unknown'}",
                        "warning",
                    )
                return SignupFormResult(
                    success=False,
                    error_message=f"HTTP {response.status_code}: {response.text[:200]}"
                )

            # 解析响应判断账号状态
            try:
                response_data = response.json()
                page_type = response_data.get("page", {}).get("type", "")
                self._log(f"响应页面类型: {page_type}")

                is_existing = page_type == OPENAI_PAGE_TYPES["EMAIL_OTP_VERIFICATION"]

                if is_existing:
                    self._otp_sent_at = time.time()
                    if record_existing_account:
                        self._log(f"检测到已注册账号，将自动切换到登录流程")
                        self._is_existing_account = True
                    else:
                        self._log("登录流程已触发，等待系统自动发送的验证码")

                return SignupFormResult(
                    success=True,
                    page_type=page_type,
                    is_existing_account=is_existing,
                    response_data=response_data
                )

            except Exception as parse_error:
                self._log(f"解析响应失败: {parse_error}", "warning")
                # 无法解析，默认成功
                return SignupFormResult(success=True)

        except Exception as e:
            self._log(f"{log_label}失败: {e}", "error")
            return SignupFormResult(success=False, error_message=str(e))

    def _extract_openai_error_details(self, response) -> Dict[str, str]:
        """提取 OpenAI 错误详情，便于日志和恢复逻辑复用。"""
        try:
            payload = response.json() or {}
        except Exception:
            return {"code": "", "message": "", "type": ""}

        error = payload.get("error") or {}
        return {
            "code": str(error.get("code") or "").strip(),
            "message": str(error.get("message") or "").strip(),
            "type": str(error.get("type") or "").strip(),
        }

    def _extract_openai_error(self, response) -> Tuple[str, str]:
        """提取 OpenAI 错误码和错误消息，便于细分恢复逻辑。"""
        details = self._extract_openai_error_details(response)
        return details["code"], details["message"]

    def _is_existing_account_registration_conflict(self, error_code: str, error_message: str) -> bool:
        message = str(error_message or "").strip().lower()
        code = str(error_code or "").strip().lower()
        if code == "user_exists":
            return True
        if "already" in message or "exists" in message:
            return True
        return "failed to register username" in message

    def _submit_signup_form(
        self,
        did: str,
        sen_token: Optional[str],
        *,
        record_existing_account: bool = True,
    ) -> SignupFormResult:
        """提交注册入口表单。"""
        return self._submit_auth_start(
            did,
            sen_token,
            screen_hint="signup",
            referer="https://auth.openai.com/create-account",
            log_label="提交注册表单",
            record_existing_account=record_existing_account,
        )

    def _submit_login_start(self, did: str, sen_token: Optional[str]) -> SignupFormResult:
        """提交登录入口表单。"""
        return self._submit_auth_start(
            did,
            sen_token,
            screen_hint="login",
            referer="https://auth.openai.com/log-in",
            log_label="提交登录入口",
            record_existing_account=False,
        )

    def _submit_login_password(self) -> SignupFormResult:
        """提交登录密码，进入邮箱验证码页面。"""
        try:
            response = self.session.post(
                OPENAI_API_ENDPOINTS["password_verify"],
                headers=self._build_auth_headers("https://auth.openai.com/log-in/password"),
                data=json.dumps({"password": self.password}),
            )

            self._log(f"提交登录密码状态: {response.status_code}")

            if response.status_code != 200:
                return SignupFormResult(
                    success=False,
                    error_message=f"HTTP {response.status_code}: {response.text[:200]}"
                )

            response_data = response.json()
            page_type = response_data.get("page", {}).get("type", "")
            self._log(f"登录密码响应页面类型: {page_type}")

            # 如果遇到 Codex 授权同意页，自动提交同意后继续
            if page_type == OPENAI_PAGE_TYPES["CODEX_CONSENT"]:
                self._log("遇到 Codex 授权同意页，自动处理...")
                return self._submit_codex_consent(response_data)

            is_existing = page_type == OPENAI_PAGE_TYPES["EMAIL_OTP_VERIFICATION"]
            if is_existing:
                self._otp_sent_at = time.time()
                self._log("登录密码校验通过，等待系统自动发送的验证码")

            return SignupFormResult(
                success=True,
                page_type=page_type,
                is_existing_account=is_existing,
                response_data=response_data,
            )

        except Exception as e:
            self._log(f"提交登录密码失败: {e}", "error")
            return SignupFormResult(success=False, error_message=str(e))

    def _submit_codex_consent(self, response_data: dict) -> SignupFormResult:
        """处理 Codex 授权同意页：跳过 OTP，标记让 _complete_token_exchange 直接走 workspace + 重定向链。"""
        self._log("Codex consent 流程: 跳过 OTP，直接进入 workspace 选择 + 授权重定向")
        self._consent_skip_otp = True
        return SignupFormResult(
            success=True,
            page_type=OPENAI_PAGE_TYPES["CODEX_CONSENT"],
            is_existing_account=True,
            response_data=response_data,
        )

    def _submit_codex_consent_post(self) -> Tuple[bool, Optional[str]]:
        """POST consent 到 authorize/continue 端点，触发 workspace 分配。

        Returns:
            Tuple[成功标志, consent 响应中的 continue_url（如有）]
        """
        try:
            self._log("提交 Codex 授权同意，触发 workspace 分配...")
            response = self.session.post(
                OPENAI_API_ENDPOINTS["signup"],
                headers=self._build_auth_headers("https://auth.openai.com/sign-in-with-chatgpt/codex/consent"),
                data="{}",
                allow_redirects=False,
            )
            self._log(f"Codex 授权同意响应状态: {response.status_code}")

            # 3xx 重定向：服务端直接返回了授权链的下一跳
            if response.status_code in (301, 302, 303, 307, 308):
                location = str(response.headers.get("Location") or "").strip()
                if location:
                    redirect_url = urljoin(OPENAI_API_ENDPOINTS["signup"], location)
                    self._log(f"Codex 授权同意返回重定向: {redirect_url[:100]}...")
                    return True, redirect_url
                self._log("Codex 授权同意返回重定向但缺少 Location", "warning")
                return True, None

            if response.status_code != 200:
                self._log(f"Codex 授权同意失败: {response.text[:200]}", "warning")
                return False, None

            # 解析 JSON 响应，提取 continue_url
            continue_url = None
            try:
                payload = response.json() or {}
                if isinstance(payload, dict):
                    continue_url = str(payload.get("continue_url") or "").strip() or None
                    page_type = str((payload.get("page") or {}).get("type") or "").strip()
                    if continue_url:
                        self._log(f"Codex 授权同意响应包含 continue_url: {continue_url[:100]}...")
                    elif page_type:
                        self._log(f"Codex 授权同意响应页面类型: {page_type}")
                    else:
                        self._log(f"Codex 授权同意响应字段: {list(payload.keys())}")
            except Exception:
                pass

            return True, continue_url
        except Exception as e:
            self._log(f"提交 Codex 授权同意异常: {e}", "error")
            return False, None

    def _reset_auth_flow(self) -> None:
        """重置会话，准备重新发起 OAuth 流程。"""
        self.http_client.close()
        self.session = None
        self.oauth_start = None
        self.session_token = None
        self._otp_sent_at = None
        self._session_exchange_token_info = None

    def _reset_oauth_context_preserving_session(self) -> None:
        """仅重置 OAuth 上下文，保留当前登录态 cookies。"""
        self.oauth_start = None
        self.session_token = None
        self._otp_sent_at = None
        self._consent_skip_otp = False
        self._session_exchange_token_info = None

    def _prepare_authorize_flow_with_existing_session(self, label: str) -> Tuple[Optional[str], Optional[str]]:
        """保留当前登录态，重建新的 OAuth 授权上下文。"""
        self._log(f"{label}: 保留当前登录态，重建 OAuth 授权上下文...")
        if not self.session and not self._init_session():
            return None, None

        self._reset_oauth_context_preserving_session()
        self._log(f"{label}: OAuth 流程准备开跑，系好鞋带...")
        if not self._start_oauth():
            return None, None

        self._log(f"{label}: 领取新的 Device ID 通行证...")
        did = self._get_device_id()
        if not did:
            return None, None

        self._log(f"{label}: 重新过一道 Sentinel POW 小题...")
        sen_token = self._check_sentinel(did)
        if not sen_token:
            return did, None

        self._log(f"{label}: 新的 OAuth 授权上下文已就绪")
        return did, sen_token

    def _prepare_authorize_flow(self, label: str) -> Tuple[Optional[str], Optional[str]]:
        """初始化当前阶段的授权流程，返回 device id 和 sentinel token。"""
        self._log(f"{label}: 先把会话热热身...")
        if not self._init_session():
            return None, None

        self._log(f"{label}: OAuth 流程准备开跑，系好鞋带...")
        if not self._start_oauth():
            return None, None

        self._log(f"{label}: 领取 Device ID 通行证...")
        did = self._get_device_id()
        if not did:
            return None, None

        self._log(f"{label}: 解一道 Sentinel POW 小题，答对才给进...")
        sen_token = self._check_sentinel(did)
        if not sen_token:
            return did, None

        self._log(f"{label}: Sentinel 点头放行，继续前进")
        return did, sen_token

    def _try_direct_authorize_after_registration(self, result: RegistrationResult) -> bool:
        """注册完成后，尝试直接在当前 OAuth 上下文中完成授权（不二次登录）。"""
        try:
            # 提交 authorize/continue 推进授权流程
            self._log("提交 authorize/continue 推进注册后的授权流程...")
            consent_ok, consent_continue_url = self._submit_codex_consent_post()
            if not consent_ok:
                self._log("authorize/continue 提交失败", "warning")
                return False

            # 尝试获取 workspace
            workspace_lookup = self._get_workspace_lookup()
            workspace_id = workspace_lookup.workspace_id
            if workspace_id:
                result.workspace_id = workspace_id
                self._log(f"直接授权路径拿到 Workspace ID: {workspace_id}")

                # 尝试 workspace/select
                workspace_selection = self._select_workspace(workspace_id)
                if workspace_selection.success:
                    callback_url = self._follow_redirects(workspace_selection.continue_url)
                    if callback_url:
                        token_info = self._handle_oauth_callback(callback_url)
                        if token_info:
                            self._fill_result_from_token_info(result, token_info)
                            if enforce_refresh_token_requirement(result, subject="当前账号"):
                                return True
                            self._log(result.error_message, "warning")
                            return False

            # 尝试从 OAuth 授权入口续上
            if self.oauth_start and self.oauth_start.auth_url:
                self._log("尝试从原始 OAuth 授权入口续上...")
                callback_url = self._try_resume_current_oauth_entry("直接授权")
                if callback_url:
                    token_info = self._handle_oauth_callback(callback_url)
                    if token_info:
                        self._fill_result_from_token_info(result, token_info)
                        if enforce_refresh_token_requirement(result, subject="当前账号"):
                            return True
                        self._log(result.error_message, "warning")
                        return False

            self._log("直接授权路径未能完成 token 获取", "warning")
            return False
        except Exception as e:
            self._log(f"直接授权路径异常: {e}", "warning")
            return False

    def _fill_result_from_token_info(self, result: RegistrationResult, token_info: Dict[str, Any]):
        """将 token 信息填充到结果对象中。"""
        result.account_id = token_info.get("account_id", "")
        result.access_token = token_info.get("access_token", "")
        result.refresh_token = token_info.get("refresh_token", "")
        result.id_token = token_info.get("id_token", "")
        result.password = self.password or ""
        result.source = "login" if self._is_existing_account else "register"
        result.cookies = serialize_cookie_store(getattr(self.session, "cookies", None))
        session_cookie = resolve_session_cookie_from_cookie_store(getattr(self.session, "cookies", None))
        if session_cookie:
            self.session_token = session_cookie["value"]
            result.session_token = session_cookie["value"]
        fallback_st = str(token_info.get("session_token") or "").strip()
        if not result.session_token and fallback_st:
            self.session_token = fallback_st
            result.session_token = fallback_st

    def _complete_via_continue_url(self, continue_url: str, result: RegistrationResult) -> bool:
        """consent 返回 continue_url 时，直接跟随重定向链拿 OAuth 回调，跳过 workspace/select。"""
        self._log("consent 响应自带 continue_url，直接走重定向链获取 OAuth 回调，跳过 workspace/select...")

        # 尝试从 cookie 获取 workspace_id 用于结果填充
        workspace_lookup = self._get_workspace_lookup()
        if workspace_lookup.workspace_id:
            result.workspace_id = workspace_lookup.workspace_id

        callback_url = self._follow_redirects(continue_url)
        if not callback_url:
            # 如果 continue_url 本身就是 callback（包含 code= 和 state=）
            if "code=" in continue_url and "state=" in continue_url:
                callback_url = continue_url
            else:
                result.error_message = "consent continue_url 重定向链未找到回调 URL"
                return False

        self._log("处理 OAuth 回调，准备把 token 请出来...")
        token_info = self._handle_oauth_callback(callback_url)
        if not token_info:
            result.error_message = "处理 OAuth 回调失败"
            return False

        result.account_id = token_info.get("account_id", "")
        result.access_token = token_info.get("access_token", "")
        result.refresh_token = token_info.get("refresh_token", "")
        result.id_token = token_info.get("id_token", "")
        result.password = self.password or ""
        result.source = "login" if self._is_existing_account else "register"

        result.cookies = serialize_cookie_store(getattr(self.session, "cookies", None))
        session_cookie = resolve_session_cookie_from_cookie_store(getattr(self.session, "cookies", None))
        if session_cookie:
            self.session_token = session_cookie["value"]
            result.session_token = session_cookie["value"]
            self._log("Session Token 也捞到了，今天这网没白连")
        else:
            fallback_session_token = str(token_info.get("session_token") or "").strip()
            if fallback_session_token:
                self.session_token = fallback_session_token
                result.session_token = fallback_session_token

        if not enforce_refresh_token_requirement(result, subject="当前账号"):
            self._log(result.error_message, "warning")
            return False

        return True

    def _complete_token_exchange(self, result: RegistrationResult) -> bool:
        """在登录态已建立后，继续完成 workspace 和 OAuth token 获取。"""
        self._recovered_workspace_id = None
        self._session_exchange_token_info = None
        otp_page_type = ""
        callback_url: Optional[str] = None
        if getattr(self, "_consent_skip_otp", False):
            self._log("Codex consent 流程: 已跳过 OTP，直接进入 workspace 选择")
            self._consent_skip_otp = False
        else:
            self._log("等待登录验证码到场，最后这位嘉宾还在路上...")
            code = self._get_verification_code()
            if not code:
                result.error_message = "获取验证码失败"
                return False

            self._log("核对登录验证码，验明正身一下...")
            otp_response = self._validate_verification_code(code)
            if otp_response is None:
                result.error_message = "验证码校验失败"
                return False

            # 检查 OTP 验证后是否需要 consent 步骤（新账号首次 Codex 登录）
            otp_page_type = str((otp_response.get("page") or {}).get("type") or "").strip()
            if otp_page_type == OPENAI_PAGE_TYPES["CODEX_CONSENT"]:
                self._log("验证码校验后遇到 Codex 授权同意页，自动提交同意...")
                consent_ok, consent_continue_url = self._submit_codex_consent_post()
                if not consent_ok:
                    result.error_message = "Codex 授权同意提交失败"
                    return False
                # consent 响应包含 OAuth 回调 URL 时直接使用
                if consent_continue_url and ("code=" in consent_continue_url and "state=" in consent_continue_url):
                    return self._complete_via_continue_url(consent_continue_url, result)
                # continue_url 不是回调 URL（如 log-in-or-create-account），忽略并继续正常流程
            elif otp_page_type == OPENAI_PAGE_TYPES["ADD_PHONE"]:
                self._log("验证码校验后进入 add_phone 页面，服务端要求补充手机号后才能继续授权", "warning")
                self._mark_add_phone_required(result, "当前账号")
                return False

        target_ws = getattr(self, "_target_workspace_id", None)
        if target_ws:
            self._log(f"使用指定的 Team Workspace ID: {target_ws}")
            workspace_id = target_ws
        else:
            self._log("摸一下 Workspace ID，看看该坐哪桌...")
            workspace_lookup = self._get_workspace_lookup()
            workspace_id = workspace_lookup.workspace_id

            # 新账号 OTP 后 cookie 可能还没有 workspace，主动推进 authorize/continue 触发分配
            if (
                not workspace_id
                and workspace_lookup.reason_code in {"missing_cookie", "missing_workspaces"}
            ):
                self._log(
                    "Cookie 中未找到可用 workspace，尝试推进授权流程触发 workspace 分配..."
                )
                consent_ok, consent_continue_url = self._submit_codex_consent_post()
                if consent_ok:
                    if consent_continue_url and "code=" in consent_continue_url and "state=" in consent_continue_url:
                        return self._complete_via_continue_url(consent_continue_url, result)
                    workspace_lookup = self._get_workspace_lookup()
                    workspace_id = workspace_lookup.workspace_id

            if not workspace_id:
                result.error_message = workspace_lookup.error_message or "获取 Workspace ID 失败"
                return False

        result.workspace_id = workspace_id

        self._log("选择 Workspace，安排个靠谱座位...")
        workspace_selection = self._select_workspace(workspace_id)
        if not workspace_selection.success:
            callback_url, recovery_error = self._recover_workspace_selection_failure(
                workspace_id,
                workspace_selection,
            )
            if not callback_url and not self._session_exchange_token_info:
                # 最终兜底：尝试通过 ChatGPT SSO 建立 session 后换取 token
                self._log("所有 workspace/select 恢复路径均失败，尝试通过 ChatGPT SSO 兜底...", "warning")
                token_info, sso_error = self._recover_tokens_via_chatgpt_sso(workspace_id)
                if token_info:
                    self._session_exchange_token_info = token_info
                    self._recovered_workspace_id = workspace_id
                else:
                    result.error_message = recovery_error or workspace_selection.error_message or "选择 Workspace 失败"
                    if sso_error:
                        result.error_message = f"{result.error_message}；ChatGPT SSO 兜底也失败：{sso_error}"
                    return False
        else:
            continue_url = workspace_selection.continue_url
            self._log("顺着重定向面包屑往前走，别跟丢了...")
            callback_url = self._follow_redirects(continue_url)
            if not callback_url:
                result.error_message = "跟随重定向链失败"
                return False

        token_info = self._session_exchange_token_info
        if token_info:
            self._log("OAuth 回调状态已失效，改走 ChatGPT session 直连收尾，直接整理 token...")
        else:
            self._log("处理 OAuth 回调，准备把 token 请出来...")
            token_info = self._handle_oauth_callback(callback_url)
            if not token_info:
                result.error_message = "处理 OAuth 回调失败"
                return False

        if self._recovered_workspace_id:
            result.workspace_id = self._recovered_workspace_id

        result.account_id = token_info.get("account_id", "")
        result.access_token = token_info.get("access_token", "")
        result.refresh_token = token_info.get("refresh_token", "")
        result.id_token = token_info.get("id_token", "")
        result.password = self.password or ""
        result.source = "login" if self._is_existing_account else "register"

        result.cookies = serialize_cookie_store(getattr(self.session, "cookies", None))
        session_cookie = resolve_session_cookie_from_cookie_store(getattr(self.session, "cookies", None))
        if session_cookie:
            self.session_token = session_cookie["value"]
            result.session_token = session_cookie["value"]
            self._log("Session Token 也捞到了，今天这网没白连")
        else:
            fallback_session_token = str(token_info.get("session_token") or "").strip()
            if fallback_session_token:
                self.session_token = fallback_session_token
                result.session_token = fallback_session_token
                self._log("Cookie 里没直接刷出新的 Session Token，先沿用 ChatGPT session 直连拿到的 token 收尾")
            else:
                cookie_names = ", ".join(list_cookie_names(getattr(self.session, "cookies", None))) or "none"
                self._log(f"这次登录没直接捞到 Session Token，当前捕获到的 cookie 名称: {cookie_names}", "warning")

        if not enforce_refresh_token_requirement(result, subject="当前账号"):
            self._log(result.error_message, "warning")
            return False

        return True

    def _restart_login_flow(self) -> Tuple[bool, str]:
        """新注册账号完成建号后，重新发起一次登录流程拿 token。

        保留现有 HTTP session（包括注册阶段的 cookies），仅重建 OAuth 上下文。
        这样服务端可以在后续 workspace/select 时正确关联当前用户身份。
        """
        self._token_acquisition_requires_login = True
        self._log("注册这边忙完了，再走一趟登录把 token 请出来，收个尾...")

        did, sen_token = self._prepare_authorize_flow_with_existing_session("重新登录")
        if not did:
            return False, "重新登录时获取 Device ID 失败"
        if not sen_token:
            return False, "重新登录时 Sentinel POW 验证失败"

        login_start_result = self._submit_login_start(did, sen_token)
        if not login_start_result.success:
            return False, f"重新登录提交邮箱失败: {login_start_result.error_message}"

        page_type = login_start_result.page_type or ""

        # 已登录态可能直接跳到 consent 或 OTP，不一定进密码页
        if page_type == OPENAI_PAGE_TYPES["CODEX_CONSENT"]:
            self._log("重新登录时直接进入 Codex 授权同意页，跳过密码和验证码")
            self._consent_skip_otp = True
            self._is_existing_account = True
            return True, ""

        if page_type == OPENAI_PAGE_TYPES["EMAIL_OTP_VERIFICATION"]:
            self._log("重新登录时直接进入验证码页面，跳过密码步骤")
            self._otp_sent_at = time.time()
            self._is_existing_account = True
            return True, ""

        if page_type != OPENAI_PAGE_TYPES["LOGIN_PASSWORD"]:
            return False, f"重新登录未进入密码页面: {page_type or 'unknown'}"

        password_result = self._submit_login_password()
        if not password_result.success:
            return False, f"重新登录提交密码失败: {password_result.error_message}"

        if password_result.page_type == OPENAI_PAGE_TYPES["CODEX_CONSENT"]:
            self._log("重新登录密码通过后直接进入 Codex 授权同意页")
            self._consent_skip_otp = True
            self._is_existing_account = True
            return True, ""

        if not password_result.is_existing_account:
            return False, f"重新登录未进入验证码页面: {password_result.page_type or 'unknown'}"
        return True, ""

    def _recover_after_registration_conflict(self) -> Tuple[bool, str]:
        """注册密码阶段疑似撞到已存在账号时，切换到登录流程兜底。"""
        conflict_detected = self._registration_conflict_detected
        if not conflict_detected:
            for line in reversed(self.logs[-6:]):
                if self._is_existing_account_registration_conflict("", line):
                    conflict_detected = True
                    self._registration_conflict_message = self._registration_conflict_message or line
                    break
        if not conflict_detected:
            return False, ""

        self._registration_conflict_detected = True
        self._log("检测到邮箱可能已存在账号，改走邮箱登录刷新身份", "warning")
        self._reset_auth_flow()

        did, sen_token = self._prepare_authorize_flow("注册冲突后切换登录")
        if not did:
            return False, "注册冲突后切换登录时获取 Device ID 失败"
        if not sen_token:
            return False, "注册冲突后切换登录时 Sentinel POW 验证失败"

        login_start_result = self._submit_login_start(did, sen_token)
        if not login_start_result.success:
            return False, f"注册冲突后提交登录入口失败: {login_start_result.error_message}"

        if login_start_result.page_type == OPENAI_PAGE_TYPES["EMAIL_OTP_VERIFICATION"]:
            self._is_existing_account = True
            self._otp_sent_at = time.time()
            self.password = None
            self._password_generated_for_registration = False
            self._log("登录入口直接进入邮箱验证码页，继续通过邮箱验证码刷新身份")
            return True, ""

        if login_start_result.page_type != OPENAI_PAGE_TYPES["LOGIN_PASSWORD"]:
            return False, f"注册冲突后未进入可用的登录页面: {login_start_result.page_type or 'unknown'}"

        detail = self._registration_conflict_message or "该邮箱疑似已在 OpenAI 注册"
        return False, f"{detail}；当前本地没有可用密码，无法自动完成邮箱登录"

    def _register_password(self, _did: Optional[str] = None, _sen_token: Optional[str] = None) -> Tuple[bool, Optional[str]]:
        """注册密码"""
        try:
            password = self._generate_password()
            self.password = password
            self._last_register_password_error = None
            self._last_register_password_context = {}
            self._log(f"生成密码: {password}")

            if _did and self.session:
                try:
                    seed_oai_device_cookie(self.session, _did)
                except Exception:
                    pass

            register_payload = {
                "password": password,
                "username": self.email,
            }
            request_headers = self._build_auth_headers(
                "https://auth.openai.com/create-account/password",
                target_url=OPENAI_API_ENDPOINTS["register"],
                add_datadog_trace=True,
            )

            response = self.session.post(
                OPENAI_API_ENDPOINTS["register"],
                headers=request_headers,
                json=register_payload,
                timeout=30,
            )

            self._log(f"提交密码状态: {response.status_code}")

            if response.status_code != 200:
                context = self._build_register_password_context(response, request_headers)
                self._last_register_password_context = context
                flow_state = context.get("flow_state") or FlowState()
                cookie_names = ", ".join(context.get("cookie_names") or []) or "none"
                fallback_text = str(getattr(response, "text", "") or "")[:200]
                detail_message = context.get("error_message") or fallback_text or f"HTTP {response.status_code}"
                self._last_register_password_error = (
                    f"注册密码接口返回异常: HTTP {context.get('status_code') or response.status_code}, "
                    f"type={context.get('error_type') or '-'}, "
                    f"code={context.get('error_code') or '-'}, "
                    f"message={detail_message}"
                )
                self._log(
                    "密码注册失败诊断: "
                    f"status={context.get('status_code') or response.status_code}, "
                    f"type={context.get('error_type') or '-'}, "
                    f"code={context.get('error_code') or '-'}, "
                    f"message={detail_message}, "
                    f"page={flow_state.page_type or '-'}, "
                    f"cookies={cookie_names}, "
                    f"browser_headers={context.get('browser_headers_enabled')}",
                    "warning",
                )

                if self._is_existing_account_registration_conflict(
                    str(context.get("error_code") or ""),
                    detail_message,
                ):
                    self._log(f"邮箱 {self.email} 可能已在 OpenAI 注册过", "error")
                    self._mark_email_as_registered()

                return False, None

            return True, password

        except Exception as e:
            self._log(f"密码注册失败: {e}", "error")
            self._last_register_password_error = str(e)
            self._last_register_password_context = {}
            return False, None

    def _register_password_with_retry(
        self,
        did: Optional[str] = None,
        sen_token: Optional[str] = None,
    ) -> Tuple[bool, Optional[str]]:
        """当 OpenAI 返回可恢复的通用 400 时，重新生成密码并重试。"""
        self._raise_if_cancelled("任务已取消，停止密码注册重试")
        max_attempts = 3

        for attempt in range(1, max_attempts + 1):
            self._raise_if_cancelled("任务已取消，停止密码注册重试")
            success, password = self._register_password(did, sen_token)
            if success:
                return True, password

            context = dict(getattr(self, "_last_register_password_context", {}) or {})
            if attempt >= max_attempts:
                break
            if not self._should_retry_register_password(context):
                break

            try:
                flow_state = self._probe_register_password_flow_state()
            except Exception as probe_error:
                self._log(f"密码注册失败后刷新授权步骤失败: {probe_error}", "warning")
                flow_state = context.get("flow_state") or FlowState()
            else:
                context["flow_state"] = flow_state
                self._last_register_password_context = context

            handled = self._handle_register_password_flow_state(flow_state)
            if handled is True:
                return True, self.password
            if handled is False:
                break

            self._log(
                f"密码注册命中可重试 400，确认仍停留在密码页后准备重新生成密码重试 ({attempt}/{max_attempts})...",
                "warning",
            )
            self._sleep_interruptible(min(2 * attempt, 4))

        return False, None

    def _mark_email_as_registered(self):
        """标记邮箱为已注册状态（用于防止重复尝试）"""
        try:
            with get_db() as db:
                # 检查是否已存在该邮箱的记录
                existing = crud.get_account_by_email(db, self.email)
                if not existing:
                    # 创建一个失败记录，标记该邮箱已注册过
                    crud.create_account(
                        db,
                        email=self.email,
                        password="",  # 空密码表示未成功注册
                        email_service=self.email_service.service_type.value,
                        email_service_id=self.email_info.get("service_id") if self.email_info else None,
                        status="failed",
                        extra_data={"register_failed_reason": "email_already_registered_on_openai"}
                    )
                    self._log(f"已在数据库中标记邮箱 {self.email} 为已注册状态")
        except Exception as e:
            logger.warning(f"标记邮箱状态失败: {e}")

    def _send_verification_code(self) -> bool:
        """发送验证码"""
        try:
            # 记录发送时间戳
            self._otp_sent_at = time.time()

            response = self.session.get(
                OPENAI_API_ENDPOINTS["send_otp"],
                headers=self._build_auth_headers("https://auth.openai.com/create-account/password"),
            )

            self._log(f"验证码发送状态: {response.status_code}")
            return response.status_code == 200

        except Exception as e:
            self._log(f"发送验证码失败: {e}", "error")
            return False

    def _get_verification_code(self) -> Optional[str]:
        """获取验证码"""
        try:
            self._log(f"正在等待邮箱 {self.email} 的验证码...")

            email_id = self.email_info.get("service_id") if self.email_info else None
            code = self.email_service.get_verification_code(
                email=self.email,
                email_id=email_id,
                timeout=120,
                pattern=OTP_CODE_PATTERN,
                otp_sent_at=self._otp_sent_at,
            )

            if code:
                self._log(f"成功获取验证码: {code}")
                return code
            else:
                self._log("等待验证码超时", "error")
                return None

        except Exception as e:
            self._log(f"获取验证码失败: {e}", "error")
            return None

    def _validate_verification_code(self, code: str) -> Optional[dict]:
        """验证验证码，成功返回响应数据字典，失败返回 None"""
        try:
            code_body = f'{{"code":"{code}"}}'

            response = self.session.post(
                OPENAI_API_ENDPOINTS["validate_otp"],
                headers=self._build_auth_headers("https://auth.openai.com/email-verification"),
                data=code_body,
            )

            self._log(f"验证码校验状态: {response.status_code}")
            if response.status_code != 200:
                return None

            try:
                response_data = response.json()
            except Exception:
                response_data = {}

            page_type = (response_data.get("page") or {}).get("type", "")
            if page_type:
                self._log(f"验证码校验响应页面类型: {page_type}")

            return response_data

        except Exception as e:
            self._log(f"验证验证码失败: {e}", "error")
            return None

    def _create_user_account(self) -> bool:
        """创建用户账户"""
        try:
            user_info = generate_random_user_info()
            self._log(f"生成用户信息: {user_info['name']}, 生日: {user_info['birthdate']}")
            create_account_body = json.dumps(user_info)

            response = self.session.post(
                OPENAI_API_ENDPOINTS["create_account"],
                headers=self._build_auth_headers("https://auth.openai.com/about-you"),
                data=create_account_body,
            )

            self._log(f"账户创建状态: {response.status_code}")

            if response.status_code != 200:
                self._log(f"账户创建失败: {response.text[:200]}", "warning")
                return False

            return True

        except Exception as e:
            self._log(f"创建账户失败: {e}", "error")
            return False

    def _format_add_phone_required_message(self, subject: str) -> str:
        """构造 add_phone 阻塞时的统一错误文案。"""
        prefix = str(subject or "").strip() or "当前账号"
        return f"{prefix}进入 add_phone 页面，需要补充手机号后才能继续授权"

    def _mark_add_phone_required(self, result: RegistrationResult, subject: str) -> None:
        result.error_code = "add_phone_required"
        result.error_message = self._format_add_phone_required_message(subject)

    def _sync_add_phone_result(self, result: RegistrationResult) -> RegistrationResult:
        if not result.success and "add_phone" in str(result.error_message or "").lower():
            result.error_code = result.error_code or "add_phone_required"
        return result

    def _get_workspace_lookup(self) -> WorkspaceLookupResult:
        """获取 Workspace Cookie 解析结果。"""
        try:
            auth_cookie = self.session.cookies.get("oai-client-auth-session")
            if not auth_cookie:
                self._log("未能获取到授权 Cookie", "error")
                return WorkspaceLookupResult(
                    error_message="服务端尚未建立授权 Cookie，无法继续选择 Workspace",
                    reason_code="missing_cookie",
                )

            # 解码 JWT
            import base64
            import json as json_module

            try:
                segments = auth_cookie.split(".")
                if len(segments) < 1:
                    self._log("授权 Cookie 格式错误", "error")
                    return WorkspaceLookupResult(
                        error_message="授权 Cookie 格式错误，无法继续选择 Workspace",
                        reason_code="invalid_cookie",
                    )

                # 解码第一个 segment
                payload = segments[0]
                pad = "=" * ((4 - (len(payload) % 4)) % 4)
                decoded = base64.urlsafe_b64decode((payload + pad).encode("ascii"))
                auth_json = json_module.loads(decoded.decode("utf-8"))
                self._log(f"授权 Cookie 字段: {list(auth_json.keys())}")

                workspaces = auth_json.get("workspaces") or []
                if not workspaces:
                    self._log(
                        f"授权 Cookie 里没有 workspace 信息 (字段: {list(auth_json.keys())})",
                        "warning",
                    )
                    return WorkspaceLookupResult(
                        error_message="服务端尚未下发 workspace 信息，无法继续授权",
                        reason_code="missing_workspaces",
                    )

                workspace_id = str((workspaces[0] or {}).get("id") or "").strip()
                if not workspace_id:
                    self._log("授权 Cookie 中的 workspace 列表缺少可用的 workspace_id", "error")
                    return WorkspaceLookupResult(
                        error_message="服务端返回了 workspace 列表，但缺少可用的 workspace_id",
                        reason_code="missing_workspace_id",
                    )

                self._log(f"Workspace ID: {workspace_id}")
                return WorkspaceLookupResult(workspace_id=workspace_id)

            except Exception as e:
                self._log(f"解析授权 Cookie 失败: {e}", "error")
                return WorkspaceLookupResult(
                    error_message="解析授权 Cookie 失败",
                    reason_code="invalid_cookie",
                )

        except Exception as e:
            self._log(f"获取 Workspace ID 失败: {e}", "error")
            return WorkspaceLookupResult(
                error_message="读取授权 Cookie 失败，无法继续选择 Workspace",
                reason_code="workspace_lookup_error",
            )

    def _get_workspace_id(self) -> Optional[str]:
        """兼容旧调用，返回解析出的 Workspace ID。"""
        return self._get_workspace_lookup().workspace_id or None

    def _sanitize_workspace_response_url(self, url: str) -> str:
        """移除 query/fragment，避免日志里出现敏感参数"""
        normalized = str(url or "").strip()
        if not normalized:
            return OPENAI_API_ENDPOINTS["select_workspace"]

        split = urlsplit(normalized)
        return urlunsplit((split.scheme, split.netloc, split.path, "", ""))

    def _build_workspace_response_diagnostics(self, response: Any) -> Dict[str, Any]:
        """构建 workspace/select 的安全诊断信息"""
        headers = getattr(response, "headers", {}) or {}
        text = str(getattr(response, "text", "") or "")
        content_type = str(
            headers.get("content-type")
            or headers.get("Content-Type")
            or ""
        ).split(";", 1)[0].strip().lower()
        location = str(headers.get("location") or headers.get("Location") or "").strip()
        normalized_text = text.lstrip().lower()

        if not text.strip():
            body_kind = "empty"
        elif "json" in content_type or normalized_text.startswith("{") or normalized_text.startswith("["):
            body_kind = "json"
        elif "html" in content_type or normalized_text.startswith("<"):
            body_kind = "html"
        else:
            body_kind = "text"

        return {
            "status_code": getattr(response, "status_code", 0),
            "content_type": content_type or "unknown",
            "response_url": self._sanitize_workspace_response_url(getattr(response, "url", "")),
            "location": location,
            "has_location": bool(location),
            "body_kind": body_kind,
            "body_length": len(text),
        }

    def _log_workspace_selection_diagnostics(self, diagnostics: Dict[str, Any]) -> None:
        """记录 workspace/select 的安全诊断日志"""
        self._log(
            "workspace/select 响应诊断: "
            f"status={diagnostics['status_code']}, "
            f"content_type={diagnostics['content_type']}, "
            f"response_url={diagnostics['response_url']}, "
            f"has_location={diagnostics['has_location']}, "
            f"body_kind={diagnostics['body_kind']}, "
            f"body_length={diagnostics['body_length']}",
            "warning",
        )

    def _format_workspace_error_message(self, status_code: int, error_code: str, error_detail: str) -> str:
        """格式化 Workspace 选择失败文案。"""
        detail = str(error_detail or "").strip()
        code = str(error_code or "").strip()
        if detail:
            return f"选择 Workspace 失败：{detail[:160]}"
        if code:
            return f"选择 Workspace 失败：错误码 {code}"
        return f"选择 Workspace 失败：HTTP {status_code or 'unknown'}"

    def _summarize_workspace_selection_failure(self, selection: WorkspaceSelectionResult) -> str:
        """提炼 workspace/select 的失败原因，避免重复前缀。"""
        detail = str(selection.error_detail or "").strip()
        if detail:
            return detail[:160]

        message = str(selection.error_message or "").strip()
        prefix = "选择 Workspace 失败："
        if message.startswith(prefix):
            return message[len(prefix):].strip() or message
        return message or "重新选择 Workspace 失败"

    def _build_safe_response_diagnostics(self, response: Any, *, default_url: str) -> Dict[str, Any]:
        """构建通用的安全响应诊断信息。"""
        headers = getattr(response, "headers", {}) or {}
        text = str(getattr(response, "text", "") or "")
        content_type = str(
            headers.get("content-type")
            or headers.get("Content-Type")
            or ""
        ).split(";", 1)[0].strip().lower()
        location = str(headers.get("location") or headers.get("Location") or "").strip()
        normalized_text = text.lstrip().lower()

        if not text.strip():
            body_kind = "empty"
        elif "json" in content_type or normalized_text.startswith("{") or normalized_text.startswith("["):
            body_kind = "json"
        elif "html" in content_type or normalized_text.startswith("<"):
            body_kind = "html"
        else:
            body_kind = "text"

        response_url = self._sanitize_workspace_response_url(getattr(response, "url", "") or default_url)
        return {
            "status_code": getattr(response, "status_code", 0),
            "content_type": content_type or "unknown",
            "response_url": response_url,
            "has_location": bool(location),
            "body_kind": body_kind,
            "body_length": len(text),
        }

    def _log_session_exchange_diagnostics(self, diagnostics: Dict[str, Any], label: str) -> None:
        """记录 ChatGPT session exchange 的安全诊断日志。"""
        self._log(
            f"{label}: ChatGPT session 响应诊断: "
            f"status={diagnostics['status_code']}, "
            f"content_type={diagnostics['content_type']}, "
            f"response_url={diagnostics['response_url']}, "
            f"has_location={diagnostics['has_location']}, "
            f"body_kind={diagnostics['body_kind']}, "
            f"body_length={diagnostics['body_length']}",
            "warning",
        )

    def _persist_session_cookie_value(self, cookie_name: str, cookie_value: str) -> None:
        """尽量把刷新后的 session token 写回当前 cookie store，便于后续落库。"""
        if not self.session or not cookie_name or not cookie_value:
            return

        cookie_store = getattr(self.session, "cookies", None)
        if cookie_store is None:
            return

        try:
            if hasattr(cookie_store, "set"):
                try:
                    cookie_store.set(cookie_name, cookie_value, domain=".chatgpt.com", path="/")
                except TypeError:
                    cookie_store.set(cookie_name, cookie_value)
                return
        except Exception:
            pass

        try:
            cookie_store[cookie_name] = cookie_value
        except Exception:
            return

    def _recover_tokens_via_chatgpt_sso(
        self,
        workspace_id: str,
    ) -> Tuple[Optional[Dict[str, Any]], str]:
        """通过 ChatGPT SSO 登录流程建立 session 并换取 token。

        当 workspace/select 持续返回 invalid_state 且所有恢复链路均失败时，
        尝试直接访问 ChatGPT 的 auth 端点，利用 auth.openai.com 上的现有登录态
        自动完成 SSO 并获取 session token。
        """
        if not self.session:
            return None, "当前没有可用登录会话"

        label = "ChatGPT SSO 兜底"
        self._log(f"{label}: 尝试通过 ChatGPT 登录端点建立 session...")

        try:
            # 访问 ChatGPT 首页，让服务端通过 SSO 自动建立 session
            response = self.session.get(
                "https://chatgpt.com/api/auth/session",
                headers={
                    "accept": "application/json",
                    "referer": "https://chatgpt.com/",
                },
                timeout=30,
            )

            if response.status_code != 200:
                self._log(f"{label}: ChatGPT auth/session 请求返回 HTTP {response.status_code}", "warning")
                return None, f"ChatGPT auth/session 返回 HTTP {response.status_code}"

            try:
                payload = response.json() or {}
            except Exception:
                return None, "ChatGPT auth/session 响应不是 JSON"

            if not isinstance(payload, dict):
                return None, "ChatGPT auth/session 响应结构异常"

            access_token = str(payload.get("accessToken") or "").strip()
            if not access_token:
                # 首次访问可能没有 token，检查 cookie 是否已建立
                session_cookie = resolve_session_cookie_from_cookie_store(getattr(self.session, "cookies", None))
                if session_cookie:
                    self._log(f"{label}: 首次访问拿到了 session cookie，尝试 workspace token 交换...")
                    return self._recover_tokens_via_session_exchange(
                        workspace_id,
                        label=label,
                    )
                return None, "ChatGPT auth/session 响应无 accessToken 且无 session cookie"

            # 拿到了 access_token，提取信息
            access_claims = _decode_jwt_payload(access_token)
            auth_claims = access_claims.get("https://api.openai.com/auth") or {}
            account_id = str(
                auth_claims.get("chatgpt_account_id")
                or payload.get("account", {}).get("id", "")
                or workspace_id
            ).strip()
            session_token = str(payload.get("sessionToken") or "").strip()

            if session_token:
                self._persist_session_cookie_value("__Secure-next-auth.session-token", session_token)

            self._log(f"{label}: 成功通过 ChatGPT SSO 获取 access token")
            return {
                "account_id": account_id,
                "access_token": access_token,
                "refresh_token": "",
                "id_token": "",
                "session_token": session_token,
            }, ""

        except Exception as exc:
            self._log(f"{label}: 请求异常: {exc}", "warning")
            return None, str(exc)

    def _recover_tokens_via_session_exchange(
        self,
        workspace_id: str,
        *,
        label: str,
    ) -> Tuple[Optional[Dict[str, Any]], str]:
        """在 OAuth 回调失效时，尝试直接用现有 session cookie 换取 workspace token。"""
        if not self.session:
            return None, "当前没有可用登录会话，无法改走 ChatGPT session 直连兜底"

        normalized_workspace_id = str(workspace_id or "").strip()
        if not normalized_workspace_id:
            return None, "缺少可用的 Workspace ID，无法改走 ChatGPT session 直连兜底"

        session_cookie = resolve_session_cookie_from_cookie_store(getattr(self.session, "cookies", None))
        if not session_cookie:
            cookie_names = ", ".join(list_cookie_names(getattr(self.session, "cookies", None))) or "none"
            self._log(f"{label}: 当前 Cookie 中没有可用 session token，现有 cookie: {cookie_names}", "warning")
            return None, "当前登录态缺少可用 session token，无法改走 ChatGPT session 直连兜底"

        exchange_url = (
            "https://chatgpt.com/api/auth/session"
            f"?exchange_workspace_token=true&workspace_id={quote(normalized_workspace_id, safe='')}"
            "&reason=setCurrentAccount"
        )
        self._log(f"{label}: OAuth 回调没续上，尝试直接用当前 session 切换 Workspace 并换取 access token...")

        try:
            response = self.session.get(
                exchange_url,
                headers={
                    "accept": "application/json",
                    "referer": "https://chatgpt.com/",
                    "Cookie": build_session_cookie_header(session_cookie["value"], session_cookie["name"]),
                },
                timeout=30,
            )
        except Exception as exc:
            self._log(f"{label}: ChatGPT session 直连请求失败: {exc}", "warning")
            return None, f"ChatGPT session 直连请求失败: {exc}"

        diagnostics = self._build_safe_response_diagnostics(response, default_url=exchange_url)
        if response.status_code != 200:
            self._log_session_exchange_diagnostics(diagnostics, label)
            error_code, error_detail = self._extract_openai_error(response)
            if error_code or error_detail:
                self._log(
                    f"{label}: ChatGPT session 直连返回错误码: {error_code or 'unknown'}, 消息: {error_detail or 'unknown'}",
                    "warning",
                )
            return None, error_detail or f"ChatGPT session 直连失败: HTTP {response.status_code}"

        try:
            payload = response.json() or {}
        except Exception:
            self._log_session_exchange_diagnostics(diagnostics, label)
            return None, "ChatGPT session 直连失败：响应不是 JSON"

        if not isinstance(payload, dict):
            self._log_session_exchange_diagnostics(diagnostics, label)
            return None, "ChatGPT session 直连失败：响应结构异常"

        access_token = str(payload.get("accessToken") or "").strip()
        if not access_token:
            self._log_session_exchange_diagnostics(diagnostics, label)
            return None, "ChatGPT session 直连失败：响应缺少 accessToken"

        access_claims = _decode_jwt_payload(access_token)
        auth_claims = access_claims.get("https://api.openai.com/auth") or {}
        account_payload = payload.get("account") if isinstance(payload.get("account"), dict) else {}
        refreshed_account_id = str(
            auth_claims.get("chatgpt_account_id")
            or account_payload.get("id")
            or normalized_workspace_id
        ).strip()
        refreshed_session_token = str(payload.get("sessionToken") or session_cookie["value"] or "").strip()
        if refreshed_session_token:
            self._persist_session_cookie_value(session_cookie["name"], refreshed_session_token)

        self._log(f"{label}: ChatGPT session 直连已拿到 access token，改用这条链路收尾")
        return {
            "account_id": refreshed_account_id,
            "access_token": access_token,
            "refresh_token": "",
            "id_token": "",
            "session_token": refreshed_session_token,
        }, ""

    def _try_resume_current_oauth_entry(self, label: str) -> Optional[str]:
        """优先沿用当前登录态，直接从新的 OAuth 入口续上 callback。"""
        if not self.session or not self.oauth_start or not str(self.oauth_start.auth_url or "").strip():
            return None

        self._log(f"{label}: invalid_state 恢复路径 session_resume，先尝试沿用当前登录态直接续授权...")
        try:
            response = self.session.get(
                self.oauth_start.auth_url,
                allow_redirects=False,
                timeout=15,
            )
            location = str(response.headers.get("Location") or "").strip()
            if response.status_code in [301, 302, 303, 307, 308] and location:
                next_url = urljoin(self.oauth_start.auth_url, location)
                if "code=" in next_url and "state=" in next_url:
                    self._log(f"{label}: session_resume 直接拿到了 callback URL")
                    return next_url

                self._log(f"{label}: session_resume 拿到了新的 continue 链，继续跟进...")
                callback_url = self._follow_redirects(next_url)
                if callback_url:
                    return callback_url

            self._log(
                f"{label}: session_resume 未直接拿到 callback (HTTP {response.status_code})",
                "warning",
            )
            return None
        except Exception as e:
            self._log(f"{label}: session_resume 探测失败: {e}", "warning")
            return None

    def _refresh_workspace_lookup_for_recovery(
        self,
        workspace_id: str,
        label: str,
    ) -> WorkspaceLookupResult:
        """恢复链路里优先使用当前 cookie 中最新的 Workspace ID。"""
        lookup = self._get_workspace_lookup()
        latest_workspace_id = lookup.workspace_id
        if latest_workspace_id:
            self._recovered_workspace_id = latest_workspace_id
            if latest_workspace_id != workspace_id:
                self._log(
                    f"{label}: 检测到新的 Workspace ID，恢复链路改用最新值: {latest_workspace_id}"
                )
            return lookup

        self._recovered_workspace_id = workspace_id
        reason = lookup.reason_code or "unknown"
        self._log(
            f"{label}: 未能从当前 Cookie 刷新出新的 Workspace ID，继续沿用原值: {workspace_id} (原因: {reason})",
            "warning",
        )
        return lookup

    def _select_workspace_after_recovery(
        self,
        workspace_id: str,
        *,
        label: str,
        error_prefix: str,
    ) -> Tuple[Optional[str], str]:
        """恢复授权后重新选择 Workspace，并跟随 redirect 找 callback。"""
        lookup = self._refresh_workspace_lookup_for_recovery(workspace_id, label)
        effective_workspace_id = lookup.workspace_id or workspace_id
        if not effective_workspace_id:
            reason = lookup.error_message or "服务端尚未下发 workspace 信息，无法继续授权"
            return None, f"{error_prefix}：{reason}"

        if not lookup.success and lookup.reason_code in {"missing_cookie", "missing_workspaces", "missing_workspace_id"}:
            self._log(
                f"{label}: 当前 Cookie 未提供新的 workspace，继续沿用已确认的 Workspace ID 重试: {effective_workspace_id}",
                "warning",
            )

        selection = self._select_workspace(effective_workspace_id)
        if not selection.success:
            reason = self._summarize_workspace_selection_failure(selection)
            return None, f"{error_prefix}：{reason}"

        self._log(f"{label}: Workspace 重新选好了，继续跟随授权重定向链...")
        callback_url = self._follow_redirects(selection.continue_url)
        if callback_url:
            return callback_url, ""
        return None, f"{error_prefix}：未能在恢复后的重定向链中找到回调 URL"

    def _continue_recovery_after_consent(
        self,
        workspace_id: str,
        *,
        label: str,
        error_prefix: str,
    ) -> Tuple[Optional[str], str]:
        """恢复链路再次同意 consent 后，优先直接续上 workspace，再回退到 auth entry。"""
        lookup = self._get_workspace_lookup()

        if lookup.success:
            self._log(f"{label}: consent 后已拿到 workspace，优先直接继续 Workspace 选择...")
            callback_url, selection_error = self._select_workspace_after_recovery(
                workspace_id,
                label=label,
                error_prefix=error_prefix,
            )
            if callback_url:
                return callback_url, ""

            self._log(
                f"{label}: consent 后直接续 workspace 未成功，尝试沿用当前登录态继续跟进授权入口...",
                "warning",
            )
            callback_url = self._try_resume_current_oauth_entry(label)
            if callback_url:
                self._refresh_workspace_lookup_for_recovery(workspace_id, label)
                return callback_url, ""
            return None, selection_error

        self._log(f"{label}: consent 后暂未拿到 workspace，继续尝试沿用当前登录态直连授权入口...", "warning")
        callback_url = self._try_resume_current_oauth_entry(label)
        if callback_url:
            self._refresh_workspace_lookup_for_recovery(workspace_id, label)
            return callback_url, ""

        return self._select_workspace_after_recovery(
            workspace_id,
            label=label,
            error_prefix=error_prefix,
        )

    def _recover_workspace_via_same_account_reauth(
        self,
        workspace_id: str,
        initial_page_type: str,
    ) -> Tuple[Optional[str], str]:
        """invalid_state 后沿用同一邮箱/密码补走登录挑战。"""
        label = "workspace/select invalid_state 恢复"
        error_prefix = "授权状态已失效，已尝试同号重新认证但仍未恢复授权流程"
        page_type = str(initial_page_type or "").strip()

        self._log(
            f"{label}: invalid_state 恢复路径 same_account_reauth，沿用同号继续补走登录挑战...",
            "warning",
        )

        if page_type == OPENAI_PAGE_TYPES["LOGIN_PASSWORD"]:
            if not str(self.password or "").strip():
                return None, "授权状态已失效，沿用登录态续授权失败，且当前账号没有可用密码，无法执行同号重新认证"

            self._log(f"{label}: same_account_reauth 进入登录密码校验...")
            password_result = self._submit_login_password()
            if not password_result.success:
                detail = password_result.error_message or "提交登录密码失败"
                return None, f"{error_prefix}：{detail}"
            page_type = str(password_result.page_type or "").strip()

        if page_type == OPENAI_PAGE_TYPES["EMAIL_OTP_VERIFICATION"]:
            self._log(f"{label}: same_account_reauth 等待新的登录验证码...")
            code = self._get_verification_code()
            if not code:
                return None, f"{error_prefix}：获取登录验证码失败"

            self._log(f"{label}: same_account_reauth 核对登录验证码...")
            otp_response = self._validate_verification_code(code)
            if otp_response is None:
                return None, f"{error_prefix}：登录验证码校验失败"
            page_type = str((otp_response.get("page") or {}).get("type") or "").strip()

        if page_type == OPENAI_PAGE_TYPES["ADD_PHONE"]:
            self._log(f"{label}: same_account_reauth 命中 add_phone 页面，当前流程无法继续授权", "warning")
            return None, f"{error_prefix}：{self._format_add_phone_required_message('同号重新认证后')}"

        if page_type == OPENAI_PAGE_TYPES["CODEX_CONSENT"]:
            self._log(f"{label}: same_account_reauth 再次遇到 Codex 授权同意页，自动提交同意...")
            consent_ok, consent_continue_url = self._submit_codex_consent_post()
            if not consent_ok:
                return None, f"{error_prefix}：重新提交 Codex 授权同意失败"
            if consent_continue_url:
                self._log(f"{label}: consent 响应自带 continue_url，直接跟随重定向链...")
                callback_url = self._follow_redirects(consent_continue_url)
                if not callback_url and "code=" in consent_continue_url and "state=" in consent_continue_url:
                    callback_url = consent_continue_url
                if callback_url:
                    self._refresh_workspace_lookup_for_recovery(workspace_id, label)
                    return callback_url, ""
                self._log(f"{label}: consent continue_url 未找到回调，回退到 workspace 选择...", "warning")
            return self._continue_recovery_after_consent(
                workspace_id,
                label=f"{label}: same_account_reauth",
                error_prefix=error_prefix,
            )

        if page_type in (
            OPENAI_PAGE_TYPES["LOGIN_PASSWORD"],
            OPENAI_PAGE_TYPES["EMAIL_OTP_VERIFICATION"],
        ):
            return None, f"{error_prefix}：同号重新认证后仍停留在 {page_type}"

        return self._select_workspace_after_recovery(
            workspace_id,
            label=f"{label}: same_account_reauth",
            error_prefix=error_prefix,
        )

    def _recover_workspace_after_invalid_state(self, workspace_id: str) -> Tuple[Optional[str], str]:
        """workspace/select 返回 invalid_state 后，保留登录态重建新的 OAuth 上下文。"""
        label = "workspace/select invalid_state 恢复"
        self._log(
            "workspace/select 返回 invalid_state，正在保留登录态重建新的 OAuth 授权流程...",
            "warning",
        )
        did, sen_token = self._prepare_authorize_flow_with_existing_session(label)
        if not did:
            return None, "授权状态已失效，需要重新建立授权流程：重建授权上下文时获取 Device ID 失败"
        if not sen_token:
            return None, "授权状态已失效，需要重新建立授权流程：重建授权上下文时 Sentinel 校验失败"

        callback_url = self._try_resume_current_oauth_entry(label)
        if callback_url:
            return callback_url, ""

        self._log(f"{label}: session_resume 未直接续上，改为探测授权入口页面...", "warning")
        login_start = self._submit_login_start(did, sen_token)
        if not login_start.success:
            detail = login_start.error_message or "重新进入授权入口失败"
            return None, f"授权状态已失效，沿用登录态续授权失败：{detail}"

        page_type = str(login_start.page_type or "").strip()
        self._log(
            f"{label}: session_resume 探测到授权入口页面类型: {page_type or 'unknown'}"
        )
        if page_type == OPENAI_PAGE_TYPES["CODEX_CONSENT"]:
            self._log(f"{label}: session_resume 再次遇到 Codex 授权同意页，自动补一次同意...")
            consent_ok, consent_continue_url = self._submit_codex_consent_post()
            if not consent_ok:
                return None, "授权状态已失效，沿用登录态续授权后仍未恢复授权流程：重新提交 Codex 授权同意失败"
            if consent_continue_url:
                self._log(f"{label}: consent 响应自带 continue_url，直接跟随重定向链...")
                callback_url = self._follow_redirects(consent_continue_url)
                if not callback_url and "code=" in consent_continue_url and "state=" in consent_continue_url:
                    callback_url = consent_continue_url
                if callback_url:
                    self._refresh_workspace_lookup_for_recovery(workspace_id, label)
                    return callback_url, ""
                self._log(f"{label}: consent continue_url 未找到回调，回退到 workspace 选择...", "warning")
            return self._continue_recovery_after_consent(
                workspace_id,
                label=f"{label}: session_resume",
                error_prefix="授权状态已失效，沿用登录态续授权后仍未恢复授权流程",
            )

        if page_type == OPENAI_PAGE_TYPES["ADD_PHONE"]:
            self._log(f"{label}: session_resume 命中 add_phone 页面，当前流程无法继续授权", "warning")
            return None, self._format_add_phone_required_message("授权状态已失效，沿用登录态续授权后")

        if page_type in (
            OPENAI_PAGE_TYPES["LOGIN_PASSWORD"],
            OPENAI_PAGE_TYPES["EMAIL_OTP_VERIFICATION"],
        ):
            self._log("授权状态已失效，沿用登录态续授权失败，已切换同号重新认证", "warning")
            return self._recover_workspace_via_same_account_reauth(workspace_id, page_type)

        return self._select_workspace_after_recovery(
            workspace_id,
            label=f"{label}: session_resume",
            error_prefix="授权状态已失效，沿用登录态续授权后仍未恢复授权流程",
        )

    def _recover_workspace_selection_failure(
        self,
        workspace_id: str,
        workspace_selection: WorkspaceSelectionResult,
    ) -> Tuple[Optional[str], str]:
        """根据 workspace/select 失败原因，尝试恢复或返回明确错误。"""
        if str(workspace_selection.error_code or "").strip() == "invalid_state":
            callback_url, recovery_error = self._recover_workspace_after_invalid_state(workspace_id)
            if callback_url:
                return callback_url, ""

            token_info, token_error = self._recover_tokens_via_session_exchange(
                workspace_id,
                label="workspace/select invalid_state 恢复",
            )
            if token_info:
                self._session_exchange_token_info = token_info
                self._recovered_workspace_id = workspace_id
                return None, ""

            combined_error = recovery_error or workspace_selection.error_message or "选择 Workspace 失败"
            if token_error:
                combined_error = f"{combined_error}；ChatGPT session 直连兜底也失败：{token_error}"
            return None, combined_error

        callback_url = self._resume_oauth_after_workspace_conflict(workspace_selection)
        if callback_url:
            return callback_url, ""
        return None, workspace_selection.error_message or "选择 Workspace 失败"

    def _resume_oauth_after_workspace_conflict(self, workspace_selection: WorkspaceSelectionResult) -> Optional[str]:
        """workspace/select 返回冲突后，尝试从原始 OAuth 入口继续。"""
        if workspace_selection.status_code != 409:
            return None
        if not self.oauth_start or not str(self.oauth_start.auth_url or "").strip():
            return None

        self._log(
            "workspace/select 返回冲突，尝试回到原始 OAuth 入口继续流程...",
            "warning",
        )
        try:
            response = self.session.get(
                self.oauth_start.auth_url,
                allow_redirects=False,
                timeout=15,
            )
            location = str(response.headers.get("Location") or "").strip()
            if response.status_code in [301, 302, 303, 307, 308] and location:
                next_url = urljoin(self.oauth_start.auth_url, location)
                if "code=" in next_url and "state=" in next_url:
                    self._log(f"冲突恢复后拿到回调 URL: {next_url[:100]}...")
                    return next_url
                self._log("冲突恢复后拿到新的 continue 链，继续跟进...")
                return self._follow_redirects(next_url)

            self._log(
                f"回放 OAuth 入口未得到可用重定向: HTTP {response.status_code}",
                "warning",
            )
            return None
        except Exception as e:
            self._log(f"回放 OAuth 入口失败: {e}", "warning")
            return None

    def _parse_workspace_selection_response(self, response: Any) -> WorkspaceSelectionResult:
        """解析 workspace/select 响应，兼容 JSON 和 3xx 重定向"""
        diagnostics = self._build_workspace_response_diagnostics(response)
        status_code = diagnostics["status_code"]
        error_code, error_detail = self._extract_openai_error(response)

        if status_code == 200:
            try:
                payload = response.json() or {}
            except Exception:
                self._log_workspace_selection_diagnostics(diagnostics)
                return WorkspaceSelectionResult(
                    error_message="选择 Workspace 失败：响应不是 JSON/重定向",
                    status_code=status_code,
                )

            continue_url = ""
            if isinstance(payload, dict):
                continue_url = str(payload.get("continue_url") or "").strip()
            if continue_url:
                return WorkspaceSelectionResult(
                    continue_url=continue_url,
                    status_code=status_code,
                    error_code=error_code,
                    error_detail=error_detail,
                )

            self._log_workspace_selection_diagnostics(diagnostics)
            return WorkspaceSelectionResult(
                error_message="选择 Workspace 失败：响应缺少 continue_url",
                status_code=status_code,
                error_code=error_code,
                error_detail=error_detail,
            )

        if status_code in [301, 302, 303, 307, 308]:
            location = diagnostics["location"]
            if location:
                continue_url = urljoin(diagnostics["response_url"], location)
                return WorkspaceSelectionResult(
                    continue_url=continue_url,
                    status_code=status_code,
                    error_code=error_code,
                    error_detail=error_detail,
                )

            self._log_workspace_selection_diagnostics(diagnostics)
            return WorkspaceSelectionResult(
                error_message="选择 Workspace 失败：重定向响应缺少 Location",
                status_code=status_code,
                error_code=error_code,
                error_detail=error_detail,
            )

        self._log_workspace_selection_diagnostics(diagnostics)
        if error_code or error_detail:
            self._log(
                f"workspace/select 返回错误码: {error_code or 'unknown'}, 消息: {error_detail or 'unknown'}",
                "warning",
            )
        return WorkspaceSelectionResult(
            error_message=self._format_workspace_error_message(status_code, error_code, error_detail),
            status_code=status_code,
            error_code=error_code,
            error_detail=error_detail,
        )

    def _select_workspace(self, workspace_id: str) -> WorkspaceSelectionResult:
        """选择 Workspace"""
        try:
            select_body = f'{{"workspace_id":"{workspace_id}"}}'

            response = self.session.post(
                OPENAI_API_ENDPOINTS["select_workspace"],
                headers=self._build_auth_headers("https://auth.openai.com/sign-in-with-chatgpt/codex/consent"),
                data=select_body,
                allow_redirects=False,
            )

            selection = self._parse_workspace_selection_response(response)
            if not selection.success:
                self._log(selection.error_message, "error")
                return selection

            self._log(f"Continue URL: {selection.continue_url[:100]}...")
            return selection

        except Exception as e:
            error_message = f"选择 Workspace 失败：{e}"
            self._log(error_message, "error")
            return WorkspaceSelectionResult(error_message=error_message)

    def _follow_redirects(self, start_url: str) -> Optional[str]:
        """跟随重定向链，寻找回调 URL"""
        try:
            current_url = start_url
            max_redirects = 6

            for i in range(max_redirects):
                self._log(f"重定向 {i+1}/{max_redirects}: {current_url[:100]}...")

                response = self.session.get(
                    current_url,
                    allow_redirects=False,
                    timeout=15
                )

                location = response.headers.get("Location") or ""

                # 如果不是重定向状态码，停止
                if response.status_code not in [301, 302, 303, 307, 308]:
                    self._log(f"非重定向状态码: {response.status_code}")
                    break

                if not location:
                    self._log("重定向响应缺少 Location 头")
                    break

                # 构建下一个 URL
                import urllib.parse
                next_url = urllib.parse.urljoin(current_url, location)

                # 检查是否包含回调参数
                if "code=" in next_url and "state=" in next_url:
                    self._log(f"找到回调 URL: {next_url[:100]}...")
                    return next_url

                current_url = next_url

            self._log("未能在重定向链中找到回调 URL", "error")
            return None

        except Exception as e:
            self._log(f"跟随重定向失败: {e}", "error")
            return None

    def _handle_oauth_callback(self, callback_url: str) -> Optional[Dict[str, Any]]:
        """处理 OAuth 回调"""
        try:
            if not self.oauth_start:
                self._log("OAuth 流程未初始化", "error")
                return None

            self._log("处理 OAuth 回调，最后一哆嗦，稳住别抖...")
            token_info = self.oauth_manager.handle_callback(
                callback_url=callback_url,
                expected_state=self.oauth_start.state,
                code_verifier=self.oauth_start.code_verifier
            )

            self._log("OAuth 授权成功，通关文牒到手")
            return token_info

        except Exception as e:
            self._log(f"处理 OAuth 回调失败: {e}", "error")
            return None

    def run(self) -> RegistrationResult:
        """
        执行完整的注册流程

        支持已注册账号自动登录：
        - 如果检测到邮箱已注册，自动切换到登录流程
        - 已注册账号跳过：设置密码、发送验证码、创建用户账户
        - 共用步骤：获取验证码、验证验证码、Workspace 和 OAuth 回调

        Returns:
            RegistrationResult: 注册结果
        """
        result = RegistrationResult(success=False, logs=self.logs)

        try:
            self._raise_if_cancelled("任务已取消，停止执行注册流程")
            self._is_existing_account = False
            self._token_acquisition_requires_login = False
            self._otp_sent_at = None
            self._password_generated_for_registration = False
            self._registration_conflict_detected = False
            self._registration_conflict_message = ""
            self._recovered_workspace_id = None
            self._session_exchange_token_info = None

            self._log("=" * 60)
            self._log("注册流程启动，开始替你敲门")
            self._log("=" * 60)

            # 1. 检查 IP 地理位置
            self._log("1. 先看看这条网络从哪儿来，别一开局就站错片场...")
            self._raise_if_cancelled("任务已取消，停止执行注册流程")
            ip_ok, location = self._check_ip_location()
            if not ip_ok:
                result.error_message = f"IP 地理位置不支持: {location}"
                self._log(f"IP 检查失败: {location}", "error")
                return result

            self._log(f"IP 位置: {location}")

            # 2. 创建邮箱
            self._log("2. 开个新邮箱，准备收信...")
            self._raise_if_cancelled("任务已取消，停止执行注册流程")
            if not self._create_email():
                result.error_message = "创建邮箱失败"
                return result

            result.email = self.email

            # 3. 准备首轮授权流程
            did, sen_token = self._prepare_authorize_flow("首次授权")
            if not did:
                result.error_message = "获取 Device ID 失败"
                return result
            if not sen_token:
                result.error_message = "Sentinel POW 验证失败"
                return result

            # 4. 提交注册入口邮箱
            self._log("4. 递上邮箱，看看 OpenAI 这球怎么接...")
            self._raise_if_cancelled("任务已取消，停止执行注册流程")
            signup_result = self._submit_signup_form(did, sen_token)
            if not signup_result.success:
                result.error_message = f"提交注册表单失败: {signup_result.error_message}"
                return result

            if self._is_existing_account:
                self._log("检测到这是老朋友账号，直接切去登录拿 token，不走弯路")
            else:
                self._log("5. 设置密码，别让小偷偷笑...")
                self._raise_if_cancelled("任务已取消，停止执行注册流程")
                password_ok, _ = self._register_password_with_retry(did, sen_token)
                if not password_ok:
                    recovered_existing, recovery_error = self._recover_after_registration_conflict()
                    if recovered_existing:
                        password_ok = True
                    else:
                        result.error_message = recovery_error or "娉ㄥ唽瀵嗙爜澶辫触"
                        return result

                if self._is_existing_account:
                    if not self._complete_token_exchange(result):
                        return self._sync_add_phone_result(result)

                    self._log("=" * 60)
                    self._log("鐧诲綍鎴愬姛锛岃€佹湅鍙嬮『鍒╁洖瀹?")
                    self._log(f"閭: {result.email}")
                    self._log(f"Account ID: {result.account_id}")
                    self._log(f"Workspace ID: {result.workspace_id}")
                    self._log("=" * 60)

                    if not enforce_refresh_token_requirement(result, subject="当前账号"):
                        self._log(result.error_message, "warning")
                        return result

                    result.success = True
                    result.metadata = {
                        "email_service": self.email_service.service_type.value,
                        "proxy_used": self.proxy_url,
                        "registered_at": datetime.now().isoformat(),
                        "is_existing_account": self._is_existing_account,
                        "token_acquired_via_relogin": self._token_acquisition_requires_login,
                    }
                    return result
                if not password_ok:
                    result.error_message = "注册密码失败"
                    return result

                self._log("6. 催一下注册验证码出门，邮差该冲刺了...")
                self._raise_if_cancelled("任务已取消，停止执行注册流程")
                if not self._send_verification_code():
                    result.error_message = "发送验证码失败"
                    return result

                self._log("7. 等验证码飞来，邮箱请注意查收...")
                self._raise_if_cancelled("任务已取消，停止执行注册流程")
                code = self._get_verification_code()
                if not code:
                    result.error_message = "获取验证码失败"
                    return result

                self._log("8. 对一下验证码，看看是不是本人...")
                self._raise_if_cancelled("任务已取消，停止执行注册流程")
                if self._validate_verification_code(code) is None:
                    result.error_message = "验证验证码失败"
                    return result

                self._log("9. 给账号办个正式户口，名字写档案里...")
                self._raise_if_cancelled("任务已取消，停止执行注册流程")
                if not self._create_user_account():
                    result.error_message = "创建用户账户失败"
                    return result

                # 优先尝试在当前注册 OAuth 上下文中直接完成授权（不走二次登录）
                self._log("10. 注册已完成，尝试在当前 OAuth 上下文中直接完成授权...")
                self._raise_if_cancelled("任务已取消，停止执行注册流程")
                if self._try_direct_authorize_after_registration(result):
                    self._log("=" * 60)
                    self._log("注册成功，账号已经稳稳落地，可以开香槟了")
                    self._log(f"邮箱: {result.email}")
                    self._log(f"Account ID: {result.account_id}")
                    self._log(f"Workspace ID: {result.workspace_id}")
                    self._log("=" * 60)

                    if not enforce_refresh_token_requirement(result, subject="当前账号"):
                        self._log(result.error_message, "warning")
                        return result

                    result.success = True
                    result.metadata = {
                        "email_service": self.email_service.service_type.value,
                        "proxy_used": self.proxy_url,
                        "registered_at": datetime.now().isoformat(),
                        "is_existing_account": False,
                        "token_acquired_via_relogin": False,
                    }
                    return result

                # 直接授权失败（非 add_phone 阻塞），回退到二次登录
                if str(result.error_code or "").strip().lower() == "add_phone_required" or "add_phone" in str(result.error_message or ""):
                    return self._sync_add_phone_result(result)
                self._log("直接授权未成功，回退到二次登录流程...", "warning")
                result.error_message = ""

                self._raise_if_cancelled("任务已取消，停止执行注册流程")
                login_ready, login_error = self._restart_login_flow()
                if not login_ready:
                    result.error_message = login_error
                    return self._sync_add_phone_result(result)

            self._raise_if_cancelled("任务已取消，停止执行注册流程")
            if not self._complete_token_exchange(result):
                return self._sync_add_phone_result(result)

            # 10. 完成
            self._log("=" * 60)
            if self._is_existing_account:
                self._log("登录成功，老朋友顺利回家")
            else:
                self._log("注册成功，账号已经稳稳落地，可以开香槟了")
            self._log(f"邮箱: {result.email}")
            self._log(f"Account ID: {result.account_id}")
            self._log(f"Workspace ID: {result.workspace_id}")
            self._log("=" * 60)

            if not enforce_refresh_token_requirement(result, subject="当前账号"):
                self._log(result.error_message, "warning")
                return result

            result.success = True
            result.metadata = {
                "email_service": self.email_service.service_type.value,
                "proxy_used": self.proxy_url,
                "registered_at": datetime.now().isoformat(),
                "is_existing_account": self._is_existing_account,
                "token_acquired_via_relogin": self._token_acquisition_requires_login,
            }

            return result

        except RegistrationCancelledError as e:
            self._log(f"注册流程已取消: {e}", "warning")
            result.error_message = str(e) or "任务已取消"
            return self._sync_add_phone_result(result)
        except Exception as e:
            self._log(f"注册过程中发生未预期错误: {e}", "error")
            result.error_message = str(e)
            return self._sync_add_phone_result(result)

    def save_to_database(self, result: RegistrationResult) -> bool:
        """
        保存注册结果到数据库

        Args:
            result: 注册结果

        Returns:
            是否保存成功
        """
        if not result.success:
            return False
        if not enforce_refresh_token_requirement(result, subject="当前账号"):
            self._log(result.error_message, "warning")
            return False

        try:
            # 获取默认 client_id
            settings = get_settings()

            with get_db() as db:
                # 保存账户信息
                account = crud.create_account(
                    db,
                    email=result.email,
                    password=result.password,
                    client_id=settings.openai_client_id,
                    session_token=result.session_token,
                    cookies=result.cookies,
                    email_service=self.email_service.service_type.value,
                    email_service_id=self.email_info.get("service_id") if self.email_info else None,
                    account_id=result.account_id,
                    workspace_id=result.workspace_id,
                    access_token=result.access_token,
                    refresh_token=result.refresh_token,
                    id_token=result.id_token,
                    proxy_used=self.proxy_url,
                    extra_data=result.metadata,
                    source=result.source
                )

                self._log(f"账户已存进数据库，落袋为安，ID: {account.id}")
                return True

        except Exception as e:
            self._log(f"保存到数据库失败: {e}", "error")
            return False
