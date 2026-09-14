"""会话与消息的 JSON 序列化（用户接口与管理接口共用）。

与项目一 ``rag-knowledge-base/src/serializers.py`` 完全同构。唯一的差别在
``Message.meta`` 的语义：项目一放引用卡片，本项目放执行轨迹（steps /
artifacts / terminated_by）—— 序列化本身一样，都是「JSON 文本解出来给前端」。
解析失败时返回 None 而不是抛异常：一条 meta 写坏了不该让会话列表整个打不开。
"""

from __future__ import annotations

import json

from .auth.models import ChatSession, Message


def message_payload(message: Message) -> dict:
    meta = None
    if message.meta:
        try:
            meta = json.loads(message.meta)
        except json.JSONDecodeError:
            meta = None
    return {
        "id": message.id,
        "role": message.role,
        "content": message.content,
        "meta": meta,
        "created_at": message.created_at.isoformat() if message.created_at else None,
    }


def session_payload(chat_session: ChatSession, *, username: str | None = None) -> dict:
    data = {
        "id": chat_session.id,
        "title": chat_session.title,
        "created_at": chat_session.created_at.isoformat() if chat_session.created_at else None,
        "updated_at": chat_session.updated_at.isoformat() if chat_session.updated_at else None,
    }
    if username is not None:
        data["username"] = username
    return data


__all__ = ["message_payload", "session_payload"]
