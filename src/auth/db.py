"""数据库引擎与会话工厂。

SQLite 零运维，路径默认 ``storage/app.db``（测试用 ``AUTH_DB_PATH`` 或直接传
参隔离）。``init_db()`` 幂等建表，并在 users 表为空时创建默认管理员 ——
口令可用环境变量 ``ADMIN_USERNAME / ADMIN_PASSWORD / ADMIN_EMAIL`` 覆盖，
使用默认口令 ``admin123`` 时会打警告日志提醒改密。
"""

from __future__ import annotations

import logging
import os
import threading
from pathlib import Path

from sqlalchemy import create_engine, event, select
from sqlalchemy.orm import Session, sessionmaker

from src.auth.models import Base, User
from src.auth.security import hash_password

logger = logging.getLogger(__name__)

DEFAULT_ADMIN_USERNAME = "admin"
DEFAULT_ADMIN_PASSWORD = "admin123"
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


def init_db(path: Path | None = None) -> None:
    """建库建表并引导默认管理员。重复调用会切换到新路径重建引擎（测试用）。"""
    global _engine, _session_factory, _db_path

    path = Path(path) if path is not None else default_db_path()
    path.parent.mkdir(parents=True, exist_ok=True)

    with _lock:
        _engine = _create_engine(path)
        _db_path = path
        Base.metadata.create_all(_engine)
        _session_factory = sessionmaker(bind=_engine, expire_on_commit=False)

    with _session_factory() as session:
        _bootstrap_admin(session)


def _bootstrap_admin(session: Session) -> None:
    """users 表为空时创建默认管理员，让全新部署开箱即可登录。"""
    if session.scalar(select(User).limit(1)) is not None:
        return

    username = os.getenv("ADMIN_USERNAME", DEFAULT_ADMIN_USERNAME).strip() or DEFAULT_ADMIN_USERNAME
    email = os.getenv("ADMIN_EMAIL", DEFAULT_ADMIN_EMAIL).strip() or DEFAULT_ADMIN_EMAIL
    password = os.getenv("ADMIN_PASSWORD", DEFAULT_ADMIN_PASSWORD)

    session.add(
        User(
            username=username,
            email=email,
            password_hash=hash_password(password),
            role="admin",
        )
    )
    session.commit()
    logger.info("已创建默认管理员 %s（%s）", username, email)
    if password == DEFAULT_ADMIN_PASSWORD:
        logger.warning(
            "默认管理员使用默认口令 %s，请登录后立即在界面里修改！"
            "也可用环境变量 ADMIN_PASSWORD 指定初始口令。",
            DEFAULT_ADMIN_PASSWORD,
        )


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
