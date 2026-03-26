"""
浏览器自动化注册引擎
使用 camoufox 进行有头注册，规避协议注册的风控封禁
"""

import json
import logging
import secrets
from datetime import datetime
from typing import Optional, Dict, Any, Callable
from urllib.parse import urlparse

from .register import RegistrationResult
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

                # 拦截 OAuth 回调，提取 code
                def _on_callback(route):
                    nonlocal callback_url
                    callback_url = route.request.url
                    route.fulfill(
                        status=200,
                        content_type="text/html",
                        body="<h1>注册完成，可关闭此窗口</h1>",
                    )

                page.route("**/auth/callback*", _on_callback)

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
                self._step_post_signup(page)

                # 给回调路由一些时间触发
                page.wait_for_timeout(3000)
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
        result.success = True
        result.access_token = token_data.get("access_token", "")
        result.refresh_token = token_data.get("refresh_token", "")
        result.id_token = token_data.get("id_token", "")
        result.account_id = token_data.get("account_id", "")
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
            bd.fill(info["birthdate"])
        except Exception:
            pass  # 生日字段可能不存在或为其他形式

        self._click_submit(page)
        page.wait_for_timeout(3000)

    def _step_post_signup(self, page):
        """处理注册后可能出现的 consent / workspace 选择等页面"""
        self._log("9. 处理后续确认页面...")
        for _ in range(6):
            if "/auth/callback" in page.url:
                return
            clicked = False
            for label in ("Continue", "Agree", "Accept", "继续", "同意", "Stay logged in"):
                btn = page.locator(f'button:has-text("{label}")').first
                try:
                    btn.wait_for(state="visible", timeout=1500)
                    btn.click()
                    page.wait_for_timeout(2000)
                    clicked = True
                    break
                except Exception:
                    continue
            if not clicked:
                page.wait_for_timeout(2000)

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
