# 应用镜像：只负责跑 FastAPI 服务与 Agent 编排。
# 模型生成的代码**不在这个镜像里跑** —— 它在沙箱镜像里跑，见 src/sandbox/image/Dockerfile。
#
# ⚠️ 这个 Dockerfile 与 docker-compose.yml 是给 **Linux 服务器**用的，两个前提：
#   1. 应用要创建「兄弟容器」，所以容器里需要 docker **CLI**（不需要 daemon），
#      并由 compose 挂载宿主的 /var/run/docker.sock；
#   2. DockerExecutor 传给 docker 的挂载路径是**宿主路径**，因此存储目录在宿主与
#      容器内必须位于**完全相同的绝对路径**（由 compose 的 APP_STORAGE_PATH 保证）。
#      Windows 上容器内不可能出现 `D:\...` 盘符路径，这套组合在 Windows 上不成立。
FROM python:3.13-slim

# 只要 CLI：用官方 cli 镜像里的静态二进制，避免为了一个客户端装整套 docker.io
COPY --from=docker:27-cli /usr/local/bin/docker /usr/local/bin/docker

# 沙箱镜像必须在**宿主**上先构建好（应用容器通过宿主 daemon 使用它）：
#   docker build -t data-analyst-sandbox:latest src/sandbox/image
# compose 不会替你构建它。

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1

WORKDIR /app

# 依赖直接取 pyproject.toml ——**刻意不在这里再抄一份包清单**。
# 抄一份就是两个真相来源：pyproject 加了依赖、Dockerfile 忘了加，镜像里就会在
# 运行时才 ImportError。这里用 tomllib 读出来交给 pip，只有一处需要维护。
COPY pyproject.toml ./
RUN python -c "import tomllib; d = tomllib.load(open('pyproject.toml','rb')); open('/tmp/req.txt','w').write('\n'.join(d['project']['dependencies']))" \
 && pip install --no-cache-dir --upgrade pip \
 && pip install --no-cache-dir -r /tmp/req.txt

COPY src ./src
COPY web ./web

# ⚠️ 刻意**不**降权到非 root 用户运行，理由不是图省事：
#   这个容器挂了宿主的 /var/run/docker.sock，而拿到 socket 就等于拿到宿主 root
#   —— 容器里的进程随时可以 `docker run --privileged -v /:/host` 把自己提上来。
#   所以"容器内换成非 root 用户"在这里几乎不减少任何真实攻击面，却会引入一整类
#   bind mount 的 uid 归属摩擦（宿主 storage 目录属主要对得上、docker.sock 的
#   GID 要对得上），部署时表现为莫名其妙的 Permission denied。
#   真正要守住的是**沙箱容器**非 root（见 sandbox 层，那边是实打实的隔离边界）。
#   如果你不接受"应用容器 = 宿主 root"这个前提，正确做法不是在这里降权，
#   而是**不要把应用放进容器**：在宿主上用 venv + systemd 跑，
#   一样是 EXECUTOR=docker，但没有 socket 挂载这一条。
EXPOSE 8000
CMD ["python", "-m", "uvicorn", "src.server:app", "--host", "0.0.0.0", "--port", "8000"]
