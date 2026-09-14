"""FastAPI 服务：把 Agent 包成一个本地网页。

接口很薄 —— 真正的逻辑全在 agent/sandbox/schema 三层里，这里只做几件事：
接上传、跑一次循环、把执行轨迹结构化后交给前端，并把聊天与轨迹落库。

多用户改造后的职责边界（与项目一 rag-knowledge-base/src/server.py 同构）：

- **登录态**：``/``、``/api/health`` 与 ``/api/auth/*`` 之外的全部接口都要
  Bearer token（``get_current_user``）；
- **数据隔离**：上传 / 提问 / 产物 / 数据集全部限定在当前用户自己的
  ``Session``（``get_workspace(user.id)``）里，跨用户一律 404 不泄露存在性；
- **会话持久化**：聊天记录存 SQLite（sessions / messages），执行轨迹塞进
  assistant 消息的 ``meta`` —— 管理员看轨迹的唯一来源，不再单独建表；
- **健康检查分层**：公开的 ``/api/health`` 只说全局状态，用户自己的文件与
  运行目录走需要登录的 ``/api/workspace``。

一个刻意的设计：**执行器与 LLM 客户端都是懒加载的**。
模块导入时不构造它们，页面上传阶段也不需要。这样即使还没配 API Key，
页面也能正常打开并把「缺 Key」以人话显示在界面上，而不是启动即崩。

一个不起眼但很关键的细节：**所有异常都走 JSON 兜底**。Starlette 的默认
ServerErrorMiddleware 在 DEBUG=False 时会返 21 字节纯文本 "Internal Server Error"，
前端 `await resp.json()` 立刻抛 `SyntaxError`，UI 只能显示一段晦涩的
"Unexpected token 'I', ..."。装一个 Exception 处理器后，任何未捕获异常
都会被拍平成 `{"detail": "..."}` JSON，前端能稳定按统一协议处理。
"""

from __future__ import annotations

import json
import logging
from contextlib import asynccontextmanager
from datetime import datetime
from functools import lru_cache
from pathlib import Path
from typing import Any

from fastapi import Depends, FastAPI, File, HTTPException, UploadFile
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session as OrmSession

from .admin_api import router as admin_router
from .agent.loop import AgentResult, run_agent
from .agent.tools import ToolRuntime
from .auth.api import router as auth_router
from .auth.db import init_db
from .auth.deps import get_current_user, get_db
from .auth.models import ChatSession, Dataset, Message, User
from .config import settings
from .datasets import dataset_payload, delete_dataset, sync_datasets
from .llm.client import ZhipuClient
from .sandbox.factory import build_executor
from .schema.extractor import SchemaError
from .serializers import message_payload, session_payload
from .workspaces import get_workspace

logger = logging.getLogger(__name__)

PROJECT_ROOT = Path(__file__).resolve().parent.parent
WEB_DIR = PROJECT_ROOT / "web"


@asynccontextmanager
async def lifespan(_app: FastAPI):
    # 建库 + 默认管理员引导（幂等）。测试不跑 lifespan，由 fixture 自行初始化。
    init_db()
    yield


app = FastAPI(
    title="私人数据分析师 Agent",
    docs_url="/api/docs",
    openapi_url="/api/openapi.json",
    lifespan=lifespan,
)
app.include_router(auth_router)
app.include_router(admin_router)


# ---------------------------------------------------------------- 全局异常兜底

@app.exception_handler(Exception)
def _unhandled_exception(_request, exc: Exception) -> JSONResponse:
    """把任何逃过接口 try/except 的异常拍平成 JSON。

    复盘：用户用真实 Excel 提问时偶发一个 500，Starlette 默认会把异常吞成
    21 字节纯文本 "Internal Server Error"。浏览器 `await resp.json()` 立刻
    抛 SyntaxError，UI 只能显示「Unexpected token 'I', "Internal S"...」。
    看着像前端 bug，实际是后端返的不是 JSON。装上这个处理器后所有 5xx 都是
    结构化 JSON，前端能稳定按 `detail` 字段显示人话。
    """
    # HTTPException 已经有自己的处理器，会先一步被 FastAPI 路由走；
    # 但 RequestValidationError（pydantic 校验失败）等不会走 HTTPException，
    # 它们默认也被这个兜底接住 —— 一并拍平。
    logger.exception("未捕获异常：%s", exc)
    return JSONResponse(
        status_code=500,
        content={"detail": f"服务器内部错误：{exc!s}"[:500]},
    )


# ---------------------------------------------------------------- 懒加载依赖
#
# settings 用 src.config 里的**全局单例**，这里绝不再造一个 Settings.from_env()。
# auth/db.py 与 auth/security.py 都是 `from .config import settings`，两份实例会
# 让「测试 monkeypatch server.settings 的 storage_root」影响不到建库路径 ——
# 那是典型的「测试过了、线上不生效」。项目一就是这么做的，保持一致。


@lru_cache(maxsize=1)
def get_executor():
    return build_executor(settings)


@lru_cache(maxsize=1)
def get_client() -> ZhipuClient:
    return ZhipuClient(
        api_key=settings.require_api_key(),
        model=settings.model,
        base_url=settings.base_url,
        proxy=settings.proxy,
    )


def reset_caches() -> None:
    """清掉缓存的执行器/客户端。配置变了或测试中需要重建时调用。"""
    get_executor.cache_clear()
    get_client.cache_clear()


# ---------------------------------------------------------------- 数据模型


class AskRequest(BaseModel):
    """问答请求。``session_id`` 缺省时自动开一个新会话。"""

    question: str = Field(min_length=1, max_length=2000)
    session_id: int | None = None


class SessionCreate(BaseModel):
    title: str | None = Field(default=None, max_length=200)


class SessionRename(BaseModel):
    title: str = Field(min_length=1, max_length=200)


# ---------------------------------------------------------------- 会话工具


def _own_session(db: OrmSession, user: User, session_id: int) -> ChatSession:
    """取属于当前用户的会话；不存在或不属于本人一律 404（不泄露存在性）。"""
    chat_session = db.get(ChatSession, session_id)
    if chat_session is None or chat_session.user_id != user.id:
        raise HTTPException(status_code=404, detail="会话不存在")
    return chat_session


# ---------------------------------------------------------------- 页面


@app.get("/", response_class=HTMLResponse)
def index() -> HTMLResponse:
    page = WEB_DIR / "index.html"
    if not page.is_file():
        raise HTTPException(status_code=500, detail="前端文件缺失：web/index.html")
    return HTMLResponse(page.read_text(encoding="utf-8"))


# ---------------------------------------------------------------- 基础接口


@app.get("/api/health")
def health() -> dict[str, Any]:
    """公开的健康检查。**只讲全局状态，不碰任何用户数据**。

    刻意不再返回 ``files`` / ``runs_dir`` —— 那是某个用户的工作区内容，
    公开接口带出去就是泄露。用户自己的那份挪到 ``/api/workspace``。
    """
    executor_ok, executor_note = True, ""
    try:
        executor = get_executor()
        executor_ok, executor_note = executor.available()
    except Exception as exc:  # noqa: BLE001
        executor_ok, executor_note = False, str(exc)

    return {
        "ok": True,
        "has_api_key": bool(settings.api_key),
        "model": settings.model,
        "executor": settings.executor,
        "executor_ok": executor_ok,
        "executor_note": executor_note,
    }


@app.get("/api/workspace")
def read_workspace(user: User = Depends(get_current_user)) -> dict[str, Any]:
    """当前用户自己的工作区概览：已加载的数据文件与运行目录。"""
    workspace = get_workspace(user.id)
    return {
        "files": [_file_info(schema) for schema in workspace.schemas],
        "runs_dir": str(workspace.runs_dir),
    }


# ---------------------------------------------------------------- 上传 / 提问


@app.post("/api/upload")
async def upload(
    files: list[UploadFile] = File(...),
    user: User = Depends(get_current_user),
    db: OrmSession = Depends(get_db),
) -> dict[str, Any]:
    uploads: list[tuple[str, bytes]] = []
    for item in files:
        uploads.append((item.filename or "data", await item.read()))

    workspace = get_workspace(user.id)
    try:
        schemas = workspace.replace_files(uploads)
    except SchemaError as exc:
        workspace.last_error = str(exc)
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    # 工作区是真相，表只是可查询的索引 —— 上传后立刻回写
    sync_datasets(db, user.id, workspace)
    return {"files": [_file_info(schema) for schema in schemas]}


@app.post("/api/ask")
def ask(
    payload: AskRequest,
    user: User = Depends(get_current_user),
    db: OrmSession = Depends(get_db),
) -> dict[str, Any]:
    """跑一次完整的工具调用循环。这是唯一会真正执行代码的接口。

    执行轨迹（``steps``）与产物列表会随 assistant 消息一起写进 ``meta`` ——
    这是管理员事后查看「模型到底跑了什么」的唯一来源。
    """
    question = payload.question.strip()
    if not question:
        raise HTTPException(status_code=400, detail="问题不能为空")

    if payload.session_id is None:
        chat_session = ChatSession(user_id=user.id, title=question[:50])
        db.add(chat_session)
        db.commit()
        db.refresh(chat_session)
    else:
        chat_session = _own_session(db, user, payload.session_id)

    # 只挂当前用户自己的文件 —— 跨用户隔离就落在这里
    workspace = get_workspace(user.id)
    if not workspace.files:
        raise HTTPException(status_code=400, detail="请先上传数据文件")

    try:
        client = get_client()
    except RuntimeError as exc:
        # 配置错误要原样透给用户 —— 模型改不了代码，是环境没配好
        raise HTTPException(status_code=503, detail=str(exc)) from exc

    try:
        executor = get_executor()
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(status_code=503, detail=f"执行器不可用：{exc}") from exc

    runtime = ToolRuntime(
        executor=executor,
        schemas=workspace.schemas,
        data_files=workspace.files,
        artifact_dir=workspace.artifacts_dir,
        run_dir_factory=workspace.new_run_dir,
        # 与执行器共用同一份限额配置 —— 前端跑的单次执行和沙箱层的
        # 超时/输出上限必须是同一个数，否则两边会对不上。
        timeout_seconds=settings.executor_config.timeout_seconds,
        max_output_bytes=settings.executor_config.max_output_bytes,
    )

    try:
        result = run_agent(
            question,
            client=client,
            runtime=runtime,
            schemas=workspace.schemas,
            max_steps=settings.agent_max_steps,
            max_repeat_failures=settings.max_repeat_failures,
            history_max_chars=settings.history_max_chars,
            history_keep_turns=settings.history_keep_turns,
        )
    except RuntimeError as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc

    answer = _serialize(result)
    db.add(Message(session_id=chat_session.id, role="user", content=question))
    db.add(
        Message(
            session_id=chat_session.id,
            role="assistant",
            content=result.answer,
            # 轨迹与产物一起入库：管理员审计、用户回看历史都靠它
            meta=json.dumps(
                {
                    "steps": answer["steps"],
                    "artifacts": answer["artifacts"],
                    "terminated_by": answer["terminated_by"],
                },
                ensure_ascii=False,
            ),
        )
    )
    if chat_session.title == "新会话":
        chat_session.title = question[:50]
    chat_session.updated_at = datetime.now()
    db.commit()

    return {**answer, "session_id": chat_session.id}


@app.get("/api/artifact/{name}")
def artifact(name: str, user: User = Depends(get_current_user)) -> FileResponse:
    """取产物文件（图表等）。归属天然成立：只看当前用户自己的产物目录；
    路径穿越由 session.artifact_path 拦住。"""
    path = get_workspace(user.id).artifact_path(name)
    if path is None:
        raise HTTPException(status_code=404, detail=f"产物不存在：{name}")
    return FileResponse(path)


@app.post("/api/reset")
def reset(
    user: User = Depends(get_current_user), db: OrmSession = Depends(get_db)
) -> dict[str, Any]:
    workspace = get_workspace(user.id)
    workspace.clear_files()
    # 工作区空了 → datasets 表里该用户的行走「工作区里已消失」这条分支全删
    sync_datasets(db, user.id, workspace)
    return {"ok": True}


# ---------------------------------------------------------------- 会话


@app.get("/api/sessions")
def list_sessions(
    user: User = Depends(get_current_user), db: OrmSession = Depends(get_db)
) -> dict:
    sessions = (
        db.query(ChatSession)
        .filter_by(user_id=user.id)
        .order_by(ChatSession.updated_at.desc(), ChatSession.id.desc())
        .all()
    )
    return {"sessions": [session_payload(s) for s in sessions]}


@app.post("/api/sessions")
def create_session(
    payload: SessionCreate | None = None,
    user: User = Depends(get_current_user),
    db: OrmSession = Depends(get_db),
) -> dict:
    chat_session = ChatSession(
        user_id=user.id,
        title=(payload.title.strip() if payload and payload.title else None) or "新会话",
    )
    db.add(chat_session)
    db.commit()
    db.refresh(chat_session)
    return session_payload(chat_session)


@app.get("/api/sessions/{session_id}/messages")
def list_messages(
    session_id: int,
    user: User = Depends(get_current_user),
    db: OrmSession = Depends(get_db),
) -> dict:
    chat_session = _own_session(db, user, session_id)
    messages = (
        db.query(Message).filter_by(session_id=chat_session.id).order_by(Message.id).all()
    )
    return {
        "session": session_payload(chat_session),
        "messages": [message_payload(m) for m in messages],
    }


@app.patch("/api/sessions/{session_id}")
def rename_session(
    session_id: int,
    payload: SessionRename,
    user: User = Depends(get_current_user),
    db: OrmSession = Depends(get_db),
) -> dict:
    chat_session = _own_session(db, user, session_id)
    chat_session.title = payload.title.strip()
    db.commit()
    return session_payload(chat_session)


@app.delete("/api/sessions/{session_id}")
def delete_session(
    session_id: int,
    user: User = Depends(get_current_user),
    db: OrmSession = Depends(get_db),
) -> dict:
    chat_session = _own_session(db, user, session_id)
    db.delete(chat_session)  # messages 级联删除
    db.commit()
    return {"ok": True}


# ---------------------------------------------------------------- 数据集


@app.get("/api/datasets")
def list_datasets(
    user: User = Depends(get_current_user), db: OrmSession = Depends(get_db)
) -> dict:
    rows = (
        db.query(Dataset)
        .filter(Dataset.user_id == user.id)
        .order_by(Dataset.id)
        .all()
    )
    return {"datasets": [dataset_payload(row) for row in rows]}


@app.delete("/api/datasets/{dataset_id}")
def delete_dataset_endpoint(
    dataset_id: int,
    user: User = Depends(get_current_user),
    db: OrmSession = Depends(get_db),
) -> dict:
    """删掉自己的一个数据集：文件（尽力而为）→ 内存工作区 → 表行。"""
    row = db.get(Dataset, dataset_id)
    if row is None or row.user_id != user.id:
        raise HTTPException(status_code=404, detail="数据集不存在")

    workspace = get_workspace(user.id)
    return delete_dataset(db, workspace, row)


# ---------------------------------------------------------------- 序列化


def _file_info(schema) -> dict[str, Any]:
    return {
        "name": schema.file_name,
        "path": schema.data_path,
        "rows": schema.n_rows,
        "cols": schema.n_cols,
        "columns": [
            {"name": c.name, "dtype": c.dtype, "null_ratio": round(c.null_ratio, 4)}
            for c in schema.columns
        ],
    }


def _serialize(result: AgentResult) -> dict[str, Any]:
    """把 AgentResult 摊平成前端好渲染的结构。

    **代码原样带出去**：前端要把模型写的每一段 Python 都显示出来。
    这是用户信任的来源 —— 看得见它到底跑了什么，才敢信它的结论。
    """
    steps: list[dict[str, Any]] = []
    for step in result.steps:
        calls = []
        outcomes = []
        for call in step.tool_calls:
            arguments = call.parsed_arguments()
            calls.append(
                {
                    "id": call.id,
                    "name": call.name,
                    "code": arguments.get("code", ""),
                    "file_name": arguments.get("file_name", ""),
                }
            )
        for outcome in step.outcomes:
            outcomes.append(
                {
                    "status": outcome.status,
                    "stdout": outcome.stdout,
                    "stderr": outcome.stderr,
                    "hint": outcome.hint,
                    "artifacts": list(outcome.artifacts),
                }
            )
        steps.append({"index": step.index, "calls": calls, "outcomes": outcomes})

    return {
        "answer": result.answer,
        "terminated_by": result.terminated_by,
        "artifacts": list(result.artifacts),
        "steps": steps,
    }


__all__ = ["app", "reset_caches", "settings"]
