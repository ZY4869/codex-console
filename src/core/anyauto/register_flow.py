"""
Any-auto-register 风格注册流程（V2）。
以状态机 + Session 复用为主，必要时回退 OAuth。
"""

from __future__ import annotations

import random
import secrets
import string
import time
from typing import Any, Callable, Dict, Optional

from .chatgpt_client import ChatGPTClient
from .oauth_client import OAuthClient
from .utils import decode_jwt_payload, generate_random_birthday, generate_random_name
from ..register import (
    MISSING_REFRESH_TOKEN_ERROR_CODE,
    build_missing_refresh_token_error,
    has_required_refresh_token,
)
from ...config.constants import DEFAULT_PASSWORD_LENGTH, PASSWORD_CHARSET, PASSWORD_SPECIAL_CHARSET
from ...config.settings import get_settings


class EmailServiceAdapter:
    """将 codex-console 邮箱服务适配成 any-auto-register 预期接口。"""

    def __init__(self, email_service, email: str, email_id: Optional[str], log_fn: Callable[[str], None]):
        self.es = email_service
        self.email = email
        self.email_id = email_id
        self.log_fn = log_fn or (lambda _msg: None)
        self._used_codes: set[str] = set()

    def wait_for_verification_code(self, email, timeout=60, otp_sent_at=None, exclude_codes=None):
        exclude = set(exclude_codes or [])
        exclude.update(self._used_codes)
        deadline = time.time() + max(1, int(timeout))
        sent_at = otp_sent_at or time.time()

        while time.time() < deadline:
            remaining = max(1, int(deadline - time.time()))
            code = self.es.get_verification_code(
                email=email,
                email_id=self.email_id,
                timeout=remaining,
                otp_sent_at=sent_at,
            )
            if not code:
                return None
            if code in exclude:
                exclude.add(code)
                continue
            self._used_codes.add(code)
            self.log_fn(f"成功获取验证码: {code}")
            return code
        return None


class AnyAutoRegistrationEngine:
    def __init__(
        self,
        email_service,
        proxy_url: Optional[str] = None,
        callback_logger: Optional[Callable[[str], None]] = None,
        max_retries: int = 3,
        browser_mode: str = "protocol",
        extra_config: Optional[Dict[str, Any]] = None,
        check_cancelled: Optional[Callable[[], bool]] = None,
    ):
        self.email_service = email_service
        self.proxy_url = proxy_url
        self.callback_logger = callback_logger or (lambda _msg: None)
        self.max_retries = max(1, int(max_retries or 1))
        self.browser_mode = browser_mode or "protocol"
        self.extra_config = dict(extra_config or {})
        self._check_cancelled = check_cancelled or (lambda: False)

        self.email: Optional[str] = None
        self.inbox_email: Optional[str] = None
        self.email_info: Optional[Dict[str, Any]] = None
        self.password: Optional[str] = None
        self.session = None
        self.device_id: Optional[str] = None

        self.same_email_retry_limit = max(1, int(self.extra_config.get("same_email_retry_limit", 4) or 4))
        self._reuse_email_on_next_attempt = False
        self._current_email_failure_streak = 0
        self._last_passwordless_oauth_error = ""
        self._last_oauth_enrichment_error = ""

    def _log(self, message: str, level: str = "info"):
        if self.callback_logger:
            self.callback_logger(message)

    def _is_cancel_requested(self) -> bool:
        try:
            return bool(self._check_cancelled())
        except Exception:
            return False

    def _raise_if_cancelled(self, reason: str = "任务已取消") -> None:
        if self._is_cancel_requested():
            raise RuntimeError(reason)

    def _sleep_interruptible(self, seconds: float) -> None:
        remaining = max(0.0, float(seconds or 0.0))
        while remaining > 0:
            self._raise_if_cancelled("任务在等待阶段被取消")
            chunk = min(0.2, remaining)
            time.sleep(chunk)
            remaining -= chunk

    def _clear_current_email(self, reset_failure_streak: bool = False) -> None:
        self.email = None
        self.inbox_email = None
        self.email_info = None
        if reset_failure_streak:
            self._current_email_failure_streak = 0

    def _prepare_email_for_attempt(self) -> Optional[str]:
        if self._reuse_email_on_next_attempt and self.email_info:
            raw_email = str((self.email_info or {}).get("email") or self.email or "").strip()
            if raw_email:
                normalized_email = raw_email.lower()
                self.inbox_email = raw_email
                self.email = normalized_email
                try:
                    self.email_info["email"] = normalized_email
                except Exception:
                    pass
                self._reuse_email_on_next_attempt = False
                self._log(
                    f"复用同一个邮箱继续重试 ({self._current_email_failure_streak}/{self.same_email_retry_limit}): {normalized_email}"
                )
                return normalized_email

            self._reuse_email_on_next_attempt = False
            self._clear_current_email(reset_failure_streak=True)

        self.password = None
        self.email_info = self.email_service.create_email()
        raw_email = str((self.email_info or {}).get("email") or "").strip()
        if not raw_email:
            return None

        normalized_email = raw_email.lower()
        self.inbox_email = raw_email
        self.email = normalized_email
        self._current_email_failure_streak = 0
        try:
            self.email_info["email"] = normalized_email
        except Exception:
            pass

        if raw_email != normalized_email:
            self._log(f"邮箱规范化: {raw_email} -> {normalized_email}")
        return normalized_email

    def _schedule_retry(self, attempt: int, reason: str, *, prefer_same_email: bool = False) -> bool:
        if attempt >= self.max_retries - 1:
            return False

        detail = str(reason or "").strip()
        if detail:
            self._log(detail)

        if prefer_same_email and self.email_info and self.email:
            self._current_email_failure_streak += 1
            if self._current_email_failure_streak < self.same_email_retry_limit:
                self._reuse_email_on_next_attempt = True
                self._log(
                    f"准备整流程重试，并复用同一个邮箱 ({self._current_email_failure_streak}/{self.same_email_retry_limit}): {self.email}"
                )
                return True

            self._log(
                f"同一个邮箱连续失败 {self._current_email_failure_streak}/{self.same_email_retry_limit}，判定该邮箱异常，下轮更换新邮箱: {self.email}"
            )
            self._reuse_email_on_next_attempt = False
            self._clear_current_email(reset_failure_streak=True)
            return True

        self._reuse_email_on_next_attempt = False
        self._clear_current_email(reset_failure_streak=True)
        self._log("准备整流程重试，下轮将重新创建邮箱")
        return True

    @staticmethod
    def _build_add_phone_failure_payload(error_message: str = "add_phone_required") -> Dict[str, Any]:
        detail = str(error_message or "add_phone_required").strip() or "add_phone_required"
        return {
            "success": False,
            "error_message": detail,
            "metadata": {
                "phone_verification_required": True,
                "token_pending": False,
                "oauth_error": detail,
            },
        }

    @staticmethod
    def _build_missing_refresh_token_failure_payload(subject: str = "当前账号") -> Dict[str, Any]:
        return {
            "success": False,
            "error_code": MISSING_REFRESH_TOKEN_ERROR_CODE,
            "error_message": build_missing_refresh_token_error(subject),
        }

    @staticmethod
    def _build_password(length: int) -> str:
        length = max(8, int(length or DEFAULT_PASSWORD_LENGTH))
        password_chars = [
            secrets.choice(string.ascii_lowercase),
            secrets.choice(string.ascii_uppercase),
            secrets.choice(string.digits),
            secrets.choice(PASSWORD_SPECIAL_CHARSET),
        ]
        password_chars.extend(secrets.choice(PASSWORD_CHARSET) for _ in range(length - len(password_chars)))
        secrets.SystemRandom().shuffle(password_chars)
        return "".join(password_chars)

    @staticmethod
    def _should_retry(message: str) -> bool:
        text = str(message or "").lower()
        retriable_markers = [
            "tls",
            "ssl",
            "curl: (35)",
            "authorize",
            "registration_disallowed",
            "http 400",
            "failed to create account",
            "创建账号失败",
            "未获取到 authorization code",
            "consent",
            "workspace",
            "organization",
            "otp",
            "验证码",
            "session",
            "accesstoken",
            "next-auth",
            "add_phone",
        ]
        return any(marker.lower() in text for marker in retriable_markers)

    @staticmethod
    def _extract_account_id_from_token(token: str) -> str:
        payload = decode_jwt_payload(token)
        if not isinstance(payload, dict):
            return ""
        auth_claims = payload.get("https://api.openai.com/auth") or {}
        for key in ("chatgpt_account_id", "account_id", "workspace_id"):
            value = str(auth_claims.get(key) or payload.get(key) or "").strip()
            if value:
                return value
        return ""

    @staticmethod
    def _is_phone_required_error(message: str) -> bool:
        text = str(message or "").lower()
        return any(
            marker in text
            for marker in (
                "add_phone",
                "add-phone",
                "phone",
                "phone required",
                "phone verification",
                "手机号",
            )
        )

    def _passwordless_oauth_reauth(
        self,
        chatgpt_client: ChatGPTClient,
        email: str,
        skymail_adapter: EmailServiceAdapter,
        oauth_config: Dict[str, Any],
    ) -> Optional[Dict[str, Any]]:
        self._last_passwordless_oauth_error = ""
        self._log("检测到 add_phone，尝试通过 passwordless OTP 补全 OAuth token...")
        oauth_client = OAuthClient(
            config=oauth_config,
            proxy=self.proxy_url,
            verbose=False,
            browser_mode=self.browser_mode,
        )
        oauth_client._log = self._log

        tokens = oauth_client.login_passwordless_and_get_tokens(
            email,
            chatgpt_client.device_id,
            chatgpt_client.ua,
            chatgpt_client.sec_ch_ua,
            chatgpt_client.impersonate,
            skymail_adapter,
        )
        if tokens and tokens.get("access_token"):
            return {
                "access_token": tokens.get("access_token", ""),
                "refresh_token": tokens.get("refresh_token", ""),
                "id_token": tokens.get("id_token", ""),
                "session": oauth_client.session,
            }

        self._last_passwordless_oauth_error = str(getattr(oauth_client, "last_error", "") or "").strip()
        if self._last_passwordless_oauth_error:
            self._log(f"Passwordless OAuth 失败: {self._last_passwordless_oauth_error}")
        return None

    @staticmethod
    def _extract_session_token_from_session(session) -> str:
        try:
            cookies = getattr(session, "cookies", None)
            jar = getattr(cookies, "jar", cookies)
            iterable = jar.items() if isinstance(jar, dict) else (jar or [])
            for cookie in iterable:
                if isinstance(cookie, tuple):
                    name, value = cookie
                else:
                    name = getattr(cookie, "name", "")
                    value = getattr(cookie, "value", "")
                if name == "__Secure-next-auth.session-token":
                    return str(value or "").strip()
        except Exception:
            return ""
        return ""

    @staticmethod
    def _extract_workspace_id_from_oauth_client(oauth_client: OAuthClient) -> str:
        try:
            session_data = oauth_client._decode_oauth_session_cookie()
        except Exception:
            return ""
        workspaces = (session_data or {}).get("workspaces") or []
        if not workspaces:
            return ""
        return str((workspaces[0] or {}).get("id") or "").strip()

    def _build_oauth_success_payload(
        self,
        chatgpt_client: ChatGPTClient,
        oauth_client: OAuthClient,
        tokens: Dict[str, Any],
        fallback_account_id: str = "",
    ) -> Dict[str, Any]:
        workspace_id = self._extract_workspace_id_from_oauth_client(oauth_client)
        if workspace_id:
            self._log(f"OAuth Workspace ID: {workspace_id}")
        session_token = self._extract_session_token_from_session(oauth_client.session)
        account_id = self._extract_account_id_from_token(tokens.get("access_token", ""))
        account_id = account_id or workspace_id or str(fallback_account_id or "").strip()
        if not account_id:
            device_prefix = str(getattr(chatgpt_client, "device_id", "") or "")[:8]
            account_id = f"v2_acct_{device_prefix}" if device_prefix else ""
        return {
            "access_token": tokens.get("access_token", ""),
            "refresh_token": tokens.get("refresh_token", ""),
            "id_token": tokens.get("id_token", ""),
            "account_id": account_id,
            "workspace_id": workspace_id or account_id,
            "session_token": session_token,
        }

    def _best_effort_enrich_session_tokens(
        self,
        chatgpt_client: ChatGPTClient,
        email: str,
        password: str,
        skymail_adapter: EmailServiceAdapter,
        oauth_config: Dict[str, Any],
        fallback_account_id: str = "",
    ) -> Optional[Dict[str, Any]]:
        self._log("Session tokens missing refresh_token, trying OAuth enrichment...")
        self._last_oauth_enrichment_error = ""
        oauth_client = OAuthClient(
            config=oauth_config,
            proxy=self.proxy_url,
            verbose=False,
            browser_mode=self.browser_mode,
        )
        oauth_client._log = self._log
        oauth_client.session = chatgpt_client.session
        try:
            tokens = oauth_client.login_and_get_tokens(
                email,
                password,
                chatgpt_client.device_id,
                chatgpt_client.ua,
                chatgpt_client.sec_ch_ua,
                chatgpt_client.impersonate,
                skymail_adapter,
            )
        except Exception as exc:
            self._last_oauth_enrichment_error = str(exc).strip()
            self._log(f"OAuth enrichment raised exception, keep session tokens: {exc}")
            return None
        if not tokens or not tokens.get("refresh_token"):
            last_error = str(getattr(oauth_client, "last_error", "") or "refresh_token missing").strip()
            self._last_oauth_enrichment_error = last_error
            self._log(f"OAuth enrichment did not return refresh_token, keep session tokens: {last_error}")
            return None
        self.session = oauth_client.session or self.session
        self._last_oauth_enrichment_error = ""
        self._log("OAuth enrichment succeeded, refresh_token acquired")
        return self._build_oauth_success_payload(
            chatgpt_client,
            oauth_client,
            tokens,
            fallback_account_id=fallback_account_id,
        )

    def run(self):
        """执行 any-auto-register 风格注册流程。"""
        last_error = ""
        settings = get_settings()
        password_len = int(
            getattr(settings, "registration_default_password_length", DEFAULT_PASSWORD_LENGTH)
            or DEFAULT_PASSWORD_LENGTH
        )
        configured_same_email_retry_limit = (self.extra_config or {}).get(
            "same_email_retry_limit",
            getattr(settings, "registration_same_email_retry_limit", self.same_email_retry_limit),
        )
        self.same_email_retry_limit = max(1, int(configured_same_email_retry_limit or 1))
        # Keep the inner flow budget large enough to fully honor same-email retries.
        self.max_retries = max(self.max_retries, self.same_email_retry_limit)

        oauth_config = {
            "oauth_issuer": str(getattr(settings, "openai_auth_url", "") or "https://auth.openai.com"),
            "oauth_client_id": str(getattr(settings, "openai_client_id", "") or "app_EMoamEEZ73f0CkXaXp7hrann"),
            "oauth_redirect_uri": str(getattr(settings, "openai_redirect_uri", "") or "http://localhost:1455/auth/callback"),
        }
        oauth_config.update(dict(self.extra_config or {}))
        adaptive_register_recovery = bool((self.extra_config or {}).get("adaptive_register_recovery"))

        for attempt in range(self.max_retries):
            try:
                self._raise_if_cancelled("任务已取消")
                if attempt == 0:
                    self._log("=" * 60)
                    self._log("开始注册流程 V2 (Session 复用直取 AccessToken)")
                    self._log(f"请求模式: {self.browser_mode}")
                    self._log("=" * 60)
                else:
                    backoff = min(2 ** attempt, 10) + random.uniform(0.5, 2.0)
                    self._log(f"整流程重试 {attempt + 1}/{self.max_retries}，等待 {backoff:.1f}s ...")
                    self._sleep_interruptible(backoff)

                normalized_email = self._prepare_email_for_attempt()
                if not normalized_email:
                    last_error = "创建邮箱失败"
                    return {"success": False, "error_message": last_error}

                self.password = self.password or self._build_password(password_len)
                first_name, last_name = generate_random_name()
                birthdate = generate_random_birthday()
                self._log(f"邮箱: {normalized_email}, 密码: {self.password}")
                self._log(f"注册信息: {first_name} {last_name}, 生日: {birthdate}")

                email_id = (self.email_info or {}).get("service_id")
                skymail_adapter = EmailServiceAdapter(self.email_service, normalized_email, email_id, self._log)

                self._raise_if_cancelled("任务已取消")
                chatgpt_client = ChatGPTClient(
                    proxy=self.proxy_url,
                    verbose=False,
                    browser_mode=self.browser_mode,
                )
                chatgpt_client._log = self._log

                self._log("步骤 1/2: 执行注册状态机...")
                success, msg = chatgpt_client.register_complete_flow(
                    normalized_email,
                    self.password,
                    first_name,
                    last_name,
                    birthdate,
                    skymail_adapter,
                    adaptive_register_recovery=adaptive_register_recovery,
                )
                if not success:
                    last_error = f"注册流失败: {msg}"
                    if self._should_retry(msg) and self._schedule_retry(
                        attempt,
                        f"注册流失败，准备整流程重试: {msg}",
                        prefer_same_email=True,
                    ):
                        continue
                    return {"success": False, "error_message": last_error}

                add_phone_required = "add_phone" in str(msg or "").lower()
                try:
                    state = getattr(chatgpt_client, "last_registration_state", None)
                    if state:
                        target = f"{getattr(state, 'continue_url', '')} {getattr(state, 'current_url', '')}".lower()
                        if "add-phone" in target or "add_phone" in str(getattr(state, "page_type", "")).lower():
                            add_phone_required = True
                except Exception:
                    pass

                self.session = chatgpt_client.session
                self.device_id = chatgpt_client.device_id

                if add_phone_required:
                    self._raise_if_cancelled("任务已取消")
                    pwdless = self._passwordless_oauth_reauth(
                        chatgpt_client,
                        normalized_email,
                        skymail_adapter,
                        oauth_config,
                    )
                    if pwdless and pwdless.get("access_token"):
                        self.session = pwdless.get("session") or self.session
                        if not has_required_refresh_token(pwdless.get("refresh_token", "")):
                            return self._build_missing_refresh_token_failure_payload()
                        return {
                            "success": True,
                            "access_token": pwdless.get("access_token", ""),
                            "refresh_token": pwdless.get("refresh_token", ""),
                            "id_token": pwdless.get("id_token", ""),
                        }

                    last_error = self._last_passwordless_oauth_error or "add_phone_required"
                    if self._schedule_retry(
                        attempt,
                        f"检测到 add_phone，按失败处理并更换邮箱重试: {last_error}",
                        prefer_same_email=False,
                    ):
                        continue
                    return self._build_add_phone_failure_payload(last_error)

                self._log("步骤 2/2: 优先复用注册会话提取 ChatGPT Session / AccessToken...")
                self._raise_if_cancelled("任务已取消")
                session_ok, session_result = chatgpt_client.reuse_session_and_get_tokens()
                if session_ok:
                    self._log("Token 提取完成！")
                    account_id = str(session_result.get("account_id", "") or "").strip()
                    if not account_id:
                        account_id = str(session_result.get("workspace_id", "") or "").strip()
                    if not account_id:
                        account_id = self._extract_account_id_from_token(session_result.get("access_token", ""))

                    merged_tokens: Dict[str, Any] = {}
                    if not str(session_result.get("refresh_token", "") or "").strip():
                        enriched_tokens = self._best_effort_enrich_session_tokens(
                            chatgpt_client,
                            normalized_email,
                            self.password or "",
                            skymail_adapter,
                            oauth_config,
                            fallback_account_id=account_id,
                        )
                        if enriched_tokens:
                            merged_tokens = enriched_tokens
                        elif self._is_phone_required_error(self._last_oauth_enrichment_error):
                            last_error = self._last_oauth_enrichment_error or "add_phone_required"
                            if self._schedule_retry(
                                attempt,
                                f"检测到 add_phone，按失败处理并更换邮箱重试: {last_error}",
                                prefer_same_email=False,
                            ):
                                continue
                            return self._build_add_phone_failure_payload(last_error)

                    account_id = account_id or str(merged_tokens.get("account_id", "") or "").strip()
                    workspace_id = str(session_result.get("workspace_id", "") or "").strip() or account_id
                    workspace_id = workspace_id or str(merged_tokens.get("workspace_id", "") or "").strip()
                    refresh_token = merged_tokens.get("refresh_token", "") or session_result.get("refresh_token", "")
                    if not has_required_refresh_token(refresh_token):
                        return self._build_missing_refresh_token_failure_payload()
                    return {
                        "success": True,
                        "access_token": merged_tokens.get("access_token", "") or session_result.get("access_token", ""),
                        "refresh_token": refresh_token,
                        "id_token": merged_tokens.get("id_token", "") or session_result.get("id_token", ""),
                        "session_token": session_result.get("session_token", "") or merged_tokens.get("session_token", ""),
                        "account_id": account_id,
                        "workspace_id": workspace_id,
                        "metadata": {
                            "auth_provider": session_result.get("auth_provider", ""),
                            "expires": session_result.get("expires", ""),
                            "user_id": session_result.get("user_id", ""),
                            "user": session_result.get("user") or {},
                            "account": session_result.get("account") or {},
                            "oauth_token_enriched": bool(merged_tokens),
                        },
                    }

                self._log(f"复用会话失败，回退到 OAuth 登录补全流程: {session_result}")
                tokens = None
                oauth_client = None
                for oauth_attempt in range(2):
                    self._raise_if_cancelled("任务已取消")
                    if oauth_attempt > 0:
                        self._log(f"同账号 OAuth 重试 {oauth_attempt + 1}/2 ...")
                        self._sleep_interruptible(1)

                    oauth_client = OAuthClient(
                        config=oauth_config,
                        proxy=self.proxy_url,
                        verbose=False,
                        browser_mode=self.browser_mode,
                    )
                    oauth_client._log = self._log
                    oauth_client.session = chatgpt_client.session

                    tokens = oauth_client.login_and_get_tokens(
                        normalized_email,
                        self.password,
                        chatgpt_client.device_id,
                        chatgpt_client.ua,
                        chatgpt_client.sec_ch_ua,
                        chatgpt_client.impersonate,
                        skymail_adapter,
                    )
                    if tokens and tokens.get("access_token"):
                        break

                    if self._is_phone_required_error(getattr(oauth_client, "last_error", "")):
                        break

                if tokens and tokens.get("access_token"):
                    self._log("OAuth 回退补全成功")
                    oauth_payload = self._build_oauth_success_payload(
                        chatgpt_client,
                        oauth_client,
                        tokens,
                    )
                    if not has_required_refresh_token(oauth_payload.get("refresh_token", "")):
                        return self._build_missing_refresh_token_failure_payload()
                    return {"success": True, **oauth_payload}

                if oauth_client and self._is_phone_required_error(getattr(oauth_client, "last_error", "")):
                    last_error = str(getattr(oauth_client, "last_error", "") or "add_phone_required").strip()
                    if self._schedule_retry(
                        attempt,
                        f"检测到 add_phone，按失败处理并更换邮箱重试: {last_error}",
                        prefer_same_email=False,
                    ):
                        continue
                    return self._build_add_phone_failure_payload(last_error)

                last_error = str(getattr(oauth_client, "last_error", "") or "").strip() or "获取最终 OAuth Tokens 失败"
                return {"success": False, "error_message": f"账号已创建成功，但 {last_error}"}

            except Exception as attempt_error:
                last_error = str(attempt_error)
                if self._should_retry(last_error) and self._schedule_retry(
                    attempt,
                    f"本轮出现异常，准备整流程重试: {last_error}",
                    prefer_same_email=True,
                ):
                    continue
                return {"success": False, "error_message": last_error}

        return {"success": False, "error_message": last_error or "注册失败"}
