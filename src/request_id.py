"""请求 id：让"一次请求"在日志与响应里对得上号。

用户报错时只要给出响应里的 request_id，运维 `grep <id>` 就能捞出这次请求的全部日志
（含完整堆栈）—— 这是"对外屏蔽细节、对内保留细节"能同时成立的前提。

三条从真实探针里得到的结论（改这里之前先读一遍）：

1. ``@app.middleware("http")`` 里 ``ContextVar.set`` 对**同步端点**同样有效 ——
   FastAPI 把同步端点丢进 anyio 线程池，而 anyio 会把上下文复制进工作线程。
2. **异常处理器里读不到那个 ContextVar**：处理器挂在比业务中间件更外层的
   ``ServerErrorMiddleware`` 上，而中间件的 ``finally`` 已经 reset 过了。所以 id 的
   真相放在 ``request.state``（即 ``scope["state"]``），处理器从那里取。
3. 处理器返回 ``JSONResponse`` 时要**显式**传 ``headers=`` —— 异常路径上中间件
   根本没机会给响应加头（异常是从中间件内部抛出去的）。
"""

from __future__ import annotations

import contextvars
import logging
import re
import uuid

REQUEST_ID_HEADER = "X-Request-ID"

# 入站头只在**严格白名单**下采信：换行与控制字符会造成日志注入（把一行日志劈成多行，
# 或把伪造内容写进日志）。`.` 与 `_` 是真实系统的常见字符，不构成注入风险。
_ALLOWED = re.compile(r"^[A-Za-z0-9._-]{1,64}$")

LOG_FORMAT = "%(asctime)s %(levelname)s [%(request_id)s] %(name)s: %(message)s"

_current: contextvars.ContextVar[str] = contextvars.ContextVar("request_id", default="-")


def new_id() -> str:
    """新 id：12 位十六进制，够短好念、又够长不撞。"""
    return uuid.uuid4().hex[:12]


def sanitize(raw: str | None) -> str | None:
    """合法则原样返回，否则 ``None``（调用方 fallback 到新生成的 id）。"""
    if not raw:
        return None
    candidate = raw.strip()
    return candidate if _ALLOWED.match(candidate) else None


def current() -> str:
    """当前请求的 id（不在请求上下文里时为 ``"-"``）。"""
    return _current.get()


def bind(scope: dict, rid: str) -> None:
    """把 id 写进 ``scope["state"]`` —— 异常处理器读的就是这里。"""
    scope.setdefault("state", {})["request_id"] = rid


def of(request) -> str:
    """从请求对象取 id（给异常处理器用）。"""
    return getattr(request.state, "request_id", "-")


class RequestIdFilter(logging.Filter):
    """给每条日志记录补上 ``request_id`` 属性，供 ``%(request_id)s`` 使用。"""

    def filter(self, record: logging.LogRecord) -> bool:
        if not getattr(record, "request_id", None):
            record.request_id = current()
        return True


def install(level: int = logging.INFO) -> None:
    """挂上过滤器。

    **不覆盖运维已配好的日志格式**：``basicConfig`` 在已有 handler 时是空操作
    （uvicorn 会自己配 handler），我们只保证每条记录都带 ``request_id`` 属性 ——
    谁想在格式里用就取得到；格式本身留给部署方。
    """
    logging.basicConfig(level=level, format=LOG_FORMAT)
    for handler in logging.getLogger().handlers:
        if not any(isinstance(f, RequestIdFilter) for f in handler.filters):
            handler.addFilter(RequestIdFilter())


__all__ = [
    "LOG_FORMAT",
    "REQUEST_ID_HEADER",
    "RequestIdFilter",
    "bind",
    "current",
    "install",
    "new_id",
    "of",
    "sanitize",
]
