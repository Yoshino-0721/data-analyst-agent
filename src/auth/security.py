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
import re
import secrets
import string
from datetime import datetime, timedelta, timezone
from typing import Protocol

import bcrypt
import jwt

logger = logging.getLogger(__name__)

ALGORITHM = "HS256"
DEFAULT_EXPIRE_HOURS = 168  # 7 天
SECRET_FILE_NAME = "secret.key"

# 管理员口令的最低要求：长度 + 字符种类。普通用户注册只要求 ≥6 位，
# 但管理员账号一旦被撞开就是全站数据（含所有人的文档与 API Key 设置），
# 所以初始口令走更严的一档。
MIN_ADMIN_PASSWORD_LENGTH = 12
MIN_ADMIN_PASSWORD_CLASSES = 3

# 明确拒绝的弱口令。放 deny-list 而不是只靠长度：`admin12345678` 有 13 位、
# 也过了字符种类检查，但它照样是第一个被尝试的候选。
WEAK_PASSWORDS = frozenset(
    {
        "admin", "admin123", "admin1234", "admin123456", "administrator",
        "password", "passw0rd", "password1", "p@ssw0rd",
        "123456", "1234567", "12345678", "123456789", "1234567890",
        "qwerty", "qwerty123", "changeme", "letmein", "welcome",
        "root", "test", "test123", "secret", "iloveyou", "abc123456",
    }
)

_PASSWORD_ALPHABET = string.ascii_letters + string.digits + "!@#$%^&*-_=+"
_CLASS_PATTERNS = (r"[a-z]", r"[A-Z]", r"\d", r"[^A-Za-z0-9]")


class TokenUser(Protocol):
    """签发 token 只需要这几个字段（User 模型天然满足）。"""

    id: int
    username: str
    role: str


def is_production() -> bool:
    """``APP_ENV`` 不是 dev / test 即视为生产环境。

    默认 dev，保证 clone 下来就能跑；生产部署必须显式设 ``APP_ENV=production``
    （Docker 镜像里已经设好），此时密钥与口令的强制要求才生效。
    """
    try:
        from src.config import settings

        env = str(getattr(settings, "env", "") or "").strip().lower()
    except Exception:  # pragma: no cover - 配置模块导入失败时退回环境变量
        env = ""
    env = env or (os.getenv("APP_ENV", "") or "").strip().lower()
    return env not in {"", "dev", "test", "local"}


def password_strength_problem(password: str) -> str | None:
    """返回不满足要求的原因；口令合格时返回 ``None``。

    只描述「哪里不行」，不回显口令本身 —— 报错信息会进日志。
    """
    value = password or ""
    if len(value) < MIN_ADMIN_PASSWORD_LENGTH:
        return f"长度不足 {MIN_ADMIN_PASSWORD_LENGTH} 位（当前 {len(value)} 位）"
    if value.lower() in WEAK_PASSWORDS:
        return "属于常见弱口令 deny-list"
    classes = sum(1 for pattern in _CLASS_PATTERNS if re.search(pattern, value))
    if classes < MIN_ADMIN_PASSWORD_CLASSES:
        return (
            f"字符种类不足 {MIN_ADMIN_PASSWORD_CLASSES} 类"
            "（需要同时含大小写字母、数字、符号中的至少三类）"
        )
    return None


def generate_strong_password(length: int = 24) -> str:
    """生成一个必然通过 ``password_strength_problem`` 的随机口令。"""
    while True:
        candidate = "".join(secrets.choice(_PASSWORD_ALPHABET) for _ in range(length))
        if password_strength_problem(candidate) is None:
            return candidate


def _secret() -> str:
    """取签名密钥：环境变量 > 已持久化的随机密钥 > 现场生成并落盘。

    **生产环境只认环境变量**：不生成、不落盘、直接拒绝启动。理由是随机生成的
    密钥虽然会持久化，但容器重建 / 换机器时它跟着 `storage/` 一起丢或一起换，
    所有人被静默强制重登；多实例部署更是各签各的，请求落到另一台就 401。
    这类问题必须在启动时就暴露，而不是等用户来报"老是掉登录"。
    """
    from_env = os.getenv("JWT_SECRET")
    if from_env and from_env.strip():
        return from_env.strip()

    if is_production():
        raise RuntimeError(
            "生产环境（APP_ENV=%s）必须显式注入 JWT_SECRET。生成一个随机长密钥：\n"
            "  python -c \"import secrets; print(secrets.token_urlsafe(48))\"\n"
            "把输出写进 .env：JWT_SECRET=<上面的输出>\n"
            "注意：多实例部署时所有实例必须使用同一个 JWT_SECRET，"
            "否则请求落到另一台会被判为未登录。"
            % (os.getenv("APP_ENV", "") or "(未设置)")
        )

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


def verify_security_config() -> None:
    """启动自检，**fail fast**：生产环境没注入 JWT_SECRET 就启动不了。

    宁可启动即失败并打印怎么修，也不要等第一个用户登录才发现密钥没配 ——
    那时进程里已经随机生成过一份，下次重启会让所有人掉线。
    """
    _secret()


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
