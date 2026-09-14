"""/api/auth/* 路由：注册、登录、当前用户与修改密码。

注册即登录（直接返回 token），前端拿到后按角色跳转：admin → 管理后台，
user → 工作台。登录账号支持用户名或邮箱。
"""

from __future__ import annotations

import re

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, Field
from sqlalchemy import or_, select
from sqlalchemy.orm import Session as OrmSession

from src.auth import throttle
from src.auth.deps import get_current_user, get_db
from src.auth.models import User
from src.auth.security import (
    create_token,
    hash_password,
    password_strength_problem,
    verify_password,
)
from src.config import settings

router = APIRouter(prefix="/api/auth", tags=["auth"])

# 用户名：2-32 位中文 / 字母 / 数字 / 下划线；邮箱做轻量格式校验（不引 email-validator）
_USERNAME_RE = re.compile(r"^[\u4e00-\u9fa5A-Za-z0-9_]{2,32}$")
_EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")


class RegisterRequest(BaseModel):
    username: str = Field(min_length=1, max_length=32)
    email: str = Field(min_length=3, max_length=255)
    # bcrypt 只看前 72 字节，这里收紧到 64 免得出现"尾部被截断也能登录"的迷惑行为
    password: str = Field(min_length=6, max_length=64)


class LoginRequest(BaseModel):
    account: str = Field(min_length=1, max_length=255)
    password: str = Field(min_length=1, max_length=64)


class ChangePasswordRequest(BaseModel):
    old_password: str = Field(min_length=1, max_length=64)
    new_password: str = Field(min_length=6, max_length=64)


def user_payload(user: User) -> dict:
    """返回给前端的用户信息（不含口令哈希）。"""
    return {
        "id": user.id,
        "username": user.username,
        "email": user.email,
        "role": user.role,
        "is_active": user.is_active,
        # 前端据此把用户按在"改密"这一步上（服务端另有硬闸门，见 deps）
        "must_change_password": user.must_change_password,
        "created_at": user.created_at.isoformat() if user.created_at else None,
    }


def _register_validations(payload: RegisterRequest) -> None:
    if not _USERNAME_RE.fullmatch(payload.username):
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="用户名需为 2-32 位中文、字母、数字或下划线",
        )
    if not _EMAIL_RE.fullmatch(payload.email):
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="邮箱格式不正确",
        )


@router.post("/register")
def register(payload: RegisterRequest, db: OrmSession = Depends(get_db)) -> dict:
    if not settings.allow_registration:
        # 默认关闭：公网部署下，任何人注册成功都会消耗**站点共用**的 API Key 额度
        # （上传文档要 embedding、提问要 chat）。开通成员请管理员在服务端设置
        # ALLOW_REGISTRATION=true 后重启。
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="本站已关闭自助注册，请联系管理员开通账号",
        )

    _register_validations(payload)

    existing = db.scalar(
        select(User).where(
            or_(User.username == payload.username, User.email == payload.email.lower())
        )
    )
    if existing is not None:
        taken = "用户名" if existing.username == payload.username else "邮箱"
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=f"{taken}已被注册")

    user = User(
        username=payload.username,
        email=payload.email.lower(),
        password_hash=hash_password(payload.password),
        role="user",
    )
    db.add(user)
    db.commit()
    db.refresh(user)

    return {"token": create_token(user), "user": user_payload(user)}


@router.post("/login")
def login(payload: LoginRequest, db: OrmSession = Depends(get_db)) -> dict:
    """登录。失败到阈值会短暂锁定该账号，期间一律 429。"""
    waiting = throttle.locked_seconds_remaining(payload.account)
    if waiting:
        raise HTTPException(
            status_code=status.HTTP_429_TOO_MANY_REQUESTS,
            detail=f"登录失败次数过多，请 {waiting} 秒后再试",
            headers={"Retry-After": str(waiting)},
        )

    user = db.scalar(
        select(User).where(
            or_(User.username == payload.account, User.email == payload.account.lower())
        )
    )
    if user is None or not verify_password(payload.password, user.password_hash):
        locked_for = throttle.record_failure(payload.account)
        if locked_for:
            # 触发锁定的**这一次**就直接 429：语义清楚，也让"到底锁没锁上"可观测。
            # 这条分支只在"凭据错误"时才会到达，所以它不泄露任何口令信息。
            raise HTTPException(
                status_code=status.HTTP_429_TOO_MANY_REQUESTS,
                detail=f"登录失败次数过多，请 {locked_for} 秒后再试",
                headers={"Retry-After": str(locked_for)},
            )
        # 刻意不区分"用户名不存在"与"口令错误"，也不在响应里透露剩余次数 ——
        # 前者会变成账号枚举，后者会告诉攻击者"再试几次就能确认猜对了"。
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="用户名或密码不正确",
        )
    if not user.is_active:
        # 口令是对的、只是账号被禁用：**不计失败**。否则管理员一禁用某人，
        # 对方只要拿旧口令猛试就能把他锁上（把封禁变成被封锁）。
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="账号已被禁用，请联系管理员",
        )

    throttle.record_success(payload.account)
    return {"token": create_token(user), "user": user_payload(user)}


@router.get("/me")
def me(user: User = Depends(get_current_user)) -> dict:
    return user_payload(user)


@router.post("/change-password")
def change_password(
    payload: ChangePasswordRequest,
    user: User = Depends(get_current_user),
    db: OrmSession = Depends(get_db),
) -> dict:
    if not verify_password(payload.old_password, user.password_hash):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="原密码不正确",
        )

    # 被强制改密的账号（用的是系统生成的初始口令）必须换成强口令 ——
    # 否则"随机强口令 + 强制修改"这条链会被一次改成 `123456` 直接绕过。
    if user.must_change_password:
        problem = password_strength_problem(payload.new_password)
        if problem is not None:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail=f"新密码不满足强度要求（{problem}）",
            )

    user.password_hash = hash_password(payload.new_password)
    user.must_change_password = False
    db.commit()
    return {"ok": True}
