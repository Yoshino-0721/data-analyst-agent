"""/api/auth/* 路由：注册、登录、当前用户与修改密码。

**注册不再等于开号**：自助注册提交后落成 ``pending``，管理员审核通过才能登录
（见 ``models.STATUS_*`` 与 ``_login_blocked_reason``）。登录账号支持用户名或邮箱。
"""

from __future__ import annotations

import re

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, Field
from sqlalchemy import or_, select
from sqlalchemy.orm import Session as OrmSession

from src.auth import throttle
from src.auth.deps import get_current_user, get_db
from src.auth.models import (
    STATUS_ACTIVE,
    STATUS_PENDING,
    STATUS_REJECTED,
    User,
)
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
        # 审核状态：前端据它区分"待审核 / 已通过 / 未通过"，也用于管理后台的待审核列表
        "status": user.status,
        # 前端据此把用户按在"改密"这一步上（服务端另有硬闸门，见 deps）
        "must_change_password": user.must_change_password,
        "created_at": user.created_at.isoformat() if user.created_at else None,
    }


def _login_blocked_reason(user: User) -> str | None:
    """账号能不能登录；不能则给出**对用户可行动**的说法。

    三种"被挡住"的情形刻意分开：待审核（等一等就行）、审核未通过（得找管理员）、
    被停用（原本能用、现在被停了）。合成一句话会让用户完全不知道下一步做什么 ——
    而"不知道该做什么"正是这类闸门最容易被当成"网站坏了"的地方。
    """
    if user.status == STATUS_PENDING:
        return "注册申请正在等待管理员审核，审核通过后即可登录"
    if user.status == STATUS_REJECTED:
        return "注册申请未通过审核，请联系管理员"
    if not user.is_active:
        return "账号已被禁用，请联系管理员"
    return None


def validate_username(username: str) -> None:
    """用户名规则。

    **公开函数**：管理端建号（``admin_api``）与自助注册必须用同一套判定 ——
    各写一份的话，迟早出现"注册拦得住、管理员建号拦不住"这种规则漂移。
    """
    if not _USERNAME_RE.fullmatch(username):
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="用户名需为 2-32 位中文、字母、数字或下划线",
        )


def validate_email(email: str) -> None:
    """邮箱格式规则，理由同 :func:`validate_username`。"""
    if not _EMAIL_RE.fullmatch(email):
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="邮箱格式不正确",
        )


def _register_validations(payload: RegisterRequest) -> None:
    validate_username(payload.username)
    validate_email(payload.email)


@router.post("/register")
def register(payload: RegisterRequest, db: OrmSession = Depends(get_db)) -> dict:
    """提交注册申请。**不直接开号、也不发 token** —— 账号落成 ``pending``。

    由管理员在后台审核通过（``PATCH /api/admin/users/{id}`` 置 status=active）后，
    本人才能用注册时设的口令登录。
    """
    if not settings.allow_registration:
        # 显式关掉自助注册的部署：连申请都收不到，只能请管理员建号。
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
        # 自助注册的账号**先不启用**：批准之前不能登录，也就不会消耗站点共用的
        # API Key 额度。"注册开放"与"立刻能用"是两件事，这里把它们分开。
        is_active=False,
        status=STATUS_PENDING,
    )
    db.add(user)
    db.commit()
    db.refresh(user)

    # 刻意**不发 token**：账号还没被批准，发了也会在登录闸门被拒 ——
    # 前端拿到 token 反而会以为注册即登录成功了。返回 pending 标志让前端说清楚。
    return {"pending": True, "user": user_payload(user)}


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
    blocked = _login_blocked_reason(user)
    if blocked:
        # 口令是对的、只是账号还不能用（待审核 / 未通过 / 被停用）：**不计失败**。
        # 否则管理员一停用某人，对方只要拿旧口令猛试就能把他锁上（把封禁变成被封锁）。
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail=blocked,
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
    # 自增版本号 → **此前签发的 token 立刻全部失效**。这是"改密"的应有语义：
    # 口令之所以要改，前提就是旧凭据可能已经泄露，那旧 token 当然不能再算数。
    user.token_version = int(user.token_version or 0) + 1
    db.commit()

    # 顺手把**新签发**的 token 一起返回：旧 token 已经死了，如果响应里什么都不给，
    # 客户端下一个请求就是 401，用户会以为"改密把账号弄坏了"。
    # 新 token 只发给这次已认证的请求，不削弱任何东西。
    return {"ok": True, "token": create_token(user)}
