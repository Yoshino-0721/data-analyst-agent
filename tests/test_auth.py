"""用户系统测试：注册 / 登录 / JWT / 权限依赖 / 默认管理员 / 口令哈希。"""

from __future__ import annotations

import logging

import jwt as pyjwt
import pytest
from sqlalchemy import select

from src.auth import db as auth_db
from src.auth.models import User
from src.auth.security import create_token, decode_token, hash_password, verify_password
from tests.conftest import (
    TEST_ADMIN_PASSWORD,
    admin_headers,
    auth_headers,
    register_and_login,
)


# ---------------------------------------------------------------- 口令哈希


class TestPasswordHashing:
    def test_same_password_different_salt(self):
        assert hash_password("secret123") != hash_password("secret123")

    def test_verify_roundtrip(self):
        hashed = hash_password("secret123")
        assert verify_password("secret123", hashed)
        assert not verify_password("wrong", hashed)

    def test_verify_malformed_hash_is_false_not_crash(self):
        assert not verify_password("secret123", "not-a-bcrypt-hash")

    def test_hash_is_not_plaintext(self):
        hashed = hash_password("secret123")
        assert "secret123" not in hashed


# ---------------------------------------------------------------- 注册


class TestRegistration:
    def test_register_creates_a_pending_account(self, client):
        """自助注册**不发 token**：账号先是 pending，等管理员审核（见
        tests/test_registration_approval.py 的全链路）。

        这里只钉"注册这一步本身"的契约：返回 pending 标志与用户信息，
        且该账号此刻不可用（is_active=False）。
        """
        response = client.post(
            "/api/auth/register",
            json={"username": "alice", "email": "alice@example.com", "password": "secret123"},
        )
        assert response.status_code == 200, response.text
        body = response.json()
        assert body["pending"] is True
        assert "token" not in body
        assert body["user"]["username"] == "alice"
        assert body["user"]["role"] == "user"
        assert body["user"]["status"] == "pending"
        assert body["user"]["is_active"] is False

    def test_approved_registration_can_log_in(self, client):
        """审核通过后，注册时设的口令即可登录（夹具封装了这条三步流程）。"""
        data = register_and_login(client, username="alice")
        assert data["token"]
        assert data["user"]["username"] == "alice"
        assert data["user"]["role"] == "user"
        assert data["user"]["is_active"] is True
        assert data["user"]["status"] == "active"

    def test_duplicate_username_rejected(self, client):
        register_and_login(client, username="alice")
        response = client.post(
            "/api/auth/register",
            json={"username": "alice", "email": "other@example.com", "password": "secret123"},
        )
        assert response.status_code == 400
        assert "已被注册" in response.json()["detail"]

    def test_duplicate_email_rejected(self, client):
        register_and_login(client, username="alice", email="a@example.com")
        response = client.post(
            "/api/auth/register",
            json={"username": "bob", "email": "a@example.com", "password": "secret123"},
        )
        assert response.status_code == 400

    def test_email_is_case_insensitive_unique(self, client):
        register_and_login(client, username="alice", email="A@Example.com")
        response = client.post(
            "/api/auth/register",
            json={"username": "bob", "email": "a@example.com", "password": "secret123"},
        )
        assert response.status_code == 400

    @pytest.mark.parametrize(
        "username", ["", "a", "has space", "bad!name", "x" * 33]
    )
    def test_invalid_username_rejected(self, client, username):
        response = client.post(
            "/api/auth/register",
            json={"username": username, "email": "u@example.com", "password": "secret123"},
        )
        assert response.status_code == 422

    def test_chinese_username_ok(self, client):
        data = register_and_login(client, username="小明_01")
        assert data["user"]["username"] == "小明_01"

    @pytest.mark.parametrize(
        "email", ["no-at-sign", "a@b", "a b@example.com", "@example.com"]
    )
    def test_invalid_email_rejected(self, client, email):
        response = client.post(
            "/api/auth/register",
            json={"username": "alice", "email": email, "password": "secret123"},
        )
        assert response.status_code == 422

    @pytest.mark.parametrize("password", ["", "12345", "x" * 65])
    def test_invalid_password_rejected(self, client, password):
        response = client.post(
            "/api/auth/register",
            json={"username": "alice", "email": "u@example.com", "password": password},
        )
        assert response.status_code == 422

    def test_password_stored_as_hash(self, client):
        register_and_login(client, username="alice", password="secret123")
        factory = auth_db.get_session_factory()
        with factory() as db:
            user = db.scalar(select(User).where(User.username == "alice"))
            assert user is not None
            assert user.password_hash != "secret123"
            assert user.password_hash.startswith("$2")


# ---------------------------------------------------------------- 登录


class TestLogin:
    def test_login_with_username(self, client):
        register_and_login(client, username="alice")
        response = client.post(
            "/api/auth/login", json={"account": "alice", "password": "secret123"}
        )
        assert response.status_code == 200
        assert response.json()["user"]["username"] == "alice"

    def test_login_with_email(self, client):
        register_and_login(client, username="alice", email="alice@example.com")
        response = client.post(
            "/api/auth/login", json={"account": "alice@example.com", "password": "secret123"}
        )
        assert response.status_code == 200

    def test_login_with_uppercase_email(self, client):
        register_and_login(client, username="alice", email="alice@example.com")
        response = client.post(
            "/api/auth/login", json={"account": "ALICE@EXAMPLE.COM", "password": "secret123"}
        )
        assert response.status_code == 200

    def test_wrong_password(self, client):
        register_and_login(client, username="alice")
        response = client.post(
            "/api/auth/login", json={"account": "alice", "password": "wrong-pass"}
        )
        assert response.status_code == 401

    def test_unknown_account(self, client):
        response = client.post(
            "/api/auth/login", json={"account": "nobody", "password": "whatever"}
        )
        assert response.status_code == 401

    def test_login_message_does_not_leak_which_part_was_wrong(self, client):
        register_and_login(client, username="alice")
        wrong_password = client.post(
            "/api/auth/login", json={"account": "alice", "password": "wrong-pass"}
        )
        unknown_user = client.post(
            "/api/auth/login", json={"account": "nobody", "password": "wrong-pass"}
        )
        assert wrong_password.json()["detail"] == unknown_user.json()["detail"]


# ---------------------------------------------------------------- token 与 /me


class TestToken:
    def test_me_returns_current_user(self, client):
        data = register_and_login(client, username="alice")
        response = client.get("/api/auth/me", headers=auth_headers(data["token"]))
        assert response.status_code == 200
        assert response.json()["username"] == "alice"

    def test_me_without_token(self, client):
        response = client.get("/api/auth/me")
        assert response.status_code == 401

    def test_me_with_garbage_token(self, client):
        response = client.get("/api/auth/me", headers=auth_headers("not-a-jwt"))
        assert response.status_code == 401

    def test_expired_token_rejected(self, client):
        data = register_and_login(client, username="alice")
        factory = auth_db.get_session_factory()
        with factory() as db:
            user = db.scalar(select(User).where(User.username == "alice"))
            expired = create_token(user, expires_hours=-1)

        response = client.get("/api/auth/me", headers=auth_headers(expired))
        assert response.status_code == 401

    def test_token_signed_with_other_secret_rejected(self, client, monkeypatch):
        data = register_and_login(client, username="alice")
        # 换一个密钥签一份 token，模拟伪造
        monkeypatch.setenv("JWT_SECRET", "attacker-secret")
        factory = auth_db.get_session_factory()
        with factory() as db:
            user = db.scalar(select(User).where(User.username == "alice"))
            forged = create_token(user)

        monkeypatch.setenv("JWT_SECRET", "unit-test-secret")
        response = client.get("/api/auth/me", headers=auth_headers(forged))
        assert response.status_code == 401

    def test_token_payload_has_role(self, client):
        data = register_and_login(client, username="alice")
        payload = decode_token(data["token"])
        assert payload["role"] == "user"
        assert payload["username"] == "alice"

    def test_decode_rejects_tampered_token(self):
        token = pyjwt.encode(
            {"sub": "1", "role": "admin", "exp": 9999999999},
            "some-secret",
            algorithm="HS256",
        )
        with pytest.raises(pyjwt.InvalidTokenError):
            decode_token(token)


# ---------------------------------------------------------------- 禁用用户


class TestDisabledUser:
    def _disable(self, username: str) -> None:
        factory = auth_db.get_session_factory()
        with factory() as db:
            user = db.scalar(select(User).where(User.username == username))
            user.is_active = False
            db.commit()

    def test_existing_token_rejected_once_disabled(self, client):
        data = register_and_login(client, username="alice")
        self._disable("alice")

        response = client.get("/api/auth/me", headers=auth_headers(data["token"]))
        assert response.status_code == 403
        assert "禁用" in response.json()["detail"]

    def test_disabled_user_cannot_login(self, client):
        register_and_login(client, username="alice")
        self._disable("alice")

        response = client.post(
            "/api/auth/login", json={"account": "alice", "password": "secret123"}
        )
        assert response.status_code == 403


# ---------------------------------------------------------------- 修改密码


class TestChangePassword:
    def test_change_password_roundtrip(self, client):
        data = register_and_login(client, username="alice")

        response = client.post(
            "/api/auth/change-password",
            json={"old_password": "secret123", "new_password": "brand-new-6"},
            headers=auth_headers(data["token"]),
        )
        assert response.status_code == 200

        old_login = client.post(
            "/api/auth/login", json={"account": "alice", "password": "secret123"}
        )
        new_login = client.post(
            "/api/auth/login", json={"account": "alice", "password": "brand-new-6"}
        )
        assert old_login.status_code == 401
        assert new_login.status_code == 200

    def test_wrong_old_password(self, client):
        data = register_and_login(client, username="alice")
        response = client.post(
            "/api/auth/change-password",
            json={"old_password": "wrong-pass", "new_password": "brand-new-6"},
            headers=auth_headers(data["token"]),
        )
        assert response.status_code == 400

    def test_requires_auth(self, client):
        response = client.post(
            "/api/auth/change-password",
            json={"old_password": "x", "new_password": "brand-new-6"},
        )
        assert response.status_code == 401


# ---------------------------------------------------------------- 默认管理员


class TestDefaultAdmin:
    def test_fresh_db_bootstraps_admin(self, client):
        """conftest 里 init_db 建的是全新库 —— admin 应当已被引导创建。

        口令由 ADMIN_PASSWORD 显式提供（见 conftest），所以**不**强制改密。
        """
        response = client.post(
            "/api/auth/login",
            json={"account": "admin", "password": TEST_ADMIN_PASSWORD},
        )
        assert response.status_code == 200
        assert response.json()["user"]["role"] == "admin"
        assert response.json()["user"]["must_change_password"] is False

    def test_admin_env_override(self, tmp_path, monkeypatch):
        strong = "Boss-Override-9x!"
        monkeypatch.setenv("ADMIN_USERNAME", "boss")
        monkeypatch.setenv("ADMIN_PASSWORD", strong)
        monkeypatch.setenv("ADMIN_EMAIL", "boss@example.com")

        auth_db.init_db(tmp_path / "override.db")
        try:
            factory = auth_db.get_session_factory()
            with factory() as db:
                admin = db.scalar(select(User).where(User.username == "boss"))
                assert admin is not None
                assert admin.role == "admin"
                assert verify_password(strong, admin.password_hash)
                # 口令是外部显式给的，不强制改密
                assert admin.must_change_password is False
        finally:
            auth_db.dispose_engine()

    def test_second_init_does_not_duplicate_admin(self, tmp_path):
        db_file = tmp_path / "again.db"
        auth_db.init_db(db_file)
        try:
            auth_db.init_db(db_file)  # 同库再次初始化（模拟重启）
            factory = auth_db.get_session_factory()
            with factory() as db:
                admins = db.scalars(select(User).where(User.role == "admin")).all()
                assert len(admins) == 1
        finally:
            auth_db.dispose_engine()

    def test_explicit_admin_password_is_used_and_not_forced_to_change(
        self, tmp_path, caplog
    ):
        """显式给了强口令：用它，且不强制改密。

        （"没给口令时生成随机强口令 + 强制改密"以及"弱口令拒绝启动"这两条
        由 tests/test_auth_security.py 专门覆盖。）
        """
        with caplog.at_level(logging.INFO, logger="src.auth.db"):
            auth_db.init_db(tmp_path / "explicit.db")
        try:
            assert any("ADMIN_PASSWORD" in record.getMessage() for record in caplog.records)
            # 固定默认口令必须彻底消失
            assert not any("admin123" in record.getMessage() for record in caplog.records)
        finally:
            auth_db.dispose_engine()


# ---------------------------------------------------------------- 管理员依赖


class TestRequireAdmin:
    def test_normal_user_gets_403_on_admin_dependency(self, client):
        # settings 端点在 C2 才收紧，这里直接用依赖函数验证判定逻辑
        from fastapi import HTTPException

        from src.auth.deps import require_admin
        from src.auth.models import User

        normal = User(username="u", email="u@example.com", password_hash="x", role="user")
        with pytest.raises(HTTPException) as exc_info:
            require_admin(normal)
        assert exc_info.value.status_code == 403

    def test_admin_passes_dependency(self):
        from src.auth.deps import require_admin
        from src.auth.models import User

        admin = User(username="a", email="a@example.com", password_hash="x", role="admin")
        assert require_admin(admin) is admin

    def test_admin_headers_fixture_logs_in(self, client):
        headers = admin_headers(client)
        response = client.get("/api/auth/me", headers=headers)
        assert response.status_code == 200
        assert response.json()["role"] == "admin"
