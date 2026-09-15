"""管理员建号（模块 6.5）。

背景：账号有两条来源 —— **自助注册**（提交后落成 ``pending``，要管理员审核通过才能
登录，见 ``test_registration_approval.py``）与**管理员建号**（本条，建出来即可用）。
管理端这条端点承担**两条安全承诺**，各自都有独立用例：

1. 口令由**服务端**生成、**只在响应里返回一次**，库里只有 bcrypt 哈希；
2. 新账号带 ``must_change_password=True`` —— 在本人把口令换掉之前，除
   ``/api/auth/me`` 与改密接口外一律 403（一次性口令不该被长期使用）。
"""

from __future__ import annotations

from sqlalchemy import select

from src.auth.db import get_session_factory
from src.auth.models import User
from src.auth.security import verify_password
from tests.conftest import auth_headers

NEW_USER = {"username": "newbie", "email": "newbie@example.com", "role": "user"}


def create(client, headers, **overrides):
    return client.post("/api/admin/users", json={**NEW_USER, **overrides}, headers=headers)


def login_as(client, username, password):
    return client.post("/api/auth/login", json={"account": username, "password": password})


def _row(username: str) -> User:
    factory = get_session_factory()
    with factory() as db:
        return db.scalar(select(User).where(User.username == username))


class TestAccessControl:
    def test_normal_user_gets_403(self, client, alice_headers):
        response = create(client, alice_headers)

        assert response.status_code == 403, response.text
        assert response.json()["detail"] == "需要管理员权限"

    def test_anonymous_gets_401(self, client):
        assert client.post("/api/admin/users", json=NEW_USER).status_code == 401

    def test_403_happens_before_any_account_is_created(self, client, alice_headers):
        """别漏这条：闸门必须在**建号之前**，否则任何人都能给自己开一个管理员。"""
        create(client, alice_headers, username="sneaky", role="admin")

        assert _row("sneaky") is None


class TestCreate:
    def test_admin_creates_user_and_gets_one_time_password(self, client, admin_h):
        response = create(client, admin_h)

        assert response.status_code == 200, response.text
        body = response.json()
        assert body["user"]["username"] == "newbie"
        assert body["user"]["role"] == "user"
        assert body["user"]["is_active"] is True
        # 复用 generate_strong_password，所以长度远超注册的 6 位下限
        assert len(body["password"]) >= 12

    def test_can_create_another_admin(self, client, admin_h):
        body = create(
            client, admin_h, username="boss", email="boss@example.com", role="admin"
        ).json()

        assert body["user"]["role"] == "admin"

    def test_duplicate_username_or_email_rejected(self, client, admin_h):
        assert create(client, admin_h).status_code == 200
        assert create(client, admin_h).status_code == 400               # 用户名 + 邮箱都重复
        assert create(client, admin_h, username="other").status_code == 400   # 仅邮箱重复

    def test_validation_rules_match_registration(self, client, admin_h):
        """与注册共用同一套规则：这里若放宽，管理端就成了绕过注册校验的后门。"""
        assert create(client, admin_h, username="a").status_code == 422        # 太短
        assert create(client, admin_h, email="not-an-email").status_code == 422
        assert create(client, admin_h, role="root").status_code == 422

    def test_password_never_returned_by_read_endpoints(self, client, admin_h):
        password = create(client, admin_h).json()["password"]

        listing = client.get("/api/admin/users", headers=admin_h)

        assert listing.status_code == 200
        assert password not in listing.text
        assert all("password" not in user for user in listing.json()["users"])


class TestOneTimePasswordStorage:
    def test_db_keeps_only_a_bcrypt_hash(self, client, admin_h):
        password = create(client, admin_h).json()["password"]

        row = _row("newbie")

        assert row is not None
        assert row.password_hash != password
        assert password not in row.password_hash
        assert verify_password(password, row.password_hash)

    def test_must_change_password_is_set(self, client, admin_h):
        create(client, admin_h)

        assert _row("newbie").must_change_password is True


class TestFirstLoginIsGated:
    def test_login_reports_must_change_password(self, client, admin_h):
        password = create(client, admin_h).json()["password"]

        response = login_as(client, "newbie", password)

        assert response.status_code == 200, response.text
        assert response.json()["user"]["must_change_password"] is True

    def test_gate_blocks_business_endpoints_until_password_changed(self, client, admin_h):
        password = create(client, admin_h).json()["password"]
        token = login_as(client, "newbie", password).json()["token"]
        headers = auth_headers(token)

        assert client.get("/api/auth/me", headers=headers).status_code == 200
        blocked = client.get("/api/sessions", headers=headers)
        assert blocked.status_code == 403, blocked.text
        assert "修改密码" in blocked.json()["detail"]

        changed = client.post(
            "/api/auth/change-password",
            headers=headers,
            json={"old_password": password, "new_password": "Fresh-Newbie-7z!"},
        )
        assert changed.status_code == 200, changed.text

        fresh = login_as(client, "newbie", "Fresh-Newbie-7z!").json()
        assert fresh["user"]["must_change_password"] is False
        assert client.get(
            "/api/sessions", headers=auth_headers(fresh["token"])
        ).status_code == 200

    def test_weak_new_password_is_rejected_while_gated(self, client, admin_h):
        """一次性口令必须换成强口令 —— 否则"生成强口令 + 强制改密"能被一次改成 123456 绕过。"""
        password = create(client, admin_h).json()["password"]
        token = login_as(client, "newbie", password).json()["token"]

        response = client.post(
            "/api/auth/change-password",
            headers=auth_headers(token),
            json={"old_password": password, "new_password": "12345678"},
        )

        assert response.status_code == 400, response.text
        assert "强度" in response.json()["detail"]
