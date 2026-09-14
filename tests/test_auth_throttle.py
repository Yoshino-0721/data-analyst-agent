"""注册闸门与登录节流。

两条都是为了堵"花钱"与"撞库"这两个真实口子（见 C2 复核清单 1.1 / 1.2）：

- **自助注册默认关闭**：公网部署下，任何人注册成功都会消耗**站点共用**的 API Key
  额度（上传文档要 embedding、提问要 chat）；
- **登录失败到阈值即短暂锁定**：管理员账号被撞开就是全站数据。

注意 conftest 里的 autouse 夹具 `open_registration_and_clear_throttle` 默认把注册
放开、并清空节流状态，好让既有用例不互相干扰；本文件把它们**单独调回默认值**来测 ——
也就是说这里验的是"出厂默认行为"，不是"测试期被放宽的行为"。
"""

from __future__ import annotations

import time

from src.config import settings
from tests.conftest import TEST_ADMIN_PASSWORD, register_and_login

NEWBIE = {"username": "newbie", "email": "newbie@example.com", "password": "secret123"}


def login(client, account="admin", password="definitely-wrong"):
    return client.post("/api/auth/login", json={"account": account, "password": password})


class TestRegistrationGate:
    def test_closed_by_default(self, client, monkeypatch):
        monkeypatch.setattr(settings, "allow_registration", False)

        response = client.post("/api/auth/register", json=NEWBIE)

        assert response.status_code == 403, response.text
        assert "关闭自助注册" in response.json()["detail"]

    def test_closed_really_does_not_create_the_account(self, client, monkeypatch):
        """不能只是"返回 403" —— 账号必须真的没被建出来。"""
        monkeypatch.setattr(settings, "allow_registration", False)
        client.post("/api/auth/register", json=NEWBIE)

        monkeypatch.setattr(settings, "allow_registration", True)
        # 放开后同名注册应当成功：说明上一步确实没建号
        assert client.post("/api/auth/register", json=NEWBIE).status_code == 200

    def test_enabled_allows_registration(self, client, monkeypatch):
        monkeypatch.setattr(settings, "allow_registration", True)
        assert client.post("/api/auth/register", json=NEWBIE).status_code == 200


class TestLoginThrottle:
    def test_locks_on_the_threshold_attempt(self, client, monkeypatch):
        monkeypatch.setattr(settings, "login_max_failures", 3)
        monkeypatch.setattr(settings, "login_lock_seconds", 60)

        assert login(client).status_code == 401
        assert login(client).status_code == 401

        locked = login(client)          # 第 3 次失败即触发锁定
        assert locked.status_code == 429, locked.text
        assert "秒后再试" in locked.json()["detail"]
        assert int(locked.headers["Retry-After"]) > 0

        # 锁定期内继续尝试仍然 429
        assert login(client).status_code == 429

    def test_correct_password_is_also_rejected_while_locked(self, client, monkeypatch):
        """锁定期内连正确口令也拒绝 —— 否则攻击者一旦猜对就能立刻拿到 token。"""
        monkeypatch.setattr(settings, "login_max_failures", 1)
        monkeypatch.setattr(settings, "login_lock_seconds", 60)

        assert login(client).status_code == 429
        assert login(client, password=TEST_ADMIN_PASSWORD).status_code == 429

    def test_success_clears_the_counter(self, client, monkeypatch):
        monkeypatch.setattr(settings, "login_max_failures", 3)
        monkeypatch.setattr(settings, "login_lock_seconds", 60)

        assert login(client).status_code == 401
        assert login(client, password=TEST_ADMIN_PASSWORD).status_code == 200
        # 计数已清零：再错两次仍只是 401，不会因为"之前那次"提前锁
        assert login(client).status_code == 401
        assert login(client).status_code == 401

    def test_counter_ignores_case_and_surrounding_spaces(self, client, monkeypatch):
        """`Admin` / ` admin ` 必须共用一个计数器，否则换个大小写就绕过节流。"""
        monkeypatch.setattr(settings, "login_max_failures", 2)
        monkeypatch.setattr(settings, "login_lock_seconds", 60)

        assert login(client, account="Admin").status_code == 401
        assert login(client, account="  admin  ").status_code == 429

    def test_disabled_account_with_correct_password_never_locks(
        self, client, monkeypatch, admin_h
    ):
        """口令正确、只是账号被禁用 —— 这不是撞库，不该累计失败次数。

        否则管理员一禁用某人，对方拿旧口令猛试就能把自己锁上，
        把"封禁"变成"被封锁"（状态码也从 403 变成 429，排查时很迷惑）。
        """
        carol = register_and_login(client, "carol")
        monkeypatch.setattr(settings, "login_max_failures", 2)
        monkeypatch.setattr(settings, "login_lock_seconds", 60)
        client.patch(
            f"/api/admin/users/{carol['user']['id']}",
            json={"is_active": False},
            headers=admin_h,
        )

        for _ in range(4):
            response = client.post(
                "/api/auth/login", json={"account": "carol", "password": "secret123"}
            )
            assert response.status_code == 403, response.text

    def test_lock_expires(self, client, monkeypatch):
        """锁定期满后自动解锁（时长压到 1 秒，免得测试等 5 分钟）。"""
        monkeypatch.setattr(settings, "login_max_failures", 1)
        monkeypatch.setattr(settings, "login_lock_seconds", 1)

        assert login(client).status_code == 429
        time.sleep(1.1)
        assert login(client, password=TEST_ADMIN_PASSWORD).status_code == 200

    def test_response_does_not_leak_remaining_attempts(self, client, monkeypatch):
        """401 文案里不能出现"还剩几次" —— 那等于告诉攻击者猜对了多少。"""
        monkeypatch.setattr(settings, "login_max_failures", 3)

        detail = login(client).json()["detail"]

        assert detail == "用户名或密码不正确"
        assert "剩余" not in detail
