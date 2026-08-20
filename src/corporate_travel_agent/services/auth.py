"""内部试用鉴权：scrypt 口令哈希、HMAC 签名会话令牌，以及用户文件加载。"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import os
import secrets
import stat
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from enum import StrEnum
from pathlib import Path
from typing import Any


class Role(StrEnum):
    """应用内角色：员工 / 审批人 / 管理员。"""

    EMPLOYEE = "employee"
    APPROVER = "approver"
    ADMIN = "admin"


class AuthenticationFailed(ValueError):
    """登录或令牌校验失败。"""


@dataclass(frozen=True, slots=True)
class UserAccount:
    """持久化用户账户：口令哈希、角色与可选员工 ID。"""

    user_id: str
    password_hash: str
    roles: frozenset[Role]
    employee_id: str | None = None
    active: bool = True


@dataclass(frozen=True, slots=True)
class UserIdentity:
    """已认证主体身份，供授权与审计使用。"""

    user_id: str
    roles: frozenset[Role]
    employee_id: str | None

    def has_role(self, role: Role) -> bool:
        """判断主体是否拥有指定角色。"""
        return role in self.roles


class AuthService:
    """内部试用鉴权服务：scrypt 口令校验与 HMAC 签名会话。"""

    def __init__(
        self,
        *,
        accounts: list[UserAccount] | None = None,
        signing_secret: str | bytes | None = None,
        token_ttl: timedelta = timedelta(hours=1),
        clock: Callable[[], datetime] | None = None,
        enabled: bool = True,
    ) -> None:
        self.enabled = enabled
        self._clock = clock or (lambda: datetime.now(UTC))
        self._token_ttl = token_ttl
        if not enabled:
            self._accounts: dict[str, UserAccount] = {}
            self._secret = b""
            return
        if not timedelta(minutes=5) <= token_ttl <= timedelta(hours=24):
            raise ValueError("token_ttl must be between 5 minutes and 24 hours")
        secret_bytes = (
            signing_secret.encode("utf-8")
            if isinstance(signing_secret, str)
            else signing_secret
        )
        if secret_bytes is None or len(secret_bytes) < 32:
            raise ValueError("AUTH_SIGNING_SECRET must contain at least 32 bytes")
        if not accounts:
            raise ValueError("At least one auth account is required")
        self._accounts = {}
        for account in accounts:
            if account.user_id in self._accounts:
                raise ValueError(f"Duplicate auth user {account.user_id}")
            if Role.EMPLOYEE in account.roles and not account.employee_id:
                raise ValueError(
                    f"Employee account {account.user_id} requires employee_id"
                )
            self._accounts[account.user_id] = account
        self._secret = secret_bytes
        self._dummy_password_hash = hash_password(
            "dummy-password-for-timing-equality",
            salt=b"auth-dummy-salt!",
        )

    @classmethod
    def disabled(cls) -> AuthService:
        """返回关闭鉴权的服务实例（开发模式）。"""
        return cls(enabled=False)

    @classmethod
    def from_environment(cls) -> AuthService:
        """按环境变量构造鉴权服务；未启用 AUTH_ENABLED 时返回 disabled 实例。"""
        if os.getenv("AUTH_ENABLED", "false").casefold() != "true":
            return cls.disabled()
        users_file = os.getenv("AUTH_USERS_FILE")
        signing_secret = os.getenv("AUTH_SIGNING_SECRET")
        if not users_file:
            raise RuntimeError("AUTH_ENABLED=true requires AUTH_USERS_FILE")
        if not signing_secret:
            raise RuntimeError("AUTH_ENABLED=true requires AUTH_SIGNING_SECRET")
        ttl_minutes = int(os.getenv("AUTH_TOKEN_TTL_MINUTES", "60"))
        return cls(
            accounts=load_accounts(users_file),
            signing_secret=signing_secret,
            token_ttl=timedelta(minutes=ttl_minutes),
        )

    def login(self, user_id: str, password: str) -> tuple[str, datetime]:
        """校验口令并签发签名令牌，返回 (token, expires_at)。"""
        if not self.enabled:
            raise AuthenticationFailed("Authentication is disabled")
        account = self._accounts.get(user_id)
        password_hash = (
            account.password_hash
            if account is not None and account.active
            else self._dummy_password_hash
        )
        password_valid = verify_password(password, password_hash)
        valid = bool(account and account.active and password_valid)
        if not valid or account is None:
            raise AuthenticationFailed("Invalid user ID or password")
        now = self._aware_now()
        expires_at = now + self._token_ttl
        payload = {
            "exp": int(expires_at.timestamp()),
            "iat": int(now.timestamp()),
            "jti": secrets.token_urlsafe(16),
            "sub": account.user_id,
        }
        encoded_payload = _b64encode(
            json.dumps(
                payload,
                sort_keys=True,
                separators=(",", ":"),
            ).encode("utf-8")
        )
        signature = _b64encode(
            hmac.new(self._secret, encoded_payload.encode("ascii"), hashlib.sha256).digest()
        )
        return f"{encoded_payload}.{signature}", expires_at

    def authenticate(self, token: str | None) -> UserIdentity:
        """校验令牌并返回身份；鉴权关闭时返回开发用系统身份。"""
        if not self.enabled:
            return UserIdentity(
                user_id="development-system",
                roles=frozenset({Role.ADMIN}),
                employee_id=None,
            )
        if not token:
            raise AuthenticationFailed("Bearer token is required")
        if len(token) > 4096:
            raise AuthenticationFailed("Invalid or expired bearer token")
        try:
            encoded_payload, encoded_signature = token.split(".", 1)
            supplied_signature = _b64decode(encoded_signature)
            expected_signature = hmac.new(
                self._secret,
                encoded_payload.encode("ascii"),
                hashlib.sha256,
            ).digest()
            if not hmac.compare_digest(supplied_signature, expected_signature):
                raise AuthenticationFailed("Invalid or expired bearer token")
            payload = json.loads(_b64decode(encoded_payload))
            _validate_token_payload(payload)
        except (AuthenticationFailed, UnicodeError, ValueError, TypeError, json.JSONDecodeError):
            raise AuthenticationFailed("Invalid or expired bearer token") from None

        now_timestamp = int(self._aware_now().timestamp())
        issued_at = payload["iat"]
        expires_at = payload["exp"]
        if (
            issued_at > now_timestamp + 60
            or expires_at <= now_timestamp
            or expires_at <= issued_at
        ):
            raise AuthenticationFailed("Invalid or expired bearer token")
        if expires_at - issued_at > int(self._token_ttl.total_seconds()) + 1:
            raise AuthenticationFailed("Invalid or expired bearer token")
        account = self._accounts.get(payload["sub"])
        if account is None or not account.active:
            raise AuthenticationFailed("Invalid or expired bearer token")
        return UserIdentity(
            user_id=account.user_id,
            roles=account.roles,
            employee_id=account.employee_id,
        )

    def _aware_now(self) -> datetime:
        now = self._clock()
        if now.tzinfo is None or now.utcoffset() is None:
            raise RuntimeError("Auth clock must return a timezone-aware datetime")
        return now


def hash_password(password: str, *, salt: bytes | None = None) -> str:
    """使用 scrypt 生成可存储的口令哈希字符串。"""
    if len(password) < 12:
        raise ValueError("Password must contain at least 12 characters")
    salt = salt or secrets.token_bytes(16)
    work_factor, block_size, parallelism = 2**14, 8, 1
    digest = hashlib.scrypt(
        password.encode("utf-8"),
        salt=salt,
        n=work_factor,
        r=block_size,
        p=parallelism,
        dklen=32,
        maxmem=64 * 1024 * 1024,
    )
    return "$".join(
        (
            "scrypt",
            str(work_factor),
            str(block_size),
            str(parallelism),
            _b64encode(salt),
            _b64encode(digest),
        )
    )


def verify_password(password: str, encoded_hash: str) -> bool:
    """常量时间校验口令与已编码 scrypt 哈希是否匹配。"""
    try:
        algorithm, n_value, r_value, p_value, salt_value, digest_value = (
            encoded_hash.split("$")
        )
        if algorithm != "scrypt":
            return False
        work_factor = int(n_value)
        block_size = int(r_value)
        parallelism = int(p_value)
        if (work_factor, block_size, parallelism) != (2**14, 8, 1):
            return False
        salt = _b64decode(salt_value)
        expected = _b64decode(digest_value)
        actual = hashlib.scrypt(
            password.encode("utf-8"),
            salt=salt,
            n=work_factor,
            r=block_size,
            p=parallelism,
            dklen=len(expected),
            maxmem=64 * 1024 * 1024,
        )
        return hmac.compare_digest(actual, expected)
    except (UnicodeError, ValueError, TypeError):
        return False


def load_accounts(path_value: str | Path) -> list[UserAccount]:
    """从权限受限的 JSON 用户文件加载账户列表。"""
    path = Path(path_value).expanduser()
    if path.is_symlink():
        raise RuntimeError("AUTH_USERS_FILE must not be a symbolic link")
    try:
        file_mode = stat.S_IMODE(path.stat().st_mode)
    except FileNotFoundError as exc:
        raise RuntimeError(f"AUTH_USERS_FILE does not exist: {path}") from exc
    if os.name != "nt" and file_mode & 0o077:
        raise RuntimeError("AUTH_USERS_FILE must only be readable by its owner (chmod 600)")
    try:
        document = json.loads(path.read_text(encoding="utf-8"))
        records = document["users"]
        if not isinstance(records, list):
            raise TypeError
        accounts = [_parse_account(record) for record in records]
    except (KeyError, TypeError, ValueError, json.JSONDecodeError) as exc:
        raise RuntimeError("AUTH_USERS_FILE has an invalid schema") from exc
    if not accounts:
        raise RuntimeError("AUTH_USERS_FILE must define at least one user")
    return accounts


def _parse_account(record: Any) -> UserAccount:
    if not isinstance(record, dict):
        raise TypeError
    allowed = {"active", "employee_id", "password_hash", "roles", "user_id"}
    if set(record) - allowed:
        raise TypeError
    if not isinstance(record["roles"], list):
        raise TypeError
    roles = frozenset(Role(item) for item in record["roles"])
    if not roles:
        raise TypeError
    user_id = record["user_id"]
    password_hash = record["password_hash"]
    employee_id = record.get("employee_id")
    if not isinstance(user_id, str) or not user_id or len(user_id) > 64:
        raise TypeError
    if not isinstance(password_hash, str) or not password_hash:
        raise TypeError
    if employee_id is not None and not isinstance(employee_id, str):
        raise TypeError
    active = record.get("active", True)
    if not isinstance(active, bool):
        raise TypeError
    return UserAccount(
        user_id=user_id,
        password_hash=password_hash,
        roles=roles,
        employee_id=employee_id,
        active=active,
    )


def _validate_token_payload(payload: Any) -> None:
    if not isinstance(payload, dict) or set(payload) != {"exp", "iat", "jti", "sub"}:
        raise AuthenticationFailed("Invalid token payload")
    if not isinstance(payload["exp"], int) or not isinstance(payload["iat"], int):
        raise AuthenticationFailed("Invalid token timestamps")
    if not isinstance(payload["jti"], str) or not payload["jti"]:
        raise AuthenticationFailed("Invalid token ID")
    if not isinstance(payload["sub"], str) or not payload["sub"]:
        raise AuthenticationFailed("Invalid token subject")


def _b64encode(value: bytes) -> str:
    return base64.urlsafe_b64encode(value).rstrip(b"=").decode("ascii")


def _b64decode(value: str) -> bytes:
    padding = "=" * (-len(value) % 4)
    decoded = base64.b64decode(value + padding, altchars=b"-_", validate=True)
    if _b64encode(decoded) != value:
        raise ValueError("Noncanonical Base64URL encoding")
    return decoded
