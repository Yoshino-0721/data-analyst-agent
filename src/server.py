"""FastAPI 服务：把 Agent 包成一个本地网页。

接口很薄 —— 真正的逻辑全在 agent/sandbox/schema 三层里，这里只做三件事：
接上传、跑一次循环、把执行轨迹结构化后交给前端。

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

import logging
from contextlib import asynccontextmanager
from functools import lru_cache
from pathlib import Path
from typing import Any

from fastapi import FastAPI, File, HTTPException, UploadFile
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse
from pydantic import BaseModel, Field

from .agent.loop import AgentResult, run_agent
from .agent.tools import ToolRuntime
from .auth.api import router as auth_router
from .auth.db import init_db
from .config import Settings
from .llm.client import ZhipuClient
from .sandbox.factory import build_executor
from .schema.extractor import SchemaError
from .session import Session

logger = logging.getLogger(__name__)

PROJECT_ROOT = Path(__file__).resolve().parent.parent
WEB_DIR = PROJECT_ROOT / "web"
STORAGE_ROOT = PROJECT_ROOT / "storage" / "session"


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

settings = Settings.from_env()
session = Session(STORAGE_ROOT)


# ---------------------------------------------------------------- 懒加载依赖


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
    question: str = Field(min_length=1, max_length=2000)


# ---------------------------------------------------------------- 页面


@app.get("/", response_class=HTMLResponse)
def index() -> HTMLResponse:
    page = WEB_DIR / "index.html"
    if not page.is_file():
        raise HTTPException(status_code=500, detail="前端文件缺失：web/index.html")
    return HTMLResponse(page.read_text(encoding="utf-8"))


# ---------------------------------------------------------------- 接口


@app.get("/api/health")
def health() -> dict[str, Any]:
    """健康检查。**不抛异常** —— 缺配置也要能返回状态，让前端把话说清楚。"""
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
        "files": [_file_info(s) for s in session.schemas],
        "runs_dir": str(session.runs_dir),
    }


@app.post("/api/upload")
async def upload(files: list[UploadFile] = File(...)) -> dict[str, Any]:
    uploads: list[tuple[str, bytes]] = []
    for item in files:
        uploads.append((item.filename or "data", await item.read()))

    try:
        schemas = session.replace_files(uploads)
    except SchemaError as exc:
        session.last_error = str(exc)
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    return {"files": [_file_info(s) for s in schemas]}


@app.post("/api/ask")
def ask(payload: AskRequest) -> dict[str, Any]:
    """跑一次完整的工具调用循环。这是唯一会真正执行代码的接口。"""
    if not session.files:
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
        schemas=session.schemas,
        data_files=session.files,
        artifact_dir=session.artifacts_dir,
        run_dir_factory=session.new_run_dir,
        # 与执行器共用同一份限额配置 —— 前端跑的单次执行和沙箱层的
        # 超时/输出上限必须是同一个数，否则两边会对不上。
        timeout_seconds=settings.executor_config.timeout_seconds,
        max_output_bytes=settings.executor_config.max_output_bytes,
    )

    try:
        result = run_agent(
            payload.question,
            client=client,
            runtime=runtime,
            schemas=session.schemas,
            max_steps=settings.agent_max_steps,
            max_repeat_failures=settings.max_repeat_failures,
            history_max_chars=settings.history_max_chars,
            history_keep_turns=settings.history_keep_turns,
        )
    except RuntimeError as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc

    return _serialize(result)


@app.get("/api/artifact/{name}")
def artifact(name: str) -> FileResponse:
    """取产物文件（图表等）。路径穿越由 session.artifact_path 拦住。"""
    path = session.artifact_path(name)
    if path is None:
        raise HTTPException(status_code=404, detail=f"产物不存在：{name}")
    return FileResponse(path)


@app.post("/api/reset")
def reset() -> dict[str, Any]:
    session.clear_files()
    return {"ok": True}


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


__all__ = ["app", "reset_caches", "session", "settings"]
