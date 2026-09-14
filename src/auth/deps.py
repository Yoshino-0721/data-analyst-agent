"""FastAPI 依赖：数据库会话、当前用户与管理员门槛。

所有需要登录态的接口一律通过 ``get_current_user`` 注入用户；涉及全站数据
或管理操作的接口再加 ``require_admin``。token 无效 / 用户不存在 → 401，
账号被禁用 → 403，普通用户访问管理接口 → 403。
"""

from __future__ import annotations

from fastapi import Depends, HTTPException, Request, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from jwt import InvalidTokenError
from sqlalchemy.orm import Session as OrmSession

from src.auth.db import get_session_factory
from src.auth.models import User
from src.auth.security import decode_token

# auto_error=False：请求没带 Authorization 时返回 None，由我们统一给 JSON 401
_bearer_scheme = HTTPBearer(auto_error=False)

# 「必须先改密」期间仍然放行的两个路径：查自己、改密本身。其余一律 403。
# 用路径白名单而不是"给某些接口少挂依赖"，是为了**默认拒绝** —— 以后新增
# 接口忘了考虑这条，它自动就是被拦住的，不会变成漏网之鱼。
_PASSWORD_CHANGE_EXEMPT_PATHS = frozenset({"/api/auth/me", "/api/auth/change-password"})


def get_db() -> OrmSession:
    factory = get_session_factory()
    db = factory()
    try:
        yield db
    finally:
        db.close()


def get_current_user(
    request: Request,
    credentials: HTTPAuthorizationCredentials | None = Depends(_bearer_scheme),
    db: OrmSession = Depends(get_db),
) -> User:
    if credentials is None or not credentials.credentials:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="未登录，请先登录",
        )

    try:
        payload = decode_token(credentials.credentials)
        user_id = int(payload["sub"])
    except (InvalidTokenError, KeyError, ValueError):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="登录状态已失效，请重新登录",
        )

    user = db.get(User, user_id)
    if user is None:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="账号不存在，请重新登录",
        )
    if not user.is_active:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="账号已被禁用，请联系管理员",
        )
    # 系统生成的初始口令只够"证明你是本人"，不够继续用 —— 改密前不放行任何操作。
    if (
        user.must_change_password
        and request.url.path not in _PASSWORD_CHANGE_EXEMPT_PATHS
    ):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="该账号使用的是系统生成的初始口令，请先修改密码："
            "POST /api/auth/change-password",
        )
    return user


def require_admin(user: User = Depends(get_current_user)) -> User:
    if user.role != "admin":
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="需要管理员权限",
        )
    return user
