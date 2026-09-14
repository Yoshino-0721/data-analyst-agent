# 私人数据分析师 Agent

![Python](https://img.shields.io/badge/Python-3.13-3776AB?logo=python&logoColor=white)
![Docker](https://img.shields.io/badge/沙箱-Docker%20隔离-2496ED?logo=docker&logoColor=white)
![Tests](https://img.shields.io/badge/tests-565%20passed-brightgreen)

基于 Function Calling 的本地数据分析 Agent：上传 Excel / CSV，用自然语言提问，
模型自己写代码、放进 Docker 沙箱跑、看到报错自己改，直到算出结果并出图。
多用户共用一套服务：**每个人的数据集、产物、会话记录互相看不见**。

**核心在后端工程：执行隔离、异常分类、成本控制。** 不是一个 UI 套壳。

## ⚠️ 部署前必读（安全）

**本地执行器无 OS 级隔离，仅限本地自用或可信团队使用；生产环境必须配置 EXECUTOR=docker。**

原因很直接：`LocalSubprocessExecutor` 只是本机调试工具，模型生成的代码**以当前用户权限
运行**（可读全盘、可出网）；`AGENTS.md` §2.3 也明确它是「仅本地调试、生产禁用」的备选实现。
本机 `.env` 里为了开发体验设了 `EXECUTOR=local`，但**代码里的默认实现始终是
`DockerExecutor`** —— 别把 `.env` 的临时选择当成架构默认值。

要拿到真正的隔离边界（`--read-only`、`--network=none`、最小只读挂载、CPU/内存/PID 限额），
生产部署必须用默认的 `EXECUTOR=docker`。另外 `APP_ENV != dev` 时 `local` 执行器会**拒绝构造**，
服务同时强制要求外部注入 `JWT_SECRET`（缺了拒绝启动）。隔离设计与威胁模型见
`docs/sandbox-threat-model.md` 与 `docs/executor-interface.md`。

## 效果演示

下面这张是**真实运行截图**（真模型 + 真前端；本机未启用容器隔离，
因此这一轮用**本地调试执行器**跑，Docker 沙箱仍是默认实现）：

![界面效果](docs/demo-ui.png)

左边是对话，右边是**执行轨迹**。轨迹区把这个 Agent 的底牌全摊开了：

- **模型写的每一段 Python 代码原样展示**（带语法高亮和一键复制）——
  这是用户信任的来源，也是这个项目最该被看见的部分；
- 每一轮的**执行状态徽章**：`OK` 绿、`RUNTIME_ERROR` 红、
  `TIMEOUT`/`OOM` 橙、`REJECTED` 灰，配 `stdout` 与清洗后的 traceback 折叠面板；
- 截图这一轮里出现了一次 **`RUNTIME_ERROR`**：模型第一次按容器路径读数据
  没读到，从**清洗过的 traceback** 里看出真实原因、自己改对并继续 ——
  异常分类 + 自纠闭环的价值就在这一条上；
- 产出的图表直接嵌在轨迹里。

上面这张图是**产物本身**（模型自己写的 pandas + matplotlib 跑出来的）：

![产物示例](docs/demo-chart.png)

自己跑一遍：

```bash
# 1. 装依赖（pandas / openpyxl / fastapi / uvicorn / sqlalchemy / bcrypt / pyjwt 等）
pip install -e .

# 2. 启动服务（本机示例用 8123，避免和其它本地服务撞端口）
python -m uvicorn src.server:app --port 8123

# 3. 打开 http://127.0.0.1:8123 → 跳到登录页
#    管理员 admin：口令是你设的 ADMIN_PASSWORD，或启动日志横幅里的随机口令
#    自助注册默认关闭；开通成员需设 ALLOW_REGISTRATION=true 并重启

# 或者不起服务，直接命令行端到端跑一次：
python scripts/demo_agent.py
```

## 当前进度

| 模块 | 状态 | 位置 |
|---|---|---|
| 威胁模型与执行流程图 | ✅ | `docs/sandbox-threat-model.md` |
| Executor 接口契约 | ✅ | `docs/executor-interface.md` |
| 契约层（六分类 / 结构体 / 协议） | ✅ | `src/sandbox/executor.py` |
| 判定与净化逻辑（纯函数） | ✅ | `src/sandbox/analysis.py` |
| Docker 沙箱执行器 | ✅ | `src/sandbox/docker_executor.py` |
| 本地调试执行器 | ✅ | `src/sandbox/local_executor.py` |
| 策略工厂 | ✅ | `src/sandbox/factory.py` |
| 沙箱镜像 | ✅ | `src/sandbox/image/Dockerfile` |
| 真实容器集成测试（`-m docker`） | ✅ 17 项 | `tests/test_docker_integration.py` |
| Schema 提取与成本控制 | ✅ | `src/schema/extractor.py` |
| 手写 Function Calling 循环 | ✅ | `src/agent/` |
| 用户系统（bcrypt + JWT + SQLite） | ✅ | `src/auth/` |
| 多用户工作区隔离 | ✅ | `src/workspaces.py` |
| 会话 / 消息 / 执行轨迹持久化 | ✅ | `src/serializers.py`、`src/auth/models.py` |
| 管理员后台（API + 页面） | ✅ | `src/admin_api.py`、`web/admin.html` |
| 前端（登录 / 工作台 / 轨迹区） | ✅ | `web/login.html`、`web/index.html` |

## 多用户与权限

### 角色与权限矩阵

| 能力 | 普通用户 `user` | 管理员 `admin` |
|---|---|---|
| 上传数据集 / 提问 / 出图 | ✅ 仅限自己的数据 | ✅ 仅限自己的数据 |
| 会话与执行轨迹 | ✅ 仅限自己的 | ✅ 自己的 + **可查看任意用户的完整轨迹** |
| 数据集列表与删除 | ✅ 仅限自己的 | ✅ 全站数据集 |
| 产物 `/api/artifact/*` | ✅ 仅限自己工作区内的产物 | ✅ 同左（管理员也不能跨用户取产物文件） |
| `/api/admin/*` | ❌ 403 | ✅ |
| 系统操作（清缓存 / 健康状态） | ❌ 403 | ✅ |

两条刻意的设计：

- **越权返回 404 而不是 403**。「这个资源不属于你」和「这个资源不存在」必须长得一样，
  否则攻击者能靠状态码枚举出别人的会话 id 与数据集 id。
- **管理员只读历史，不获得执行入口**。后台能看任意用户的提问与完整执行轨迹，
  但**没有任何「以某用户身份跑一段代码」的入口** —— 管理能力不该顺带变成执行能力。
- **权限闸门只在服务端**。前端隐藏入口只是别让人误点，`require_admin` 才是真闸门。

### 默认管理员

全新部署（`users` 表为空）时，服务启动会自动引导一个管理员账号 `admin`。
**这里没有"默认口令"这种东西** —— 一个全站可见的固定口令，等于把"部署到公网后
忘记改"变成必然事件。口令只有两条来源：

| 情况 | 行为 |
|---|---|
| 不设 `ADMIN_PASSWORD`（默认） | 系统**生成随机强口令**，在启动日志里用醒目横幅打印**一次**；该账号 `must_change_password=True`，改密前除 `/api/auth/me` 与改密接口外**一律 403**，且新口令必须够强（防止一次改成 `123456` 绕过整套策略） |
| 设了 `ADMIN_PASSWORD` | 直接用它，不强制改密。但**弱口令会让服务拒绝启动**：要求 ≥12 位、至少 3 类字符（大小写/数字/符号），且不在常见弱口令表里（`admin123` 会被明确拒绝并打印生成命令） |

`ADMIN_USERNAME` / `ADMIN_EMAIL` 可改引导出来的用户名与邮箱。
引导是幂等的：表里已有用户就不会重复创建。

> 拿到随机口令后，除了在网页上改，也可以直接改：
> ```bash
> curl -X POST http://127.0.0.1:8123/api/auth/change-password \
>   -H "Content-Type: application/json" \
>   -H "Authorization: Bearer <登录接口返回的 token>" \
>   -d '{"old_password":"<横幅里的口令>","new_password":"<新的强口令>"}'
> ```

### 认证方式

除 `GET /api/health` 与三个页面（`/`、`/login`、`/admin`）外，**所有接口都需要登录态**：

```
Authorization: Bearer <token>
```

token 是 HS256 签名的 JWT（payload 含 `sub` / `username` / `role` / `exp`），
由 `/api/auth/login` 或 `/api/auth/register` 下发，前端存在 `localStorage.auth_token`。
失效或被禁用时返回 401 / 403，前端自动清 token 并跳回登录页。

### 数据隔离是怎么做到的

| 面 | 做法 |
|---|---|
| 数据集文件 | 每个用户一个工作区目录 `storage/session/users/<uid>/data/`，文件名一律 `safe_target_name()` 归一化 |
| 产物 | 每用户独立 `artifacts/`，`Session.artifact_path()` 归一化 + 校验落点，穿越一律返回 None |
| 内存状态 | `src/workspaces.py` 维护「一用户一 `Session` 实例」的注册表，互不共享 |
| 数据库 | `datasets` / `sessions` / `messages` 全部带 `user_id` / 归属校验，越权 404 |
| 沙箱挂载 | 每次**只挂本次实际用到的文件**（`data_files`），绝不挂整个上传目录 |

> ⚠️ **必须诚实说明的一点**：数据隔离是**应用层**做的，不是操作系统层。
> 尤其在本机用 `EXECUTOR=local` 调试时，模型生成的代码以当前用户权限运行、**可读整个磁盘**
> （见 `AGENTS.md` §2.5 与 `docs/sandbox-threat-model.md`）。也就是说 `local` 模式下
> **隔离只能防"用错数据"，防不住"故意越权读别人的文件"**。要拿到真正的隔离边界，
> 必须用默认的 Docker 执行器：`--read-only` + `--network=none` + 最小只读挂载。
> **生产环境不要用 `local`。**

> 另一条已知限制：多用户改造沿用既有的手写 FC 循环签名，**每轮提问只把当前问题交给模型**，
> 不注入同一会话里此前的问答。所以「会话」目前承担的是**归组与留痕**（以及管理员审计），
> 不是跨轮上下文记忆 —— 追问需要把条件说全。把历史接进循环列入后续规划。

## 为什么错误分类是这个项目的地基

闭环是「生成代码 → 执行 → 观察 → 修正」。调用者是模型，它**拿到什么信号
就会往什么方向改**：

```
同一段糟糕的代码 ──▶ 回传 "执行失败"        → 模型只能瞎试，改不到点上
                 ──▶ 回传 TIMEOUT + hint  → 模型去缩减数据量
                 ──▶ 回传 OOM + hint      → 模型去分块读取
                 ──▶ 回传 RUNTIME_ERROR   → 模型去核对列名
```

所以 `ExecStatus` 有六个值，而压不成一句「执行失败」：

| 分类 | 触发条件 | 给模型的信号 |
|---|---|---|
| `OK` | 退出码 0 | — |
| `TIMEOUT` | 宿主墙钟超时，**由外部计时器判定** | 代价太高，去优化数据量 |
| `OOM` | 退出码 137（非超时），或 stderr 出现内存类报错 | 别一次性全读进内存 |
| `RUNTIME_ERROR` | 非零退出 / traceback | 去改代码（附清洗后的栈） |
| `REJECTED` | 静态预检拦下，**没起容器** | 明确告知哪种写法不行 |
| `SANDBOX_ERROR` | 镜像缺失 / daemon 不可达 | 不回填给模型（它改不了） |

**执行轨迹会随消息落库**：每一轮的代码、状态、stdout/stderr、hint 与产物文件名
都存在 assistant 消息的 `meta` 里，所以刷新页面、换设备、甚至管理员事后审计，
都能把当时的完整过程重新渲染出来 —— 而不是只留一句结论。

## 隔离做了什么

六件事叠起来，缺一个防护面就破：

```
--read-only              根文件系统只读，只有 /out 可写
--network=none           没有任何出网口，掐灭数据外泄
--cpus / --memory        memory-swap 同值禁 swap，超了就杀而不是拖慢机器
--pids-limit=64          防 fork 炸弹打满宿主进程表
--user + no-new-privileges   非 root，无提权空间
最小权限挂载             只挂本次实际用到的文件副本，只读
```

超时是这个实现最容易写错的一环：**计时必须在容器外部** —— 被执行的代码
不可信，它完全可以捕获 `signal.alarm` 或者干脆不响应用户态信号。而且超时
之后还要把已产生的输出读回来，那是排查「为什么会卡死」的唯一线索。

## 快速验证

```bash
# 运行测试（565 项，全部用桩对象，不需要 Docker；Docker 集成测试默认跳过）
pytest

# 只跑真实容器集成测试（需要 Docker daemon 与沙箱镜像）
pytest -m docker

# 端到端闭环演示：写错 → 结构化反馈 → 改好
python scripts/demo_loop.py

# 装了 Docker 之后：一条命令验证沙箱是否真的可用
python scripts/check_docker.py           # 检查 + 跑隔离探针
python scripts/check_docker.py --build   # 顺手构建沙箱镜像
```

`check_docker.py` 会依次验证 CLI、daemon、镜像，然后真跑几个容器确认
**隔离是否真的生效** —— 注意隔离探针是「**失败**才算对」：
能写出文件、能联网，说明防护没生效。

演示脚本会依次模拟三种情况并打印**回填给模型的完整 payload**：

1. 列名猜错 → `RUNTIME_ERROR` + 清洗后的 traceback
2. 低效实现 → `TIMEOUT`，但 stdout 仍捞回了被杀前打印的内容
3. 修正后 → `OK` + 产物 `summary.txt`

## 部署

### 推荐：宿主直接部署（venv + systemd）

**这是本项目推荐的部署方式**，原因是架构性的：Agent 需要创建**沙箱容器**，也就是要驱动
宿主的 Docker daemon。应用跑在宿主上时，它拿到的路径天然就是宿主路径，不需要任何映射；
一旦把应用塞进容器，就会同时引入下面「备选」里那两个问题。

```bash
# 1. 取代码 + 建虚拟环境（需要 Python >= 3.13）
git clone <repo> /opt/data-analyst-agent
cd /opt/data-analyst-agent
python3.13 -m venv .venv
.venv/bin/pip install -e .

# 2. 构建沙箱镜像（在宿主上，只需一次；更新沙箱依赖时重建）
docker build -t data-analyst-sandbox:latest src/sandbox/image

# 3. 生产配置
cp .env.example .env
#   ZHIPUAI_API_KEY=...      必填
#   APP_ENV=production       必填：非 dev 才会强制 JWT_SECRET、并拒绝构造 local 执行器
#   EXECUTOR=docker          必填：local 无 OS 级隔离，生产禁用
#   JWT_SECRET=...           必填（生产缺它服务直接拒绝启动）
#       生成：python -c "import secrets; print(secrets.token_urlsafe(48))"
#   ADMIN_PASSWORD=...       建议设置；留空则生成随机强口令并强制首次登录改密
chmod 600 .env

# 4. 先手工起一次，确认能登录、能上传数据出图
.venv/bin/python -m uvicorn src.server:app --host 127.0.0.1 --port 8123
```

`/etc/systemd/system/data-analyst-agent.service`：

```ini
[Unit]
Description=data-analyst-agent
After=network-online.target docker.service
Wants=docker.service

[Service]
Type=simple
User=analyst
WorkingDirectory=/opt/data-analyst-agent
EnvironmentFile=/opt/data-analyst-agent/.env
ExecStart=/opt/data-analyst-agent/.venv/bin/python -m uvicorn src.server:app --host 127.0.0.1 --port 8123
Restart=on-failure
RestartSec=3

# 加固：服务只需要写自己的 storage/，不需要别的特权
NoNewPrivileges=true
PrivateTmp=true
ProtectSystem=full
ProtectHome=read-only
ReadWritePaths=/opt/data-analyst-agent/storage

[Install]
WantedBy=multi-user.target
```

```bash
sudo systemctl daemon-reload
sudo systemctl enable --now data-analyst-agent
journalctl -u data-analyst-agent -f     # 未设 ADMIN_PASSWORD 时，初始口令从这里取
```

两个容易踩的点：

- **`User=analyst` 必须在 `docker` 组里**，否则创建沙箱容器会 Permission denied：
  `sudo usermod -aG docker analyst`（改完要重新登录 / `systemctl restart`）。
- **公网必须上 HTTPS**。登录态是 `Authorization: Bearer <token>`，明文 HTTP 下
  token 可被中间人截获，等同于账号泄露。反向代理把 `8123` 暴露出去即可，
  但别忘了设 `client_max_body_size`（否则上传大 CSV 时 Nginx 先返 413）。

### 备选：容器部署（`docker-compose.yml`）

`docker-compose.yml` 与 `Dockerfile` 已准备好，并把三个安全默认值固化下来：
`APP_ENV=production`、`EXECUTOR=docker`、`JWT_SECRET` 必填（compose 的 `environment`
优先级高于 `env_file`，所以 `.env` 里写 `dev` / `local` 也压不住；缺 `JWT_SECRET`
会直接中止启动，而不是偷偷随机生成一份）。

**但它有两个必须接受的前提，请读完再决定：**

1. **必须把宿主的 `/var/run/docker.sock` 挂进应用容器**（应用要创建兄弟容器）。
   拿到 socket 等于拿到宿主 root —— 这个容器一旦被攻破，宿主就被完全接管
   （容器里的进程可以 `docker run --privileged -v /:/host` 自我提权）。
   所以 Dockerfile 里**刻意没有**降权到非 root：在这里那是**假安全**，
   只会引入 bind mount 的 uid/GID 摩擦，把失败伪装成莫名其妙的 Permission denied。
2. **存储目录在宿主与容器内必须位于完全相同的绝对路径**（由 compose 的
   `APP_STORAGE_PATH` 保证）。`DockerExecutor` 传给 docker 的挂载参数是**宿主路径**，
   两边不一致时宿主 daemon 会挂一个**空目录**进沙箱 —— 模型看到空 `/data`，
   报一个和真实原因完全无关的错。

⚠️ **该 compose 与 Dockerfile 尚未在生产环境做过运行时验证**（开发机刻意不启用
Docker Desktop / 虚拟化，项目当前就是 `EXECUTOR=local`）。首次上服务器请按
`docker-compose.yml` 末尾的 4 步清单逐步验证 —— 其中最后一步「确认一次真实提问能出图」
才是**路径映射正确**的证明，前几步全绿也说明不了这一点。

> **结论：能宿主部署就用宿主部署。** 容器部署适合「已经决定接受
> 『应用容器 = 宿主 root』这个前提、并且希望环境不可变」的场景。

## 配置项

全部通过环境变量或仓库根 `.env` 覆盖（`.env` 不入库）。

| 变量 | 默认值 | 说明 |
|---|---|---|
| `ZHIPUAI_API_KEY` | 无 | 必填，智谱 API Key |
| `ZHIPU_BASE_URL` | 智谱 v4 地址 | OpenAI 兼容接口地址 |
| `CHAT_MODEL` | `glm-5.2` | 对话模型 |
| `PROXY_URL` | 空 | 需要走代理时设置（httpx 会读它） |
| `AGENT_MAX_STEPS` | `8` | 工具调用轮数上限，用尽后摘掉 tools 强制收尾 |
| `MAX_REPEAT_FAILURES` | `3` | 连续多少次同一处错误就强制收尾 |
| `HISTORY_MAX_CHARS` | `12000` | 循环内历史消息的字符预算 |
| `HISTORY_KEEP_TURNS` | `4` | 无论如何完整保留的最后 N 轮 |
| `EXECUTOR` | `docker` | `docker`（默认，有隔离）或 `local`（**仅本机调试，无隔离**） |
| `APP_ENV` | `dev` | `dev` / `production`。非 dev 有两重效果：`local` 执行器拒绝构造，且**强制要求外部注入 `JWT_SECRET`**（缺了直接拒绝启动） |
| `JWT_SECRET` | dev 下自动生成 | JWT 签名密钥。**`APP_ENV != dev` 时必填，缺了直接拒绝启动**；dev 下不设则随机生成并持久化到 `storage/secret.key`。多实例部署必须显式设置且保持一致 |
| `JWT_EXPIRE_HOURS` | `168` | 登录态有效期（小时），默认 7 天 |
| `ADMIN_USERNAME` | `admin` | 首次启动引导的管理员用户名 |
| `ADMIN_EMAIL` | `admin@example.com` | 首次启动引导的管理员邮箱 |
| `ADMIN_PASSWORD` | 空（随机生成） | 首次启动引导的管理员口令。**没有默认口令**：留空则生成随机强口令、启动横幅打印一次并强制首次改密；填入弱口令（<12 位 / <3 类字符 / 命中弱口令表）会**拒绝启动** |
| `ALLOW_REGISTRATION` | `false` | 是否允许自助注册。**默认关闭**：公网部署下任何人注册成功都会消耗**站点共用**的 API Key 额度。要开通成员时临时设为 `true` 并重启，开通完改回 `false` |
| `LOGIN_MAX_FAILURES` | `5` | 同一账号连续登录失败多少次后锁定 |
| `LOGIN_LOCK_SECONDS` | `300` | 锁定时长（秒）。锁定期内登录返回 429 + `Retry-After` |
| `STORAGE_DIR` | `<仓库>/storage` | 落盘根目录（SQLite、各用户工作区、`secret.key`） |
| `AUTH_DB_PATH` | `<STORAGE_DIR>/app.db` | 用户库路径，特殊部署可单独指定 |

## 接口

除 `GET /api/health` 与页面外，全部需要 `Authorization: Bearer <token>`。

| 方法 | 路径 | 说明 |
|---|---|---|
| GET | `/` | 工作台页面（无 token 时前端跳登录页） |
| GET | `/login` | 登录 / 注册页面 |
| GET | `/admin` | 管理后台页面（普通用户前端提示无权限，服务端接口才是真闸门） |
| GET | `/api/health` | 健康检查（**公开**，只返回全局状态：是否配了 Key、模型、执行器可用性） |
| POST | `/api/auth/register` | 注册，body `{"username","email","password"}`；注册即登录，返回 `{"token","user"}` |
| POST | `/api/auth/login` | 登录，body `{"account","password"}`；`account` 可填用户名或邮箱 |
| GET | `/api/auth/me` | 当前登录用户 |
| POST | `/api/auth/change-password` | 改密，body `{"old_password","new_password"}` |
| GET | `/api/workspace` | 当前用户工作区状态（已加载的数据文件与 Schema 摘要、运行目录） |
| POST | `/api/upload` | 上传数据（multipart，字段名 `files`），**替换**当前用户的数据集 |
| POST | `/api/ask` | 提问，body `{"question": "...", "session_id": 可选}`；返回答案、产物与**完整执行轨迹** |
| GET | `/api/artifact/{name}` | 取产物文件（限本人工作区，穿越由 `artifact_path` 拦） |
| POST | `/api/reset` | 清空当前用户的数据集 |
| GET | `/api/datasets` | 当前用户的数据集列表 |
| DELETE | `/api/datasets/{id}` | 删除数据集（删文件 → 删表行；文件删不掉时改名挪开） |
| GET | `/api/sessions` | 会话列表 |
| POST | `/api/sessions` | 新建会话，body `{"title": "可选"}` |
| GET | `/api/sessions/{id}/messages` | 某会话的全部消息（assistant 的 `meta` 里含执行轨迹与产物） |
| PATCH | `/api/sessions/{id}` | 重命名会话 |
| DELETE | `/api/sessions/{id}` | 删除会话（消息级联删除） |

管理员接口（前缀 `/api/admin`，全部需要 `admin` 角色）：

| 方法 | 路径 | 说明 |
|---|---|---|
| GET | `/users` | 用户列表 / 搜索（`?q=` 匹配用户名或邮箱） |
| PATCH | `/users/{id}` | 改角色 / 启禁用（不能降级或禁用自己的账号） |
| POST | `/users/{id}/reset-password` | 重置口令；不传 `password` 则生成随机口令，只在响应里出现一次 |
| GET | `/sessions` | 全站会话列表（`?user_id=` 过滤） |
| GET | `/sessions/{id}/messages` | 查看任意用户的完整记录，**含执行轨迹**（原样带出 `meta`） |
| DELETE | `/sessions/{id}` | 删除任意会话 |
| GET | `/datasets` | 全站数据集列表（`?user_id=` 过滤） |
| DELETE | `/datasets/{id}` | 删除任意数据集（删文件 + 删表行 + 摘掉该用户的内存工作区） |
| POST | `/system/reset-caches` | 清掉缓存的执行器与模型客户端 |
| GET | `/system/health` | 系统状态（不含 Key 明文） |
| GET | `/stats` | 用户 / 会话 / 消息 / 数据集计数与近 24h 活跃 |

## 明确不做的事

诚实划界，避免把「没做」说成「做了」：

> **本地执行器无 OS 级隔离，仅限本地自用或可信团队使用；生产环境必须配置 EXECUTOR=docker。**

- **不防内核 0day 逃逸**：容器共享宿主内核。要防这类威胁得上 gVisor /
  Kata / 独立虚机，本项目定位是挡住模型写出的常规危险代码。
- **`local` 执行器没有 OS 级隔离**：它只是本机调试工具，模型代码以当前用户权限运行、
  可读全盘、可出网。上面的「数据隔离」在 `local` 模式下只防误用，不防恶意越权 ——
  要真隔离就必须用默认的 Docker 执行器。非 dev 环境下 `local` 会拒绝构造。
- **不预算 fork 炸弹之外的攻击**：不防时序 / 缓存侧信道。
- **不做会话级上下文记忆**（当前）：会话用于归组与审计，历史不注入模型。
- **不引入 LangGraph / LangChain**：手写 Function Calling 循环，理由见 `AGENTS.md`。
