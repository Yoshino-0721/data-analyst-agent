"""管理员后台 API：用户管理、全会话查看、全数据集管理与系统操作。

整条路由挂 ``require_admin`` 依赖 —— 普通用户访问任何子路径都会 403。
管理员对自己也有保护：不能降级 / 禁用自己的账号，避免把全站锁死。

与项目一 ``rag-knowledge-base/src/admin_api.py`` 同构，把 documents 换成 datasets；
两处有意的差异：

1. 项目一要能检索文档内容，所以有 ``/documents/{id}/preview``；本项目的数据集是
   CSV/Excel，预览等于把整份数据吐给管理员，没有对应场景，**不提供**该端点。
2. 项目一有 ``/system/reindex``；本项目没有索引，缓存只包含执行器与 LLM 客户端，
   所以系统操作只剩 ``reset-caches`` 与 ``health``。
"""

from __future__ import annotations

import secrets
import string
from datetime import datetime, timedelta

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy import func, or_, select
from sqlalchemy.orm import Session as OrmSession

from .auth.api import validate_email, validate_username
from .auth.deps import get_db, require_admin
from .auth.models import ChatSession, Dataset, Message, User
from .auth.security import generate_strong_password, hash_password
from .config import settings
from .datasets import dataset_payload, delete_dataset
from .serializers import message_payload, session_payload
from .workspaces import drop_workspace, get_workspace

router = APIRouter(prefix="/api/admin", tags=["admin"], dependencies=[Depends(require_admin)])


# ---------------------------------------------------------------- 数据模型


class UserCreate(BaseModel):
    """管理员建号的入参。**口令不在这里** —— 由服务端生成、只在响应里返回一次。"""

    username: str = Field(min_length=1, max_length=32)
    email: str = Field(min_length=3, max_length=255)
    role: str = Field(default="user", pattern="^(user|admin)$")


class UserUpdate(BaseModel):
    role: str | None = Field(default=None, pattern="^(user|admin)$")
    is_active: bool | None = None


class PasswordReset(BaseModel):
    password: str | None = Field(default=None, min_length=6, max_length=64)


# ---------------------------------------------------------------- 用户管理


def _user_row(user: User) -> dict:
    return {
        "id": user.id,
        "username": user.username,
        "email": user.email,
        "role": user.role,
        "is_active": user.is_active,
        "created_at": user.created_at.isoformat() if user.created_at else None,
    }


@router.post("/users")
def create_user(payload: UserCreate, db: OrmSession = Depends(get_db)) -> dict:
    """管理员建号：口令由服务端生成、**只在这次响应里返回一次**，并强制首登改密。

    与自助注册的分工：自助注册默认关闭（``ALLOW_REGISTRATION``），所以"谁能有账号"
    这件事从"任何人自助"变成了"管理员开"。两条安全承诺因此落在这条端点上：

    1. 口令**不落库明文**（库里只有 bcrypt 哈希），所以任何读接口都拿不回它 ——
       真丢了就用「重置密码」重新生成一个；
    2. 新账号带 ``must_change_password=True``：在本人把口令换掉之前，除
       ``/api/auth/me`` 与改密接口外一律 403（一次性口令不该被长期使用）。

    用户名/邮箱规则复用 ``auth.api`` 的公开校验函数，避免与注册两套规则漂移。
    """
    validate_username(payload.username)
    validate_email(payload.email)

    existing = db.scalar(
        select(User).where(
            or_(User.username == payload.username, User.email == payload.email.lower())
        )
    )
    if existing is not None:
        taken = "用户名" if existing.username == payload.username else "邮箱"
        raise HTTPException(status_code=400, detail=f"{taken}已被占用")

    password = generate_strong_password()
    user = User(
        username=payload.username,
        email=payload.email.lower(),
        password_hash=hash_password(password),
        role=payload.role,
        must_change_password=True,
    )
    db.add(user)
    db.commit()
    db.refresh(user)

    # ⚠️ 明文口令只出现在这里，且只出现这一次：不写日志、不入库、之后不再返回。
    return {"user": _user_row(user), "password": password}


@router.get("/users")
def list_users(q: str = "", db: OrmSession = Depends(get_db)) -> dict:
    query = select(User).order_by(User.id)
    if q.strip():
        like = f"%{q.strip()}%"
        query = query.where(or_(User.username.like(like), User.email.like(like)))
    users = db.scalars(query).all()
    return {"users": [_user_row(u) for u in users]}


@router.patch("/users/{user_id}")
def update_user(
    user_id: int,
    payload: UserUpdate,
    admin: User = Depends(require_admin),
    db: OrmSession = Depends(get_db),
) -> dict:
    target = db.get(User, user_id)
    if target is None:
        raise HTTPException(status_code=404, detail="用户不存在")

    self_lockout = user_id == admin.id and (
        (payload.role is not None and payload.role != "admin")
        or (payload.is_active is False)
    )
    if self_lockout:
        raise HTTPException(status_code=400, detail="不能降级或禁用自己的管理员账号")

    if payload.role is not None:
        target.role = payload.role
    if payload.is_active is not None:
        target.is_active = payload.is_active
    db.commit()
    return _user_row(target)


@router.post("/users/{user_id}/reset-password")
def reset_password(
    user_id: int,
    payload: PasswordReset | None = None,
    db: OrmSession = Depends(get_db),
) -> dict:
    """重置密码。不传 password 时生成 10 位随机密码，只在响应里出现一次。"""
    target = db.get(User, user_id)
    if target is None:
        raise HTTPException(status_code=404, detail="用户不存在")

    if payload and payload.password:
        new_password = payload.password
    else:
        alphabet = string.ascii_letters + string.digits
        new_password = "".join(secrets.choice(alphabet) for _ in range(10))

    target.password_hash = hash_password(new_password)
    # 重置出来的是**一次性口令**：本人首登必须改密，与"管理员建号"完全一致。
    # 安全边界在闸门上、不在初始口令的强度上 —— 所以这里不校验强度（那会破坏
    # "管理员下发口头临时码"这个合理场景），而是把闸门关死：改密前除
    # /api/auth/me 与改密接口外一律 403。
    target.must_change_password = True
    # 重置口令同样要踢掉该用户已签发的 token —— 这条路径的存在理由就是
    # "凭据可能已经泄露"，那么旧 token 自然也不能再算数。
    target.token_version = int(target.token_version or 0) + 1
    db.commit()
    return {"ok": True, "username": target.username, "password": new_password}


# ---------------------------------------------------------------- 会话管理


@router.get("/sessions")
def list_all_sessions(
    user_id: int | None = None, db: OrmSession = Depends(get_db)
) -> dict:
    query = select(ChatSession, User.username).join(User, ChatSession.user_id == User.id)
    if user_id is not None:
        query = query.where(ChatSession.user_id == user_id)
    rows = db.execute(
        query.order_by(ChatSession.updated_at.desc(), ChatSession.id.desc())
    ).all()

    return {
        "sessions": [
            {**session_payload(chat_session), "username": username}
            for chat_session, username in rows
        ]
    }


@router.get("/sessions/{session_id}/messages")
def read_any_session(session_id: int, db: OrmSession = Depends(get_db)) -> dict:
    """读任意用户的会话。assistant 消息的 ``meta`` 里就是执行轨迹，原样带出去。"""
    chat_session = db.get(ChatSession, session_id)
    if chat_session is None:
        raise HTTPException(status_code=404, detail="会话不存在")

    owner = db.get(User, chat_session.user_id)
    messages = (
        db.query(Message).filter_by(session_id=session_id).order_by(Message.id).all()
    )
    return {
        "session": session_payload(chat_session),
        "username": owner.username if owner else "(已删除)",
        "messages": [message_payload(m) for m in messages],
    }


@router.delete("/sessions/{session_id}")
def delete_any_session(session_id: int, db: OrmSession = Depends(get_db)) -> dict:
    chat_session = db.get(ChatSession, session_id)
    if chat_session is None:
        raise HTTPException(status_code=404, detail="会话不存在")
    db.delete(chat_session)
    db.commit()
    return {"ok": True}


# ---------------------------------------------------------------- 数据集管理


@router.get("/datasets")
def list_all_datasets(
    user_id: int | None = None, db: OrmSession = Depends(get_db)
) -> dict:
    query = select(Dataset, User.username).join(User, Dataset.user_id == User.id)
    if user_id is not None:
        query = query.where(Dataset.user_id == user_id)
    rows = db.execute(query.order_by(Dataset.id)).all()
    return {
        "datasets": [dataset_payload(row, username=username) for row, username in rows]
    }


@router.delete("/datasets/{dataset_id}")
def delete_any_dataset(dataset_id: int, db: OrmSession = Depends(get_db)) -> dict:
    """删任意用户的数据集。

    必须比用户接口多做一步 ``drop_workspace``：该用户的内存工作区里还留着
    这个文件的 ``files`` / ``schemas`` 条目与已提取的 Schema，不摘掉的话
    他下一次提问仍会把它挂进沙箱 —— 删了等于没删。
    """
    row = db.get(Dataset, dataset_id)
    if row is None:
        raise HTTPException(status_code=404, detail="数据集不存在")

    workspace = get_workspace(row.user_id)
    result = delete_dataset(db, workspace, row)
    drop_workspace(row.user_id)
    return result


# ---------------------------------------------------------------- 系统操作


@router.post("/system/reset-caches")
def reset_caches_endpoint() -> dict:
    """清掉缓存的执行器与模型客户端（改配置后或排查时用）。

    延迟导入 ``server``：server 在模块级 ``include_router`` 本路由，
    顶层互相导入会成环；请求到达时 server 一定已经加载完了。
    """
    from . import server

    server.reset_caches()
    return {"ok": True}


@router.get("/system/health")
def system_health() -> dict:
    from . import server

    executor_ok, executor_note = True, ""
    try:
        executor_ok, executor_note = server.get_executor().available()
    except Exception as exc:  # noqa: BLE001
        executor_ok, executor_note = False, str(exc)

    return {
        "status": "ok",
        "has_api_key": bool(settings.api_key),
        "model": settings.model,
        "executor": settings.executor,
        "executor_ok": executor_ok,
        "executor_note": executor_note,
        "storage_root": str(settings.storage_root),
    }


@router.get("/stats")
def stats(db: OrmSession = Depends(get_db)) -> dict:
    since = datetime.now() - timedelta(hours=24)
    return {
        "users": db.scalar(select(func.count(User.id))),
        "active_users": db.scalar(select(func.count(User.id)).where(User.is_active)),
        "sessions": db.scalar(select(func.count(ChatSession.id))),
        "messages": db.scalar(select(func.count(Message.id))),
        "datasets": db.scalar(select(func.count(Dataset.id))),
        "sessions_last_24h": db.scalar(
            select(func.count(ChatSession.id)).where(ChatSession.updated_at >= since)
        ),
    }


__all__ = ["router"]
