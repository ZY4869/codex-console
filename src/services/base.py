"""
邮箱服务抽象基类
所有邮箱服务实现的基类
"""

import abc
import logging
import re
import secrets
import string
from typing import Optional, Dict, Any, List
from enum import Enum
from functools import wraps

from ..config.constants import EmailServiceType


logger = logging.getLogger(__name__)

VERIFICATION_EMAIL_KEYWORDS = ("openai", "xai", "x.ai", "grok")
ALNUM_LOCAL_PART_PATTERN = re.compile(r"^[A-Za-z0-9]+$")
ALNUM_PREFIX_REQUEST_KEYS = ("name", "prefix", "local", "alias")
ALNUM_PREFIX_POLICY_EXEMPT_TYPES = {
    EmailServiceType.OUTLOOK,
    EmailServiceType.IMAP_MAIL,
}


class EmailServiceError(Exception):
    """邮箱服务异常"""
    pass


class EmailServiceStatus(Enum):
    """邮箱服务状态"""
    HEALTHY = "healthy"
    DEGRADED = "degraded"
    UNAVAILABLE = "unavailable"


class BaseEmailService(abc.ABC):
    """
    邮箱服务抽象基类

    所有邮箱服务必须实现此接口
    """

    def __init__(self, service_type: EmailServiceType, name: str = None):
        """
        初始化邮箱服务

        Args:
            service_type: 服务类型
            name: 服务名称
        """
        self.service_type = service_type
        self.name = name or f"{service_type.value}_service"
        self._status = EmailServiceStatus.HEALTHY
        self._last_error = None

    @property
    def status(self) -> EmailServiceStatus:
        """获取服务状态"""
        return self._status

    @property
    def last_error(self) -> Optional[str]:
        """获取最后一次错误信息"""
        return self._last_error

    @abc.abstractmethod
    def create_email(self, config: Dict[str, Any] = None) -> Dict[str, Any]:
        """
        创建新邮箱地址

        Args:
            config: 配置参数，如邮箱前缀、域名等

        Returns:
            包含邮箱信息的字典，至少包含:
            - email: 邮箱地址
            - service_id: 邮箱服务中的 ID
            - token/credentials: 访问凭证（如果需要）

        Raises:
            EmailServiceError: 创建失败
        """
        pass

    @abc.abstractmethod
    def get_verification_code(
        self,
        email: str,
        email_id: str = None,
        timeout: int = 120,
        pattern: str = r"(?<!\d)(\d{6})(?!\d)",
        otp_sent_at: Optional[float] = None,
    ) -> Optional[str]:
        """
        获取验证码

        Args:
            email: 邮箱地址
            email_id: 邮箱服务中的 ID（如果需要）
            timeout: 超时时间（秒）
            pattern: 验证码正则表达式
            otp_sent_at: OTP 发送时间戳，用于过滤旧邮件

        Returns:
            验证码字符串，如果超时或未找到返回 None

        Raises:
            EmailServiceError: 服务错误
        """
        pass

    @abc.abstractmethod
    def list_emails(self, **kwargs) -> List[Dict[str, Any]]:
        """
        列出所有邮箱（如果服务支持）

        Args:
            **kwargs: 其他参数

        Returns:
            邮箱列表

        Raises:
            EmailServiceError: 服务错误
        """
        pass

    @abc.abstractmethod
    def delete_email(self, email_id: str) -> bool:
        """
        删除邮箱

        Args:
            email_id: 邮箱服务中的 ID

        Returns:
            是否删除成功

        Raises:
            EmailServiceError: 服务错误
        """
        pass

    @abc.abstractmethod
    def check_health(self) -> bool:
        """
        检查服务健康状态

        Returns:
            服务是否健康

        Note:
            此方法不应抛出异常，应捕获异常并返回 False
        """
        pass

    def get_email_info(self, email_id: str) -> Optional[Dict[str, Any]]:
        """
        获取邮箱信息（可选实现）

        Args:
            email_id: 邮箱服务中的 ID

        Returns:
            邮箱信息字典，如果不存在返回 None
        """
        # 默认实现：遍历列表查找
        for email_info in self.list_emails():
            if email_info.get("id") == email_id:
                return email_info
        return None

    def wait_for_email(
        self,
        email: str,
        email_id: str = None,
        timeout: int = 120,
        check_interval: int = 3,
        expected_sender: str = None,
        expected_subject: str = None
    ) -> Optional[Dict[str, Any]]:
        """
        等待并获取邮件（可选实现）

        Args:
            email: 邮箱地址
            email_id: 邮箱服务中的 ID
            timeout: 超时时间（秒）
            check_interval: 检查间隔（秒）
            expected_sender: 期望的发件人（包含检查）
            expected_subject: 期望的主题（包含检查）

        Returns:
            邮件信息字典，如果超时返回 None
        """
        import time
        from datetime import datetime

        start_time = time.time()
        last_email_id = None

        while time.time() - start_time < timeout:
            try:
                emails = self.list_emails()
                for email_info in emails:
                    email_data = email_info.get("email", {})
                    current_email_id = email_info.get("id")

                    # 检查是否是新的邮件
                    if last_email_id and current_email_id == last_email_id:
                        continue

                    # 检查邮箱地址
                    if email_data.get("address") != email:
                        continue

                    # 获取邮件列表
                    messages = self.get_email_messages(email_id or current_email_id)
                    for message in messages:
                        # 检查发件人
                        if expected_sender and expected_sender not in message.get("from", ""):
                            continue

                        # 检查主题
                        if expected_subject and expected_subject not in message.get("subject", ""):
                            continue

                        # 返回邮件信息
                        return {
                            "id": message.get("id"),
                            "from": message.get("from"),
                            "subject": message.get("subject"),
                            "content": message.get("content"),
                            "received_at": message.get("received_at"),
                            "email_info": email_info
                        }

                    # 更新最后检查的邮件 ID
                    if messages:
                        last_email_id = current_email_id

            except Exception as e:
                logger.warning(f"等待邮件时出错: {e}")

            time.sleep(check_interval)

        return None

    def get_email_messages(self, email_id: str, **kwargs) -> List[Dict[str, Any]]:
        """
        获取邮箱中的邮件列表（可选实现）

        Args:
            email_id: 邮箱服务中的 ID
            **kwargs: 其他参数

        Returns:
            邮件列表

        Note:
            这是可选方法，某些服务可能不支持
        """
        raise NotImplementedError("此邮箱服务不支持获取邮件列表")

    def get_message_content(self, email_id: str, message_id: str) -> Optional[Dict[str, Any]]:
        """
        获取邮件内容（可选实现）

        Args:
            email_id: 邮箱服务中的 ID
            message_id: 邮件 ID

        Returns:
            邮件内容字典

        Note:
            这是可选方法，某些服务可能不支持
        """
        raise NotImplementedError("此邮箱服务不支持获取邮件内容")

    def update_status(self, success: bool, error: Exception = None):
        """
        更新服务状态

        Args:
            success: 操作是否成功
            error: 错误信息
        """
        if success:
            self._status = EmailServiceStatus.HEALTHY
            self._last_error = None
        else:
            self._status = EmailServiceStatus.DEGRADED
            if error:
                self._last_error = str(error)

    def __str__(self) -> str:
        """字符串表示"""
        return f"{self.name} ({self.service_type.value})"


def looks_like_verification_email(*parts: str) -> bool:
    text = "\n".join(str(part or "") for part in parts).lower()
    return any(keyword in text for keyword in VERIFICATION_EMAIL_KEYWORDS)


def _load_email_prefix_alnum_only_setting() -> bool:
    try:
        from ..config.settings import get_settings

        settings = get_settings()
        return bool(getattr(settings, "registration_email_prefix_alnum_only", True))
    except Exception:
        return True


def _generate_alnum_local_part(length: int = 10) -> str:
    alphabet = string.ascii_letters + string.digits
    return "".join(secrets.choice(alphabet) for _ in range(max(1, length)))


def _sanitize_local_part(value: Any) -> str:
    text = str(value or "").strip()
    if not text:
        return text
    sanitized = re.sub(r"[^A-Za-z0-9]+", "", text)
    return sanitized or _generate_alnum_local_part()


def _apply_email_prefix_policy_to_request_config(config: Any) -> Any:
    if not _load_email_prefix_alnum_only_setting():
        return config
    if not isinstance(config, dict):
        return config

    normalized = dict(config)

    for key in ALNUM_PREFIX_REQUEST_KEYS:
        value = normalized.get(key)
        if value is None or not str(value).strip():
            continue
        normalized[key] = _sanitize_local_part(value)

    for key in ("address", "email"):
        value = normalized.get(key)
        if value is None:
            continue
        text = str(value).strip()
        if not text:
            continue
        local_part, separator, domain = text.partition("@")
        sanitized_local_part = _sanitize_local_part(local_part)
        normalized[key] = f"{sanitized_local_part}{separator}{domain}" if separator else sanitized_local_part

    return normalized


def _validate_created_email_prefix(service_type: EmailServiceType, email_info: Any):
    if service_type in ALNUM_PREFIX_POLICY_EXEMPT_TYPES:
        return
    if not _load_email_prefix_alnum_only_setting():
        return
    if not isinstance(email_info, dict):
        return

    email = str(email_info.get("email") or email_info.get("address") or "").strip()
    if not email:
        return

    local_part = email.split("@", 1)[0].strip()
    if local_part and ALNUM_LOCAL_PART_PATTERN.fullmatch(local_part):
        return

    raise EmailServiceError("邮箱前缀策略要求创建出的邮箱前缀只能包含字母和数字")


class EmailServiceFactory:
    """邮箱服务工厂"""

    _registry: Dict[EmailServiceType, type] = {}

    @classmethod
    def register(cls, service_type: EmailServiceType, service_class: type):
        """
        注册邮箱服务类

        Args:
            service_type: 服务类型
            service_class: 服务类
        """
        if not issubclass(service_class, BaseEmailService):
            raise TypeError(f"{service_class} 必须是 BaseEmailService 的子类")
        cls._registry[service_type] = service_class
        logger.info(f"注册邮箱服务: {service_type.value} -> {service_class.__name__}")

    @classmethod
    def create(
        cls,
        service_type: EmailServiceType,
        config: Dict[str, Any],
        name: str = None
    ) -> BaseEmailService:
        """
        创建邮箱服务实例

        Args:
            service_type: 服务类型
            config: 服务配置
            name: 服务名称

        Returns:
            邮箱服务实例

        Raises:
            ValueError: 服务类型未注册或配置无效
        """
        if service_type not in cls._registry:
            raise ValueError(f"未注册的服务类型: {service_type.value}")

        service_class = cls._registry[service_type]
        try:
            instance = service_class(config, name)
            original_create_email = instance.create_email

            @wraps(original_create_email)
            def create_email_with_prefix_policy(*args, **kwargs):
                if service_type in ALNUM_PREFIX_POLICY_EXEMPT_TYPES:
                    return original_create_email(*args, **kwargs)

                patched_args = list(args)
                patched_kwargs = dict(kwargs)

                if patched_args:
                    patched_args[0] = _apply_email_prefix_policy_to_request_config(patched_args[0])
                elif "config" in patched_kwargs:
                    patched_kwargs["config"] = _apply_email_prefix_policy_to_request_config(
                        patched_kwargs.get("config")
                    )

                payload = original_create_email(*patched_args, **patched_kwargs)
                _validate_created_email_prefix(service_type, payload)
                return payload

            instance.create_email = create_email_with_prefix_policy
            return instance
        except Exception as e:
            raise ValueError(f"创建邮箱服务失败: {e}")

    @classmethod
    def get_available_services(cls) -> List[EmailServiceType]:
        """
        获取所有已注册的服务类型

        Returns:
            已注册的服务类型列表
        """
        return list(cls._registry.keys())

    @classmethod
    def get_service_class(cls, service_type: EmailServiceType) -> Optional[type]:
        """
        获取服务类

        Args:
            service_type: 服务类型

        Returns:
            服务类，如果未注册返回 None
        """
        return cls._registry.get(service_type)


# 简化的工厂函数
def create_email_service(
    service_type: EmailServiceType,
    config: Dict[str, Any],
    name: str = None
) -> BaseEmailService:
    """
    创建邮箱服务（简化工厂函数）

    Args:
        service_type: 服务类型
        config: 服务配置
        name: 服务名称

    Returns:
        邮箱服务实例
    """
    return EmailServiceFactory.create(service_type, config, name)
