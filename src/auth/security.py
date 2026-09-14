"""口令哈希与 JWT 签发/校验。

- 口令用 **bcrypt 加盐哈希**（直接用 ``bcrypt`` 库，不经 passlib —— 后者在
  bcrypt 4.x 上有兼容坑），数据库里只有哈希，绝不存明文。
- 登录态用 **JWT（HS256）**，前端存 token、每次请求带 ``Authorization:
  Bearer <token>``。过期时间默认 7 天，可用 ``JWT_EXPIRE_HOURS`` 调整。
- 签名密钥优先取 ``JWT_SECRET``；没有则随机生成一份持久化到
  ``storage/secret.key``，保证重启后已签发的 token 仍然有效。
"""

from __future__ import annotations

import logging
import os
import secrets
from datetime import datetime, timedelta, timezone
from typing import Protocol

import bcrypt
import jwt

logger = logging.getLogger(__name__)

ALGORITHM = "HS256"
DEFAULT_EXPIRE_HOURS = 168  # 7 天
SECRET_FILE_NAME = "secret.key"


class TokenUser(Protocol):
    """签发 token 只需要这几个字段（User 模型天然满足）。"""

    id: int
    username: str
    role: str


def _secret() -> str:
    """取签名密钥：环境变量 > 已持久化的随机密钥 > 现场生成并落盘。"""
    from_env = os.getenv("JWT_SECRET")
    if from_env and from_env.strip():
        return from_env.strip()

    from src.config import settings

    key_file = settings.storage_root / SECRET_FILE_NAME
    if key_file.is_file():
        stored = key_file.read_text(encoding="utf-8").strip()
        if stored:
            return stored

    key = secrets.token_hex(32)
    try:
        settings.storage_root.mkdir(parents=True, exist_ok=True)
        key_file.write_text(key, encoding="utf-8")
    except OSError as exc:  # pragma: no cover - 落盘失败时进程内仍可用
        logger.warning("JWT 密钥无法持久化到 %s：%s（重启后登录态会失效）", key_file, exc)
    return key


def hash_password(password: str) -> str:
    """bcrypt 加盐哈希。每次调用盐都不同，同一口令两次哈希结果不一样。"""
    return bcrypt.hashpw(password.encode("utf-8"), bcrypt.gensalt()).decode("ascii")


def verify_password(password: str, hashed: str) -> bool:
    """校验口令；哈希格式不合法时按不匹配处理而不是抛异常。"""
    try:
        return bcrypt.checkpw(password.encode("utf-8"), hashed.encode("ascii"))
    except (ValueError, TypeError):
        return False


def create_token(user: TokenUser, *, expires_hours: int | None = None) -> str:
    """签发登录 token。``expires_hours`` 供测试构造过期 token 使用。"""
    if expires_hours is None:
        try:
            expires_hours = int(os.getenv("JWT_EXPIRE_HOURS", str(DEFAULT_EXPIRE_HOURS)))
        except ValueError:
            expires_hours = DEFAULT_EXPIRE_HOURS

    now = datetime.now(timezone.utc)
    payload = {
        "sub": str(user.id),
        "username": user.username,
        "role": user.role,
        "iat": now,
        "exp": now + timedelta(hours=expires_hours),
    }
    return jwt.encode(payload, _secret(), algorithm=ALGORITHM)


def decode_token(token: str) -> dict:
    """校验并解码 token；过期 / 签名错误抛 ``jwt.InvalidTokenError`` 子类。"""
    return jwt.decode(token, _secret(), algorithms=[ALGORITHM])
