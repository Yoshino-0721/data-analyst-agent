"""ORM 模型：用户、会话与聊天消息。

表名刻意与需求保持一致：``users`` / ``sessions`` / ``messages``。
``ChatSession`` 只是为避开与 SQLAlchemy Session 同名的冲突，落库表名仍是
``sessions``。时间统一用 naive 本地时间 —— 本机部署，界面按本地时间读即可。
"""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import Boolean, DateTime, ForeignKey, Integer, String, Text
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship


class Base(DeclarativeBase):
    pass


def _now() -> datetime:
    return datetime.now()


class User(Base):
    """注册用户。密码只存 bcrypt 哈希，绝不存明文。"""

    __tablename__ = "users"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    username: Mapped[str] = mapped_column(String(64), unique=True, index=True)
    email: Mapped[str] = mapped_column(String(255), unique=True, index=True)
    password_hash: Mapped[str] = mapped_column(String(255))
    # user：只能操作自己的数据；admin：可管理全站
    role: Mapped[str] = mapped_column(String(16), default="user", server_default="user")
    is_active: Mapped[bool] = mapped_column(Boolean, default=True, server_default="1")
    # 自动生成初始口令的账号（例如首次启动引导出来的管理员）置 True：
    # 在改密之前，除 /api/auth/me 与改密接口外一律 403。见 deps.get_current_user。
    must_change_password: Mapped[bool] = mapped_column(
        Boolean, default=False, server_default="0"
    )
    # token 版本号：改密（或管理员重置口令）时自增，**此前签发的 token 立刻全部失效**。
    # 存量 token 的 payload 里没有这个字段，一律判为失效 —— 见 security.token_version_of。
    token_version: Mapped[int] = mapped_column(Integer, default=0, server_default="0")
    created_at: Mapped[datetime] = mapped_column(DateTime, default=_now)


class ChatSession(Base):
    """一次问答会话。归属一个用户，删除时级联删掉全部消息。"""

    __tablename__ = "sessions"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id"), index=True)
    # 默认取首条提问截断；前端可改名
    title: Mapped[str] = mapped_column(String(200), default="新会话", server_default="新会话")
    created_at: Mapped[datetime] = mapped_column(DateTime, default=_now)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=_now, onupdate=_now)

    messages: Mapped[list["Message"]] = relationship(
        back_populates="session", cascade="all, delete-orphan"
    )


class Message(Base):
    """一条聊天记录。

    ``meta`` 存 JSON 文本，放随消息一起持久化的附加信息（项目一放引用卡片，
    项目二放执行轨迹），管理员查看记录时可以一并还原。
    """

    __tablename__ = "messages"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    session_id: Mapped[int] = mapped_column(ForeignKey("sessions.id"), index=True)
    # user / assistant（与聊天语义一致，与 User.role 的取值无关）
    role: Mapped[str] = mapped_column(String(16))
    content: Mapped[str] = mapped_column(Text)
    meta: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=_now)

    session: Mapped["ChatSession"] = relationship(back_populates="messages")


class Dataset(Base):
    """一个用户上传的数据集的元数据（向量库之外的可查询索引）。

    与项目一的 ``Document`` 同构：向量 / 原始文件都不进关系库，这里只放
    「谁传了哪个文件、多少行多少列」这类可以直接 SQL 查询的索引信息。
    数据集**按用户归属，会话共用** —— 同一用户的不同会话看到的是同一批数据集。
    """

    __tablename__ = "datasets"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id"), index=True)
    filename: Mapped[str] = mapped_column(String(255))
    n_rows: Mapped[int] = mapped_column(Integer, default=0, server_default="0")
    n_cols: Mapped[int] = mapped_column(Integer, default=0, server_default="0")
    created_at: Mapped[datetime] = mapped_column(DateTime, default=_now)
