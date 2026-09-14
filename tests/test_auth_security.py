"""账号与密钥安全加固测试。

三条硬规则，每条都对应一个真实事故：

1. **弱口令零容忍**：显式设了 ``ADMIN_PASSWORD=admin123``（或任何弱口令）
   直接终止启动 —— 一个全站可见的固定口令，等于把"部署到公网后忘记改"
   变成必然事件。
2. **不给口令就发随机口令 + 强制改密**：随机口令在启动横幅里打印一次，
   该账号在改密前除 ``/api/auth/me`` 与改密接口外一律 403，且新口令必须够强
   （否则"随机强口令 + 强制修改"这条链会被一次改成 ``123456`` 直接绕过）。
3. **生产环境必须外部注入 JWT_SECRET**：随机生成虽然会落盘，但容器重建 /
   换机器会连 ``storage/`` 一起换掉，所有人被静默强制重登；多实例更是各签各的。
   这类问题必须在启动时就暴露。

另外覆盖一个容易漏的运维点：老 ``storage/app.db`` 没有
``must_change_password`` 列，升级后必须自动补列而不是直接报错。
"""

from __future__ import annotations

import logging
import re
import sqlite3

import pytest
from sqlalchemy import select

from src.auth import db as auth_db
from src.auth import security
from src.auth.models import User
from src.auth.security import (
    generate_strong_password,
    is_production,
    password_strength_problem,
    verify_password,
    verify_security_config,
)
from src.config import settings
from tests.conftest import auth_headers

STRONG = "Fresh-Admin-7z!"
BANNER_PASSWORD_RE = re.compile(r"口\s*令：(\S+)")


def _init_without_admin_password(db_file, caplog):
    """在"没设 ADMIN_PASSWORD"的条件下引导一次，返回（明文口令, admin 行）。"""
    with caplog.at_level(logging.WARNING, logger="src.auth.db"):
        auth_db.init_db(db_file)
    factory = auth_db.get_session_factory()
    with factory() as db:
        admin = db.scalar(select(User).where(User.role == "admin"))

    banner = "\n".join(record.getMessage() for record in caplog.records)
    match = BANNER_PASSWORD_RE.search(banner)
    assert match is not None, f"启动横幅里没有打印初始口令：\n{banner}"
    return match.group(1), admin


# ---------------------------------------------------------------- 口令强度判定


class TestPasswordStrength:
    @pytest.mark.parametrize(
        "weak",
        ["admin123", "admin123456", "password", "P@ssw0rd", "12345678", "changeme"],
    )
    def test_deny_listed_passwords_are_rejected(self, weak):
        """deny-list 挡的是"够长但人人都会先试"的那一类。"""
        assert password_strength_problem(weak) is not None

    @pytest.mark.parametrize("short", ["Ab1!", "Short-9x", "Abc-1234567"])
    def test_too_short_is_rejected(self, short):
        assert password_strength_problem(short) is not None

    def test_too_few_character_classes_is_rejected(self):
        assert password_strength_problem("alllowercaseletters") is not None
        assert password_strength_problem("12345678901234567890") is not None

    def test_strong_password_passes(self):
        assert password_strength_problem("Str0ng-Password!") is None
        assert password_strength_problem(STRONG) is None

    def test_reason_never_echoes_the_password(self):
        """报错会进日志，绝不能把口令本身写进去。"""
        reason = password_strength_problem("admin123") or ""
        assert "admin123" not in reason

    def test_generator_always_produces_a_strong_password(self):
        generated = {generate_strong_password() for _ in range(20)}
        assert len(generated) == 20, "随机口令不该出现重复"
        for candidate in generated:
            assert password_strength_problem(candidate) is None
            assert len(candidate) >= 12
            assert candidate != "admin123"


# ---------------------------------------------------------------- 引导行为


class TestBootstrap:
    def test_weak_admin_password_refuses_to_start(self, tmp_path, monkeypatch):
        """显式设弱口令 → RuntimeError 终止启动，并告诉运维怎么生成强口令。"""
        monkeypatch.setenv("ADMIN_PASSWORD", "admin123")
        try:
            with pytest.raises(RuntimeError) as exc_info:
                auth_db.init_db(tmp_path / "weak.db")
        finally:
            auth_db.dispose_engine()

        message = str(exc_info.value)
        assert "ADMIN_PASSWORD" in message
        assert "secrets.token_urlsafe" in message, "必须给出生成强口令的具体命令"

    def test_missing_admin_password_generates_random_and_forces_change(
        self, tmp_path, monkeypatch, caplog
    ):
        monkeypatch.delenv("ADMIN_PASSWORD", raising=False)
        password, admin = _init_without_admin_password(tmp_path / "gen.db", caplog)
        try:
            assert admin is not None
            assert admin.must_change_password is True
            assert admin.password_hash != "admin123"
            # 横幅里那串口令必须真的能验过 —— 否则运维拿着它登不进去
            assert verify_password(password, admin.password_hash)
        finally:
            auth_db.dispose_engine()

    def test_explicit_strong_password_does_not_force_change(self, tmp_path, monkeypatch):
        monkeypatch.setenv("ADMIN_PASSWORD", STRONG)
        auth_db.init_db(tmp_path / "explicit.db")
        try:
            factory = auth_db.get_session_factory()
            with factory() as db:
                admin = db.scalar(select(User).where(User.role == "admin"))
                assert admin.must_change_password is False
                assert verify_password(STRONG, admin.password_hash)
        finally:
            auth_db.dispose_engine()


# ---------------------------------------------------------------- 强制改密闸门


class TestForcedPasswordChange:
    def test_gate_blocks_everything_until_password_is_changed(
        self, client, tmp_path, monkeypatch, caplog
    ):
        monkeypatch.delenv("ADMIN_PASSWORD", raising=False)
        generated, _admin = _init_without_admin_password(tmp_path / "gate.db", caplog)

        login = client.post(
            "/api/auth/login", json={"account": "admin", "password": generated}
        )
        assert login.status_code == 200, login.text
        assert login.json()["user"]["must_change_password"] is True
        headers = auth_headers(login.json()["token"])

        # 放行白名单：查自己
        assert client.get("/api/auth/me", headers=headers).status_code == 200

        # 其余一律 403，且文案直接告诉用户该调哪个接口
        blocked = client.get("/api/sessions", headers=headers)
        assert blocked.status_code == 403, blocked.text
        assert "修改密码" in blocked.json()["detail"]
        assert client.get("/api/admin/users", headers=headers).status_code == 403
        assert client.get("/api/workspace", headers=headers).status_code == 403

        # 想一次改成弱口令绕过 → 拒绝
        weak = client.post(
            "/api/auth/change-password",
            headers=headers,
            json={"old_password": generated, "new_password": "12345678"},
        )
        assert weak.status_code == 400, weak.text
        assert "强度" in weak.json()["detail"]

        # 换成强口令 → 放行
        changed = client.post(
            "/api/auth/change-password",
            headers=headers,
            json={"old_password": generated, "new_password": STRONG},
        )
        assert changed.status_code == 200, changed.text

        # 旧口令失效、新口令可用，且闸门解除
        assert (
            client.post(
                "/api/auth/login", json={"account": "admin", "password": generated}
            ).status_code
            == 401
        )
        relogin = client.post(
            "/api/auth/login", json={"account": "admin", "password": STRONG}
        )
        assert relogin.status_code == 200, relogin.text
        assert relogin.json()["user"]["must_change_password"] is False
        fresh = auth_headers(relogin.json()["token"])
        assert client.get("/api/sessions", headers=fresh).status_code == 200
        assert client.get("/api/admin/users", headers=fresh).status_code == 200

    def test_ungated_admin_is_not_affected(self, client, admin_h):
        """显式强口令引导出来的管理员不该被闸门碰到（否则整个后台都进不去）。"""
        assert client.get("/api/admin/users", headers=admin_h).status_code == 200

    def test_flag_is_exposed_on_registration_and_me(self, client):
        """`must_change_password` 必须在用户信息里，前端才有依据把用户按在改密页。"""
        body = client.post(
            "/api/auth/register",
            json={"username": "carol", "email": "carol@example.com", "password": "secret123"},
        ).json()
        assert body["user"]["must_change_password"] is False
        assert client.get("/api/auth/me", headers=auth_headers(body["token"])).json()[
            "must_change_password"
        ] is False


# ---------------------------------------------------------------- 生产环境密钥


class TestProductionSecret:
    @pytest.mark.parametrize("env_name", ["production", "prod", "staging", "DEV-PROD"])
    def test_non_dev_env_counts_as_production(self, env_name, monkeypatch):
        monkeypatch.setattr(settings, "env", env_name)
        monkeypatch.delenv("APP_ENV", raising=False)
        assert is_production() is True

    @pytest.mark.parametrize("env_name", ["dev", "test", "local"])
    def test_dev_like_env_is_not_production(self, env_name, monkeypatch):
        monkeypatch.setattr(settings, "env", env_name)
        assert is_production() is False

    def test_production_without_jwt_secret_refuses_to_start(self, monkeypatch):
        monkeypatch.setattr(settings, "env", "production")
        monkeypatch.delenv("JWT_SECRET", raising=False)
        with pytest.raises(RuntimeError) as exc_info:
            verify_security_config()

        message = str(exc_info.value)
        assert "JWT_SECRET" in message
        assert "token_urlsafe" in message, "必须给出生成密钥的具体命令"
        assert "多实例" in message, "要提醒多实例必须同值"

    def test_production_with_injected_secret_is_fine(self, monkeypatch):
        monkeypatch.setattr(settings, "env", "production")
        monkeypatch.setenv("JWT_SECRET", "u" * 48)
        verify_security_config()  # 不抛即通过

    def test_dev_without_secret_generates_and_persists_one(self, tmp_path, monkeypatch):
        """dev 下没有 JWT_SECRET 时仍然开箱即用，但密钥要落盘且稳定。"""
        monkeypatch.setattr(settings, "env", "dev")
        monkeypatch.delenv("JWT_SECRET", raising=False)
        monkeypatch.setattr(settings, "storage_root", tmp_path / "storage")

        first = security._secret()
        key_file = tmp_path / "storage" / "secret.key"
        assert key_file.is_file(), "dev 下的随机密钥也必须落盘，否则重启就掉登录"
        assert security._secret() == first, "重启后必须沿用同一把密钥"


# ---------------------------------------------------------------- 老库迁移


class TestLegacyDatabaseMigration:
    def test_legacy_users_table_gets_the_new_column(self, tmp_path, monkeypatch):
        """老库的 users 表没有 must_change_password 列，升级后必须自动补齐。"""
        db_file = tmp_path / "legacy.db"
        conn = sqlite3.connect(db_file)
        conn.executescript(
            "CREATE TABLE users ("
            " id INTEGER PRIMARY KEY,"
            " username VARCHAR(64),"
            " email VARCHAR(255),"
            " password_hash VARCHAR(255),"
            " role VARCHAR(16),"
            " is_active BOOLEAN,"
            " created_at DATETIME);"
        )
        conn.commit()
        conn.close()

        auth_db.init_db(db_file)
        try:
            conn = sqlite3.connect(db_file)
            columns = {row[1] for row in conn.execute("PRAGMA table_info(users)")}
            conn.close()
            assert "must_change_password" in columns

            factory = auth_db.get_session_factory()
            with factory() as db:
                admin = db.scalar(select(User).where(User.role == "admin"))
                assert admin is not None, "补列之后引导流程必须照常可用"
                assert admin.must_change_password is False
        finally:
            auth_db.dispose_engine()

    def test_init_is_idempotent_on_an_upgraded_db(self, tmp_path, monkeypatch):
        """补列必须幂等：重复 init_db 不能报 duplicate column。"""
        db_file = tmp_path / "twice.db"
        auth_db.init_db(db_file)
        auth_db.init_db(db_file)
        try:
            factory = auth_db.get_session_factory()
            with factory() as db:
                assert len(db.scalars(select(User).where(User.role == "admin")).all()) == 1
        finally:
            auth_db.dispose_engine()
