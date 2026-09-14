"""共享测试夹具。"""

from __future__ import annotations

import pytest


@pytest.fixture(autouse=True)
def isolated_auth_db(tmp_path_factory, monkeypatch):
    """每个用例独享一个全新 SQLite，绝不让测试碰到真实的 storage/app.db。

    同时固定 JWT_SECRET，避免签发密钥落盘到真实 storage/ 目录。
    目录取自 tmp_path_factory，与用例自己的 tmp_path 互不干扰。
    """
    from src.auth import db as auth_db

    monkeypatch.setenv("JWT_SECRET", "unit-test-secret")
    auth_db.init_db(tmp_path_factory.mktemp("authdb") / "auth.db")
    yield
    auth_db.dispose_engine()


@pytest.fixture()
def client():
    """带独立认证库的 TestClient。"""
    from fastapi.testclient import TestClient

    from src.server import app

    return TestClient(app)


def auth_headers(token: str) -> dict:
    return {"Authorization": f"Bearer {token}"}


def register_and_login(client, username="alice", email=None, password="secret123") -> dict:
    """注册并登录一个用户，返回 {token, user}。"""
    response = client.post(
        "/api/auth/register",
        json={
            "username": username,
            "email": email or f"{username}@example.com",
            "password": password,
        },
    )
    assert response.status_code == 200, response.text
    return response.json()


def admin_headers(client) -> dict:
    """默认管理员（引导创建的 admin/admin123）登录后的请求头。"""
    response = client.post(
        "/api/auth/login", json={"account": "admin", "password": "admin123"}
    )
    assert response.status_code == 200, response.text
    return auth_headers(response.json()["token"])
