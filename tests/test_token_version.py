"""改密即失效旧 token —— 以及"别让用户卡死"的完整链路（C2 复核清单 1.3）。

## 为什么存量 token 必须"一律失效"

升级前签发的 token 里没有版本号。这里刻意把它判为失效，而不是"缺字段就当版本 0
放行"：后者看着平滑，实际会让**这次安全改动对已签发的 token 完全不生效** ——
以为修好了，攻击者手里的 token 照样能用。代价是升级后所有人重新登录一次，
对自用 / 团队内部署可以接受。

## 为什么改密接口顺带返回新 token

旧 token 当次失效之后，如果响应里什么都不给，客户端下一个请求就是 401，
用户会以为"改密把账号弄坏了"。返回一个新签发的 token（只发给这次已认证的请求）
既保住了"旧凭据全灭"，又不会让人卡在门口。
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import jwt

from tests.conftest import auth_headers, register_and_login

# conftest 固定了 JWT_SECRET，所以这里能用同一把密钥伪造"升级前的老 token"
TEST_SECRET = "unit-test-secret"


def _legacy_token(user_id: int, username: str = "alice") -> str:
    """手工签一个**不带 token_version** 的 token，模拟升级前签发的存量 token。"""
    now = datetime.now(timezone.utc)
    return jwt.encode(
        {
            "sub": str(user_id),
            "username": username,
            "role": "user",
            "iat": now,
            "exp": now + timedelta(hours=1),
        },
        TEST_SECRET,
        algorithm="HS256",
    )


def _me(client, token: str):
    return client.get("/api/auth/me", headers=auth_headers(token))


class TestExistingTokensDieOnUpgrade:
    def test_legacy_token_without_version_is_rejected(self, client, alice):
        """升级前签发的 token（无版本号）必须 401 —— 这是"当次生效"的关键。"""
        response = _me(client, _legacy_token(alice["user"]["id"]))

        assert response.status_code == 401, response.text

    def test_fresh_token_carries_the_version(self, client, alice):
        assert _me(client, alice["token"]).status_code == 200
        decoded = jwt.decode(alice["token"], TEST_SECRET, algorithms=["HS256"])
        assert decoded["token_version"] == 0

    def test_tampered_version_is_rejected(self, client, alice):
        """把 payload 里的版本号改大也照样拒绝（签名已失效）。"""
        decoded = jwt.decode(alice["token"], TEST_SECRET, algorithms=["HS256"])
        decoded["token_version"] = 99
        forged = jwt.encode(decoded, "wrong-secret", algorithm="HS256")

        assert _me(client, forged).status_code == 401


class TestPasswordChangeInvalidatesOldTokens:
    def test_old_token_dies_and_response_gives_a_working_one(self, client):
        data = register_and_login(client, "alice")
        old_token = data["token"]
        assert _me(client, old_token).status_code == 200

        changed = client.post(
            "/api/auth/change-password",
            headers=auth_headers(old_token),
            json={"old_password": "secret123", "new_password": "Brand-New-6x!"},
        )
        assert changed.status_code == 200, changed.text

        # 旧 token 立刻失效
        assert _me(client, old_token).status_code == 401
        # 响应里带的新 token 直接可用 —— 客户端不必再登录一次
        assert _me(client, changed.json()["token"]).status_code == 200
        # 重新登录同样可以
        relogin = client.post(
            "/api/auth/login", json={"account": "alice", "password": "Brand-New-6x!"}
        )
        assert relogin.status_code == 200
        assert _me(client, relogin.json()["token"]).status_code == 200

    def test_only_that_users_tokens_die(self, client, alice, bob):
        """改密只失效**本人**的 token —— 别把别人一起踢下线。"""
        client.post(
            "/api/auth/change-password",
            headers=auth_headers(alice["token"]),
            json={"old_password": "secret123", "new_password": "Brand-New-6x!"},
        )

        assert _me(client, bob["token"]).status_code == 200

    def test_admin_reset_password_also_kills_tokens(self, client, alice, admin_h):
        """管理员重置口令同样要踢掉旧 token —— 那条路径的存在理由就是"凭据可能泄露"。"""
        assert _me(client, alice["token"]).status_code == 200

        reset = client.post(
            f"/api/admin/users/{alice['user']['id']}/reset-password",
            json={},
            headers=admin_h,
        )
        assert reset.status_code == 200, reset.text

        assert _me(client, alice["token"]).status_code == 401
        assert client.post(
            "/api/auth/login",
            json={"account": "alice", "password": reset.json()["password"]},
        ).status_code == 200

    def test_admin_reset_does_not_kill_the_admins_own_token(self, client, admin_h):
        """管理员重置**别人**的口令，不该把自己踢下线。"""
        alice = register_and_login(client, "alice")

        client.post(
            f"/api/admin/users/{alice['user']['id']}/reset-password", json={}, headers=admin_h
        )

        assert client.get("/api/auth/me", headers=admin_h).status_code == 200


class TestFullChainDoesNotStrandTheUser:
    """点名要覆盖的链路：建号 → 一次性口令 → 首登被闸门拦 → 改密 → 闸门解除。"""

    def test_admin_created_user_can_get_all_the_way_in(self, client, admin_h):
        created = client.post(
            "/api/admin/users",
            json={"username": "newbie", "email": "newbie@example.com", "role": "user"},
            headers=admin_h,
        ).json()
        one_time = created["password"]

        # 1. 用一次性口令登录
        login = client.post(
            "/api/auth/login", json={"account": "newbie", "password": one_time}
        )
        assert login.status_code == 200, login.text
        assert login.json()["user"]["must_change_password"] is True
        first_token = login.json()["token"]

        # 2. 闸门拦住业务接口，但放行 /me 与改密
        assert _me(client, first_token).status_code == 200
        blocked = client.get("/api/sessions", headers=auth_headers(first_token))
        assert blocked.status_code == 403
        assert "修改密码" in blocked.json()["detail"]

        # 3. 改密：拿回一个新 token
        changed = client.post(
            "/api/auth/change-password",
            headers=auth_headers(first_token),
            json={"old_password": one_time, "new_password": "Fresh-Newbie-7z!"},
        )
        assert changed.status_code == 200, changed.text

        # 4. 改密用的那个 token 已失效（版本号变了）
        assert _me(client, first_token).status_code == 401
        # 5. 响应里的新 token 直接可用，且闸门已解除
        second_token = changed.json()["token"]
        me = _me(client, second_token)
        assert me.status_code == 200
        assert me.json()["must_change_password"] is False
        assert client.get(
            "/api/sessions", headers=auth_headers(second_token)
        ).status_code == 200

    def test_gated_user_can_always_reach_the_change_password_endpoint(self, client, admin_h):
        """最怕的失败模式：闸门把人关在里面、连改密都调不了。

        这条同时断言两件事 —— 业务接口被拦住（安全），改密接口能调到（可用性）。
        缺任何一条都会把人卡死。
        """
        one_time = client.post(
            "/api/admin/users",
            json={"username": "gated", "email": "gated@example.com", "role": "user"},
            headers=admin_h,
        ).json()["password"]
        token = client.post(
            "/api/auth/login", json={"account": "gated", "password": one_time}
        ).json()["token"]

        assert client.get("/api/sessions", headers=auth_headers(token)).status_code == 403
        assert client.post(
            "/api/auth/change-password",
            headers=auth_headers(token),
            json={"old_password": one_time, "new_password": "Fresh-Gated-7z!"},
        ).status_code == 200


class TestMigrationAddsTheColumn:
    def test_legacy_db_gets_token_version(self, tmp_path):
        """老库没有 token_version 列，升级后必须自动补齐（否则查询直接报错）。"""
        import sqlite3

        from src.auth import db as auth_db

        db_file = tmp_path / "legacy-token.db"
        conn = sqlite3.connect(db_file)
        conn.executescript(
            "CREATE TABLE users (id INTEGER PRIMARY KEY, username VARCHAR(64),"
            " email VARCHAR(255), password_hash VARCHAR(255), role VARCHAR(16),"
            " is_active BOOLEAN, must_change_password BOOLEAN, created_at DATETIME);"
        )
        conn.commit()
        conn.close()

        auth_db.init_db(db_file)
        try:
            conn = sqlite3.connect(db_file)
            columns = {row[1] for row in conn.execute("PRAGMA table_info(users)")}
            conn.close()

            assert "token_version" in columns
            assert "must_change_password" in columns
        finally:
            auth_db.dispose_engine()

    def test_migration_is_idempotent(self, tmp_path):
        from src.auth import db as auth_db

        db_file = tmp_path / "twice-token.db"
        auth_db.init_db(db_file)
        auth_db.init_db(db_file)  # 不該报 duplicate column
        auth_db.dispose_engine()
