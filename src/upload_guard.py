"""上传体积上限：**两道闸门**，缺一不可。

只信 ``Content-Length`` 是不够的 —— 它由客户端提供，可以伪造；而在
``Transfer-Encoding: chunked`` 下它根本不存在。所以：

1. **快速拒绝**（:func:`enforce_content_length`）：请求头声明超限就直接 413，
   **一个字节都不读**，避免白读一遍大文件；
2. **兜底**（:func:`read_within_limit`）：真正读取时按**累计字节**判断，
   超限立刻中断并 413。

只有 1 会被伪造头 / chunked 绕过；只有 2 则会让超大请求先占满内存才开始拒绝。
所以两条都要，且**上限是"整次请求"的总量**：多个文件共享同一份预算，
否则传 10 个各 49 MiB 的文件仍然能写满磁盘。
"""

from __future__ import annotations

from fastapi import HTTPException, Request, UploadFile

# 读取分片大小：1 MiB。太小会让大文件多很多次系统调用，太大则单次就可能超预算。
CHUNK_SIZE = 1 << 20

# 413 用字面量而不是 starlette 的常量：那个常量的名字跨版本改过
# （REQUEST_ENTITY_TOO_LARGE / CONTENT_TOO_LARGE），字面量不会因升级而 AttributeError。
_TOO_LARGE = 413


def limit_bytes() -> int:
    """当前上限（字节）。每次现读 settings，测试 monkeypatch 立刻生效。"""
    from src.config import settings

    return max(1, int(settings.max_upload_size))


def _too_large(limit: int) -> HTTPException:
    return HTTPException(
        status_code=_TOO_LARGE,
        detail=(
            f"上传内容超过上限（{limit // (1024 * 1024)} MiB）。"
            "请拆分文件，或调大服务端 MAX_UPLOAD_SIZE"
        ),
    )


def enforce_content_length(request: Request, limit: int | None = None) -> None:
    """按请求头快速拒绝。缺失或不可解析时**放行**，交给读取时兜底。"""
    limit = limit if limit is not None else limit_bytes()
    raw = request.headers.get("content-length")
    if not raw:
        return
    try:
        declared = int(raw)
    except (TypeError, ValueError):
        return
    if declared > limit:
        raise _too_large(limit)


async def read_within_limit(upload: UploadFile, remaining: int) -> bytes:
    """读取一个上传文件；累计超过 ``remaining`` 字节立即中断。

    ``remaining`` 是**整次请求**剩余的预算（调用方按已读字节递减），
    所以多文件上传的总量也被同一份上限约束。
    """
    if remaining <= 0:
        raise _too_large(limit_bytes())

    chunks: list[bytes] = []
    total = 0
    while True:
        chunk = await upload.read(CHUNK_SIZE)
        if not chunk:
            break
        total += len(chunk)
        if total > remaining:
            raise _too_large(limit_bytes())
        chunks.append(chunk)
    return b"".join(chunks)


__all__ = ["CHUNK_SIZE", "enforce_content_length", "limit_bytes", "read_within_limit"]
