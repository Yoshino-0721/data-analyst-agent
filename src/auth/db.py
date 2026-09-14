"""数据库引擎与会话工厂。

SQLite 零运维，路径默认 ``storage/app.db``（测试用 ``AUTH_DB_PATH`` 或直接传
参隔离）。``init_db()`` 幂等建表，并在 users 表为空时引导管理员账号：

- 给了 ``ADMIN_PASSWORD`` 就用它，但**弱口令直接拒绝启动**（含 ``admin123``）；
- 没给就**现场生成随机强口令**，在日志里用醒目横幅打印一次，并置
  ``must_change_password``：该账号在改密前除 ``/api/auth/me`` 与改密接口外一律 403。

**这里刻意不再有"默认口令"这种东西** —— 一个全站可见的固定口令，等于把
"部署到公网后忘记改"变成一个必然事件。
"""

from __future__ import annotations

import logging
import os
import threading
from pathlib import Path

from sqlalchemy import create_engine, event, select
from sqlalchemy.orm import Session, sessionmaker

from src.auth.models import Base, User
from src.auth.security import (
    generate_strong_password,
    hash_password,
    password_strength_problem,
)

logger = logging.getLogger(__name__)

DEFAULT_ADMIN_USERNAME = "admin"
DEFAULT_ADMIN_EMAIL = "admin@example.com"

_lock = threading.Lock()
_engine = None
_session_factory: sessionmaker | None = None
_db_path: Path | None = None


def default_db_path() -> Path:
    """数据库文件的默认位置；``AUTH_DB_PATH`` 供测试或特殊部署覆盖。"""
    env = os.getenv("AUTH_DB_PATH")
    if env and env.strip():
        return Path(env.strip())
    from src.config import settings

    return settings.storage_root / "app.db"


def _create_engine(path: Path):
    # FastAPI 的同步接口跑在线程池里，必须放开 SQLite 的同线程检查；
    # WAL + busy_timeout 缓解多线程并发读写时偶发的 database is locked。
    engine = create_engine(
        f"sqlite:///{path.as_posix()}",
        connect_args={"check_same_thread": False},
    )

    @event.listens_for(engine, "connect")
    def _set_pragma(dbapi_connection, _connection_record):
        cursor = dbapi_connection.cursor()
        cursor.execute("PRAGMA journal_mode=WAL")
        cursor.execute("PRAGMA busy_timeout=5000")
        cursor.close()

    return engine


def _ensure_columns(engine) -> None:
    """轻量前向迁移：给既有库补上后加的列。

    SQLite 的 ``ALTER TABLE ADD COLUMN`` 没有 ``IF NOT EXISTS``，所以先查
    ``PRAGMA table_info`` 再决定加不加。没有这一步，老 ``storage/app.db``
    升级到本版本后会因为缺列而在查询时报错。
    """
    with engine.connect() as conn:
        columns = {row[1] for row in conn.exec_driver_sql("PRAGMA table_info(users)")}
        if "must_change_password" not in columns:
            conn.exec_driver_sql(
                "ALTER TABLE users ADD COLUMN must_change_password "
                "BOOLEAN NOT NULL DEFAULT 0"
            )
            conn.commit()
            logger.info("已为既有 users 表补上 must_change_password 列")


def init_db(path: Path | None = None) -> None:
    """建库建表并引导管理员。重复调用会切换到新路径重建引擎（测试用）。"""
    global _engine, _session_factory, _db_path

    path = Path(path) if path is not None else default_db_path()
    path.parent.mkdir(parents=True, exist_ok=True)

    with _lock:
        _engine = _create_engine(path)
        _db_path = path
        Base.metadata.create_all(_engine)
        _ensure_columns(_engine)
        _session_factory = sessionmaker(bind=_engine, expire_on_commit=False)

    with _session_factory() as session:
        _bootstrap_admin(session)


def _bootstrap_admin(session: Session) -> None:
    """users 表为空时创建管理员账号，让全新部署开箱即可登录。

    口令来源只有两条路，**没有"默认口令"这一条**：

    1. 设了 ``ADMIN_PASSWORD`` —— 用它，但弱口令（含 ``admin123``）直接抛
       ``RuntimeError`` 终止启动；
    2. 没设 —— 现场生成随机强口令，日志横幅打印一次，并强制首次登录改密。
    """
    if session.scalar(select(User).limit(1)) is not None:
        return

    username = os.getenv("ADMIN_USERNAME", DEFAULT_ADMIN_USERNAME).strip() or DEFAULT_ADMIN_USERNAME
    email = os.getenv("ADMIN_EMAIL", DEFAULT_ADMIN_EMAIL).strip() or DEFAULT_ADMIN_EMAIL
    password = os.getenv("ADMIN_PASSWORD", "").strip()

    generated = not password
    if generated:
        password = generate_strong_password()
    else:
        problem = password_strength_problem(password)
        if problem is not None:
            raise RuntimeError(
                "ADMIN_PASSWORD 不满足强度要求（%s），拒绝启动。\n"
                "请换一个随机强口令：\n"
                "  python -c \"import secrets; print(secrets.token_urlsafe(24))\"\n"
                "或者**干脆不要设** ADMIN_PASSWORD —— 系统会自动生成随机强口令，\n"
                "并在启动日志里用醒目的横幅打印一次，该账号首次登录必须改密。"
                % problem
            )

    session.add(
        User(
            username=username,
            email=email,
            password_hash=hash_password(password),
            role="admin",
            must_change_password=generated,
        )
    )
    session.commit()

    if generated:
        logger.warning(
            "\n"
            "============== 管理员初始口令（只显示这一次，请立即保存） ==============\n"
            "  用户名：%s\n"
            "  口  令：%s\n"
            "  该账号首次登录后必须先改密；改密前除 /api/auth/me 与改密接口外，\n"
            "  任何请求都会返回 403。不经网页直接改密：\n"
            "    curl -X POST http://127.0.0.1:8000/api/auth/change-password \\\n"
            "      -H \"Content-Type: application/json\" \\\n"
            "      -H \"Authorization: Bearer <登录接口返回的 token>\" \\\n"
            "      -d '{\"old_password\":\"<上面的口令>\",\"new_password\":\"<新的强口令>\"}'\n"
            "  想自己指定初始口令，就设环境变量 ADMIN_PASSWORD（弱口令会被拒绝启动）。\n"
            "========================================================================",
            username,
            password,
        )
    else:
        logger.info("已创建管理员 %s（%s），口令取自 ADMIN_PASSWORD", username, email)


def get_session_factory() -> sessionmaker:
    """取会话工厂；尚未初始化时按默认路径兜底初始化一次。"""
    if _session_factory is None:
        init_db()
    return _session_factory


def dispose_engine() -> None:
    """释放引擎（测试隔离用；生产进程不调用）。"""
    global _engine, _session_factory, _db_path

    with _lock:
        if _engine is not None:
            _engine.dispose()
        _engine = None
        _session_factory = None
        _db_path = None
