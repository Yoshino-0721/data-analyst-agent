"""共享测试夹具。"""

from __future__ import annotations

import sys

import pytest

from src.config import settings


@pytest.fixture(autouse=True)
def fast_bcrypt(monkeypatch):
    """测试期把 bcrypt 成本从默认 12 轮降到 4 轮。

    每个用例的 autouse ``isolated_auth_db`` 都要建一次全新库，而建库会引导
    默认管理员 —— 一次 12 轮 bcrypt 约 0.25s，累计一百多秒。降到 4 轮后
    「口令哈希」这件事依然真的在跑（盐、格式、校验路径全都覆盖），只是成本下来了。

    **生产代码一行不改**：只把各模块里 ``from src.auth.security import
    hash_password`` 进来的绑定换成便宜的版本。这类绑定在导入时就固定了，
    所以必须逐模块替换 —— 光换 ``src.auth.security`` 自己不管用
    （``api.py`` / ``db.py`` / ``admin_api.py`` 各有各的那份）。
    """
    import bcrypt

    from src.auth import security as security_module

    original = security_module.hash_password

    def cheap_hash(password: str) -> str:
        return bcrypt.hashpw(
            password.encode("utf-8"), bcrypt.gensalt(rounds=4)
        ).decode("ascii")

    monkeypatch.setattr(security_module, "hash_password", cheap_hash)
    for module in list(sys.modules.values()):
        if getattr(module, "hash_password", None) is original:
            monkeypatch.setattr(module, "hash_password", cheap_hash)
    yield


# 测试期统一使用的管理员口令（理由同 rag-knowledge-base 的同名常量）：
# 满足强口令要求，且由 ADMIN_PASSWORD **显式提供** —— 引导出来的管理员
# `must_change_password=False`，不会被"必须先改密"的硬闸门拦住，否则所有
# 调用管理接口的既有用例都会 403。强制改密那条链路由
# tests/test_auth_security.py 用"不设 ADMIN_PASSWORD"专门覆盖。
TEST_ADMIN_PASSWORD = "Unit-Test-Admin-9x!"


@pytest.fixture(autouse=True)
def open_registration_and_clear_throttle(monkeypatch):
    """测试期放开自助注册、并清空登录节流状态。

    这两条默认值（注册默认关闭、登录失败到阈值锁定）都会让**既有用例互相干扰**：
    注册会直接 403；某个用例故意试错几次，后面的用例就会被锁在 429 上。
    针对这两条本身的用例在 tests/test_auth_throttle.py 里把它们单独调回来 ——
    默认值仍保持"关闭 / 开启节流"，测试期只是显式地打开它。
    """
    from src.auth import throttle

    monkeypatch.setattr(settings, "allow_registration", True)
    throttle.reset()
    yield
    throttle.reset()


@pytest.fixture(autouse=True)
def isolated_auth_db(tmp_path_factory, monkeypatch, fast_bcrypt):
    """每个用例独享一个全新 SQLite，绝不让测试碰到真实的 storage/app.db。

    同时固定 JWT_SECRET，避免签发密钥落盘到真实 storage/ 目录。
    目录取自 tmp_path_factory，与用例自己的 tmp_path 互不干扰。
    显式依赖 fast_bcrypt：引导管理员要哈希口令，顺序反了就白降成本了。
    """
    from src.auth import db as auth_db

    monkeypatch.setenv("JWT_SECRET", "unit-test-secret")
    monkeypatch.setenv("ADMIN_PASSWORD", TEST_ADMIN_PASSWORD)
    auth_db.init_db(tmp_path_factory.mktemp("authdb") / "auth.db")
    yield
    auth_db.dispose_engine()


@pytest.fixture(autouse=True)
def isolated_storage_root(tmp_path, monkeypatch):
    """把所有落盘根目录指向本用例的 tmp —— 任何用例都不许碰真实 storage/。

    指向 ``settings``（全局单例）而不是 ``server.settings``：server 现在就是
    ``from .config import settings``，一次 patch 全仓生效。``user_root()``
    每次现读 ``settings.storage_root``，所以按用户建工作区也会落进 tmp。
    """
    monkeypatch.setattr(settings, "storage_root", tmp_path / "storage")
    yield


@pytest.fixture(autouse=True)
def clean_workspace_registry():
    """每个用例开始时清空按用户的工作区注册表。

    注册表是模块级全局 dict，不归零的话上个用例留下的 ``Session``（还攥着
    已删文件的内存态、以及旧的 tmp 路径）会被下个用例复用。
    """
    from src import workspaces

    workspaces.reset_registry()
    yield
    workspaces.reset_registry()


@pytest.fixture()
def client():
    """带独立认证库的 TestClient。"""
    from fastapi.testclient import TestClient

    from src.server import app

    return TestClient(app)


def auth_headers(token: str) -> dict:
    return {"Authorization": f"Bearer {token}"}


def approve_user(user_id: int) -> None:
    """把自助注册出来的账号置为「已审核」——等价于管理员在后台点了「通过」。

    直接改库，不走 PATCH：审核端点本身由
    ``tests/test_registration_approval.py`` 用真实接口端到端覆盖；
    这里是给几十个既有用例提供"可直接登录的用户"的快捷方式，
    免得每个用例都得先登一次管理员。
    """
    from src.auth import db as auth_db
    from src.auth.models import STATUS_ACTIVE, User

    with auth_db.get_session_factory()() as session:
        user = session.get(User, user_id)
        assert user is not None, f"要审核的用户 {user_id} 不存在"
        user.status = STATUS_ACTIVE
        user.is_active = True
        session.commit()


def register_and_login(client, username="alice", email=None, password="secret123") -> dict:
    """注册 → 管理员审核通过 → 登录，返回 {token, user}。

    自助注册出来的是 **pending**（连 token 都不发），所以这里显式补上审核这一步；
    「注册后被挡在门外」与「审核通过后能正常用」两条路径本身由
    ``tests/test_registration_approval.py`` 覆盖。
    """
    registered = client.post(
        "/api/auth/register",
        json={
            "username": username,
            "email": email or f"{username}@example.com",
            "password": password,
        },
    )
    assert registered.status_code == 200, registered.text
    body = registered.json()
    assert body.get("pending") is True, "自助注册应当落成待审核"
    assert "token" not in body, "待审核的账号不该拿到 token"

    approve_user(body["user"]["id"])

    response = client.post(
        "/api/auth/login", json={"account": username, "password": password}
    )
    assert response.status_code == 200, response.text
    return response.json()


def admin_headers(client) -> dict:
    """引导创建的管理员登录后的请求头（口令见 TEST_ADMIN_PASSWORD）。"""
    response = client.post(
        "/api/auth/login",
        json={"account": "admin", "password": TEST_ADMIN_PASSWORD},
    )
    assert response.status_code == 200, response.text
    return auth_headers(response.json()["token"])


# ---------------------------------------------------------------- 常用账号夹具
#
# 多用户接口的用例基本都需要「两个普通用户 + 一个管理员」，统一提到这里
# 避免每个测试文件各写一份。普通用户现注册现登录，管理员是引导创建的那个。


@pytest.fixture()
def alice(client) -> dict:
    return register_and_login(client, "alice")


@pytest.fixture()
def alice_headers(alice) -> dict:
    return auth_headers(alice["token"])


@pytest.fixture()
def bob(client) -> dict:
    return register_and_login(client, "bob")


@pytest.fixture()
def bob_headers(bob) -> dict:
    return auth_headers(bob["token"])


@pytest.fixture()
def admin_h(client) -> dict:
    return admin_headers(client)


@pytest.fixture()
def authed_client(client, alice) -> tuple:
    """(已注册 alice 并带 token 的 client, alice 的请求头)。"""
    return client, auth_headers(alice["token"])


# ---------------------------------------------------------------- 带 token 的客户端


class AuthedClient:
    """把 token 自动塞进每个请求的 TestClient 包装。

    用例主体的写法与多用户改造前完全一致（``api.post(...).json()``），
    差别只在「每个请求都带上了这个用户的 Authorization 头」。
    """

    def __init__(self, client, token: str, user: dict) -> None:
        self.client = client
        self.token = token
        self.user = user
        self.headers = auth_headers(token)

    def _headers(self, extra: dict | None) -> dict:
        merged = dict(self.headers)
        merged.update(extra or {})
        return merged

    def get(self, url, **kwargs):
        return self.client.get(url, headers=self._headers(kwargs.pop("headers", None)), **kwargs)

    def post(self, url, **kwargs):
        return self.client.post(url, headers=self._headers(kwargs.pop("headers", None)), **kwargs)

    def patch(self, url, **kwargs):
        return self.client.patch(url, headers=self._headers(kwargs.pop("headers", None)), **kwargs)

    def delete(self, url, **kwargs):
        return self.client.delete(url, headers=self._headers(kwargs.pop("headers", None)), **kwargs)

    @property
    def workspace(self):
        """该用户在内存里的工作区（用例要往产物目录里塞文件时用）。"""
        from src import workspaces

        return workspaces.get_workspace(self.user["id"])


@pytest.fixture()
def api(client, alice) -> AuthedClient:
    """已注册 alice 并自动带 token 的客户端。"""
    return AuthedClient(client, alice["token"], alice["user"])


@pytest.fixture()
def bob_api(client, bob) -> AuthedClient:
    """已注册 bob 并自动带 token 的客户端。"""
    return AuthedClient(client, bob["token"], bob["user"])


@pytest.fixture()
def admin_api(client, admin_h) -> AuthedClient:
    """默认管理员并自动带 token 的客户端。"""
    me = client.get("/api/auth/me", headers=admin_h)
    assert me.status_code == 200, me.text
    return AuthedClient(client, admin_h["Authorization"].split(" ", 1)[1], me.json())


def upload_csv(api: AuthedClient, name: str, content: str = "a,b\n1,2\n") -> dict:
    """往该用户的工作区传一份小 CSV，返回 /api/upload 的响应体。"""
    response = api.post(
        "/api/upload", files=[("files", (name, content.encode("utf-8"), "text/csv"))]
    )
    assert response.status_code == 200, response.text
    return response.json()
