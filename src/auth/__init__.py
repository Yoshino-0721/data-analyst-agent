"""用户系统：注册 / 登录（JWT）、角色权限与 SQLite 持久化。

包结构（项目一 rag-knowledge-base 中的同名包与本包保持同构，属有意复制而非
共享依赖，保证两个项目各自可独立部署）：

- ``models``   ORM 模型（users / sessions / messages）
- ``db``       引擎与会话工厂、建表与默认管理员引导
- ``security`` bcrypt 口令哈希 + PyJWT 签发校验
- ``deps``     FastAPI 依赖（get_db / get_current_user / require_admin）
- ``api``      ``/api/auth/*`` 路由
"""
