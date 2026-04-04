"""
浏览器自动化注册引擎
使用 camoufox 进行有头注册，规避协议注册的风控封禁
"""

import json
import logging
import secrets
from datetime import datetime
from typing import Optional, Dict, Any, Callable
from urllib.parse import parse_qs, unquote, urlparse

from .register import RegistrationResult, enforce_refresh_token_requirement
from .openai.oauth import OAuthManager, OAuthStart
from ..services import BaseEmailService
from ..config.constants import (
    generate_random_user_info,
    OTP_CODE_PATTERN,
    DEFAULT_PASSWORD_LENGTH,
    PASSWORD_CHARSET,
)
from ..config.settings import get_settings
from ..database import crud
from ..database.session import get_db

logger = logging.getLogger(__name__)

# 页面等待超时（毫秒）
NAV_TIMEOUT = 30_000
ELEMENT_TIMEOUT = 15_000
OTP_POLL_TIMEOUT = 120
BROWSER_BIRTH_YEAR_MIN = 1995
BROWSER_BIRTH_YEAR_MAX = 2000


class BrowserRegistrationEngine:
    """使用 camoufox 浏览器自动化的注册引擎"""

    def __init__(
        self,
        email_service: BaseEmailService,
        proxy_url: Optional[str] = None,
        callback_logger: Optional[Callable[[str], None]] = None,
        task_uuid: Optional[str] = None,
    ):
        self.email_service = email_service
        self.proxy_url = proxy_url
        self.callback_logger = callback_logger or (lambda msg: logger.info(msg))
        self.task_uuid = task_uuid
        self.logs: list = []

        settings = get_settings()
        self.oauth_manager = OAuthManager(
            client_id=settings.openai_client_id,
            auth_url=settings.openai_auth_url,
            token_url=settings.openai_token_url,
            redirect_uri=settings.openai_redirect_uri,
            scope=settings.openai_scope,
            proxy_url=proxy_url,
        )

        self.email: Optional[str] = None
        self.password: Optional[str] = None
        self.email_info: Optional[Dict[str, Any]] = None
        self._cancelled = False
        self._browser_context = None  # 持有浏览器上下文引用，用于立即关闭

    # ------------------------------------------------------------------
    # 日志 & 工具
    # ------------------------------------------------------------------

    def _log(self, message: str, level: str = "info"):
        ts = datetime.now().strftime("%H:%M:%S")
        line = f"[{ts}] {message}"
        self.logs.append(line)
        self.callback_logger(line)
        if self.task_uuid:
            try:
                with get_db() as db:
                    crud.append_task_log(db, self.task_uuid, line)
            except Exception:
                pass
        getattr(logger, level, logger.info)(message)

    def cancel(self):
        """立即取消注册：关闭浏览器，所有 Playwright 操作会抛异常退出"""
        self._cancelled = True
        ctx = self._browser_context
        if ctx:
            try:
                ctx.close()
            except Exception:
                pass
        self._log("任务已取消，浏览器已关闭", "warning")

    def _check_cancelled(self):
        """检查是否已取消，若取消则抛异常中断流程"""
        if self._cancelled:
            raise RuntimeError("任务已取消")

    @staticmethod
    def _generate_password(length: int = DEFAULT_PASSWORD_LENGTH) -> str:
        return "".join(secrets.choice(PASSWORD_CHARSET) for _ in range(length))

    def _build_proxy_config(self) -> Optional[Dict[str, str]]:
        if not self.proxy_url:
            return None
        parsed = urlparse(self.proxy_url)
        cfg: Dict[str, str] = {
            "server": f"{parsed.scheme}://{parsed.hostname}:{parsed.port}"
        }
        if parsed.username:
            cfg["username"] = parsed.username or ""
            cfg["password"] = parsed.password or ""
        return cfg

    @staticmethod
    def _clamp_browser_birth_year(year: int) -> int:
        return max(BROWSER_BIRTH_YEAR_MIN, min(BROWSER_BIRTH_YEAR_MAX, int(year or BROWSER_BIRTH_YEAR_MAX)))

    @classmethod
    def _parse_birthdate_parts(cls, raw_value: str) -> Optional[tuple[int, int, int]]:
        raw = str(raw_value or "").strip()
        if not raw:
            return None

        normalized = raw.replace(".", "/").replace("-", "/")
        pieces = [piece.strip() for piece in normalized.split("/") if piece.strip()]
        if len(pieces) != 3 or not all(piece.isdigit() for piece in pieces):
            return None

        first, second, third = (int(piece) for piece in pieces)
        if len(pieces[0]) == 4 or first > 31:
            year, month, day = first, second, third
        elif len(pieces[2]) == 4 or third > 31:
            if first > 12 and second <= 12:
                day, month, year = first, second, third
            else:
                month, day, year = first, second, third
        else:
            return None

        if not (1 <= month <= 12 and 1 <= day <= 31):
            return None
        return year, month, day

    @classmethod
    def _build_browser_birthdate_candidates(cls, raw_birthdate: str) -> list[str]:
        parts = cls._parse_birthdate_parts(raw_birthdate)
        if not parts:
            return [str(raw_birthdate or "").strip()]

        year, month, day = parts
        safe_year = cls._clamp_browser_birth_year(year)
        candidates = [
            f"{safe_year:04d}-{month:02d}-{day:02d}",
            f"{month:02d}/{day:02d}/{safe_year:04d}",
            f"{safe_year:04d}/{month:02d}/{day:02d}",
            f"{month:02d}-{day:02d}-{safe_year:04d}",
        ]
        ordered: list[str] = []
        seen = set()
        for candidate in candidates:
            if candidate and candidate not in seen:
                seen.add(candidate)
                ordered.append(candidate)
        return ordered

    @classmethod
    def _repair_browser_birthdate_value(cls, observed_value: str, fallback_birthdate: str) -> str:
        fallback_parts = cls._parse_birthdate_parts(fallback_birthdate)
        if not fallback_parts:
            return str(fallback_birthdate or "").strip()

        fallback_year, fallback_month, fallback_day = fallback_parts
        safe_year = cls._clamp_browser_birth_year(fallback_year)
        observed = str(observed_value or "").strip()
        observed_parts = cls._parse_birthdate_parts(observed)
        if not observed_parts:
            return cls._build_browser_birthdate_candidates(fallback_birthdate)[0]

        observed_year, observed_month, observed_day = observed_parts
        month = observed_month if 1 <= observed_month <= 12 else fallback_month
        day = observed_day if 1 <= observed_day <= 31 else fallback_day

        separator = "/"
        for candidate_separator in ("/", "-", "."):
            if candidate_separator in observed:
                separator = candidate_separator
                break

        normalized = observed.replace(".", "/").replace("-", "/")
        pieces = [piece.strip() for piece in normalized.split("/") if piece.strip()]
        if len(pieces) == 3 and len(pieces[0]) == 4:
            return f"{safe_year:04d}{separator}{month:02d}{separator}{day:02d}"
        return f"{month:02d}{separator}{day:02d}{separator}{safe_year:04d}"

    @classmethod
    def _is_browser_birthdate_valid(cls, value: str) -> bool:
        parts = cls._parse_birthdate_parts(value)
        if not parts:
            return False
        year, _, _ = parts
        return BROWSER_BIRTH_YEAR_MIN <= year <= BROWSER_BIRTH_YEAR_MAX

    def _fill_birthdate_field(self, locator, raw_birthdate: str):
        last_error = None
        for candidate in self._build_browser_birthdate_candidates(raw_birthdate):
            try:
                locator.fill(candidate)
                observed = candidate
                try:
                    observed = str(locator.input_value() or "").strip() or candidate
                except Exception:
                    pass

                if self._is_browser_birthdate_valid(observed):
                    return

                repaired = self._repair_browser_birthdate_value(observed, raw_birthdate)
                if repaired and repaired != observed:
                    self._log(f"   检测到生日年份异常，自动修正为: {repaired}", "warning")
                    locator.fill(repaired)
                    if self._is_browser_birthdate_valid(repaired):
                        return
            except Exception as exc:
                last_error = exc

        if last_error is not None:
            raise last_error

    @classmethod
    def _extract_callback_candidate(cls, raw_url: str) -> str:
        candidate = str(raw_url or "").strip()
        if not candidate:
            return ""
        if "code=" in candidate and "state=" in candidate:
            return candidate

        try:
            parsed = urlparse(candidate)
        except Exception:
            return ""

        nested_keys = (
            "callbackUrl",
            "callback_url",
            "continue",
            "continue_url",
            "redirect_uri",
            "redirectUrl",
            "returnTo",
            "return_to",
        )
        query_groups = (
            parse_qs(parsed.query, keep_blank_values=True),
            parse_qs(parsed.fragment, keep_blank_values=True),
        )
        for group in query_groups:
            for key in nested_keys:
                for value in group.get(key, []):
                    nested = cls._extract_callback_candidate(unquote(str(value or "").strip()))
                    if nested:
                        return nested
        return ""

    def _click_post_signup_action(self, page, label: str) -> bool:
        selectors = [
            f'button:has-text("{label}")',
            f'a:has-text("{label}")',
            f'[role="button"]:has-text("{label}")',
        ]
        for selector in selectors:
            locator = page.locator(selector).first
            try:
                locator.wait_for(state="visible", timeout=1500)
                locator.click()
                return True
            except Exception:
                continue
        return False

    # ------------------------------------------------------------------
    # 主流程
    # ------------------------------------------------------------------

    def run(self) -> RegistrationResult:
        """执行完整的浏览器注册流程"""
        result = RegistrationResult(success=False, logs=self.logs)

        try:
            from camoufox.sync_api import Camoufox  # noqa: F811
        except ImportError:
            result.error_message = (
                "camoufox 未安装，请运行: pip install 'codex-console[grok-local]'"
            )
            self._log(result.error_message, "error")
            return result

        try:
            self._prepare_credentials(result)
            oauth_start = self._start_oauth()
            callback_url = self._run_browser_flow(Camoufox, oauth_start)
            self._exchange_tokens(result, callback_url, oauth_start)
        except Exception as e:
            if not result.error_message:
                result.error_message = f"浏览器注册失败: {e}"
            self._log(result.error_message, "error")

        return result

    def _prepare_credentials(self, result: RegistrationResult):
        """创建邮箱 & 生成密码"""
        self._log("1. 创建邮箱...")
        self.email_info = self.email_service.create_email()
        if not self.email_info or "email" not in self.email_info:
            raise RuntimeError("创建邮箱失败")
        self.email = self.email_info["email"]
        result.email = self.email
        self._log(f"   邮箱: {self.email}")

        self.password = self._generate_password()
        result.password = self.password

    def _start_oauth(self) -> OAuthStart:
        """生成 OAuth 授权 URL"""
        self._log("2. 生成 OAuth 授权链接...")
        return self.oauth_manager.start_oauth()

    def _run_browser_flow(self, Camoufox, oauth_start: OAuthStart) -> str:
        """启动浏览器并走完注册页面流程，返回 callback URL"""
        self._log("3. 启动 camoufox 浏览器...")
        proxy_config = self._build_proxy_config()
        callback_url: Optional[str] = None

        with Camoufox(headless=False, proxy=proxy_config) as browser:
            self._browser_context = browser  # 保存引用，供 cancel() 关闭

            try:
                page = browser.new_page()

                def _remember_callback(raw_url: str, source: str = "page"):
                    nonlocal callback_url
                    candidate = self._extract_callback_candidate(raw_url)
                    if candidate and not callback_url:
                        callback_url = candidate
                        self._log(f"   捕获到 OAuth 回调候选({source}): {candidate[:120]}")

                # 拦截 OAuth 回调，提取 code
                def _on_callback(route):
                    _remember_callback(route.request.url, "route")
                    route.fulfill(
                        status=200,
                        content_type="text/html",
                        body="<h1>注册完成，可关闭此窗口</h1>",
                    )

                page.route("**/auth/callback*", _on_callback)
                page.on("request", lambda request: _remember_callback(request.url, "request"))
                page.on("framenavigated", lambda frame: _remember_callback(frame.url, "navigation"))

                auth_url = oauth_start.auth_url + "&screen_hint=signup"
                self._log("4. 打开注册页面...")
                self._check_cancelled()
                page.goto(auth_url, wait_until="networkidle", timeout=NAV_TIMEOUT)

                self._check_cancelled()
                self._step_email(page)
                self._check_cancelled()
                self._step_password(page)
                self._check_cancelled()
                self._step_otp(page)
                self._check_cancelled()
                self._step_profile(page)
                self._check_cancelled()
                post_signup_callback = self._step_post_signup(page)
                if post_signup_callback:
                    callback_url = callback_url or post_signup_callback

                # 给回调路由一些时间触发
                page.wait_for_timeout(3000)
                _remember_callback(page.url, "final_url")
            finally:
                self._browser_context = None

        if not callback_url:
            raise RuntimeError("未捕获到 OAuth 回调，注册流程可能未走完")
        return callback_url

    def _exchange_tokens(
        self,
        result: RegistrationResult,
        callback_url: str,
        oauth_start: OAuthStart,
    ):
        """用 callback URL 换取 access / refresh / id token"""
        self._log("10. 交换 OAuth Token...")
        token_data = self.oauth_manager.handle_callback(
            callback_url=callback_url,
            expected_state=oauth_start.state,
            code_verifier=oauth_start.code_verifier,
        )
        result.access_token = token_data.get("access_token", "")
        result.refresh_token = token_data.get("refresh_token", "")
        result.id_token = token_data.get("id_token", "")
        result.account_id = token_data.get("account_id", "")
        if not enforce_refresh_token_requirement(result, subject="当前账号"):
            self._log(result.error_message, "warning")
            return
        result.success = True
        self._log("注册成功！")

    # ------------------------------------------------------------------
    # 页面交互步骤
    # ------------------------------------------------------------------

    def _click_submit(self, page, timeout: int = 10_000):
        """等待 Turnstile 通过后点击提交按钮，失败则 force click"""
        self._wait_turnstile(page)
        btn = page.locator('button[type="submit"]').first
        try:
            btn.click(timeout=timeout)
        except Exception:
            self._log("   普通点击失败，尝试强制点击...", "warning")
            btn.click(force=True, timeout=timeout)

    def _step_email(self, page):
        """填写邮箱并提交"""
        self._log("5. 填写邮箱...")
        email_input = page.locator(
            'input[name="email"], input[type="email"]'
        ).first
        email_input.wait_for(state="visible", timeout=ELEMENT_TIMEOUT)
        email_input.fill(self.email)

        self._click_submit(page)
        page.wait_for_url("**/create-account/password**", timeout=NAV_TIMEOUT)

    def _step_password(self, page):
        """填写密码并提交"""
        self._log("6. 填写密码...")
        pwd = page.locator(
            'input[name="password"], input[type="password"]'
        ).first
        pwd.wait_for(state="visible", timeout=ELEMENT_TIMEOUT)
        pwd.fill(self.password)

        self._click_submit(page)
        page.wait_for_url("**/email-verification**", timeout=NAV_TIMEOUT)

    def _step_otp(self, page):
        """获取邮箱验证码并填入"""
        self._log("7. 等待验证码...")
        code = self.email_service.get_verification_code(
            email=self.email,
            email_id=self.email_info.get("id") or self.email_info.get("service_id"),
            timeout=OTP_POLL_TIMEOUT,
            pattern=OTP_CODE_PATTERN,
        )
        if not code:
            raise RuntimeError("获取邮箱验证码超时")
        self._log(f"   验证码: {code}")

        # 单输入框 或 多个单字符输入框
        single = page.locator('input[name="code"], input[inputmode="numeric"]').first
        try:
            single.wait_for(state="visible", timeout=5000)
            single.fill(code)
        except Exception:
            inputs = page.locator(
                'input[type="tel"], input[autocomplete="one-time-code"]'
            )
            for i, ch in enumerate(code):
                if i < inputs.count():
                    inputs.nth(i).fill(ch)

        self._click_submit(page)
        page.wait_for_url("**/about-you**", timeout=NAV_TIMEOUT)

    def _step_profile(self, page):
        """填写个人信息（姓名 + 生日）"""
        self._log("8. 填写个人信息...")
        info = generate_random_user_info()

        name_input = page.locator(
            'input[name="name"], input[name="firstName"], input[name="full_name"]'
        ).first
        name_input.wait_for(state="visible", timeout=ELEMENT_TIMEOUT)
        name_input.fill(info["name"])

        bd = page.locator(
            'input[name="birthdate"], input[type="date"], input[name="birthday"]'
        ).first
        try:
            bd.wait_for(state="visible", timeout=3000)
            self._fill_birthdate_field(bd, info["birthdate"])
        except Exception:
            pass  # 生日字段可能不存在或为其他形式

        self._click_submit(page)
        page.wait_for_timeout(3000)

    def _step_post_signup(self, page):
        """处理注册后可能出现的 consent / workspace 选择等页面"""
        self._log("9. 处理后续确认页面...")
        action_labels = (
            "Continue",
            "Agree",
            "Accept",
            "Continue to ChatGPT",
            "Log in",
            "Login",
            "继续",
            "同意",
            "Stay logged in",
        )
        for _ in range(8):
            callback_candidate = self._extract_callback_candidate(page.url)
            if callback_candidate:
                return callback_candidate
            clicked = False
            for label in action_labels:
                if self._click_post_signup_action(page, label):
                    page.wait_for_timeout(2000)
                    clicked = True
                    break
            if not clicked:
                page.wait_for_timeout(2000)
        return self._extract_callback_candidate(page.url)

    # ------------------------------------------------------------------
    # Turnstile 处理
    # ------------------------------------------------------------------

    def _wait_turnstile(self, page, timeout_ms: int = 15_000):
        """等待 Turnstile 验证通过（camoufox 通常自动通过）"""
        try:
            frame = page.frame_locator(
                'iframe[src*="turnstile"], iframe[title*="Turnstile"]'
            ).first
            checkbox = frame.locator(
                'input[type="checkbox"], .cf-turnstile-checkbox, div[role="checkbox"]'
            )
            try:
                checkbox.wait_for(state="visible", timeout=3000)
                checkbox.click()
                page.wait_for_timeout(5000)
            except Exception:
                pass  # checkbox 未出现，可能已自动通过
        except Exception:
            # Turnstile 可能不存在或已自动通过
            pass

    # ------------------------------------------------------------------
    # 数据库保存（与 RegistrationEngine 同接口）
    # ------------------------------------------------------------------

    def save_to_database(self, result: RegistrationResult) -> bool:
        """保存注册结果到数据库"""
        if not result.success:
            return False
        if not enforce_refresh_token_requirement(result, subject="当前账号"):
            self._log(result.error_message, "warning")
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
                    email_service_id=(
                        self.email_info.get("service_id") if self.email_info else None
                    ),
                    account_id=result.account_id,
                    workspace_id=result.workspace_id,
                    access_token=result.access_token,
                    refresh_token=result.refresh_token,
                    id_token=result.id_token,
                    proxy_used=self.proxy_url,
                    extra_data=result.metadata,
                    source=result.source,
                )
                self._log(f"账户已存进数据库，ID: {account.id}")
                return True
        except Exception as e:
            self._log(f"保存到数据库失败: {e}", "error")
            return False
