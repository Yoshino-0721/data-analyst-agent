# 宿主部署手册（data-analyst-agent）

面向 **Linux 宿主 + systemd + venv** 的部署方式。仓库里另有 `Dockerfile` /
`docker-compose.yml`（把**应用自身**容器化）——那条路径必须把宿主的
`/var/run/docker.sock` 挂进应用容器，拿到 socket 约等于拿到宿主 root。**本手册不走那条路**：
应用直接跑在宿主上，只有「模型写出来的代码」会被关进兄弟容器，**全程不挂 docker.sock**。

目标机器实测环境：Ubuntu 24.04.4 LTS / Docker 29.6.1 / Python 3.13（deadsnakes）。

---

## 0. 环境门槛

| 项 | 要求 | 说明 |
|---|---|---|
| OS | Ubuntu 22.04 / 24.04 LTS | — |
| Python | **≥ 3.13** | `pyproject.toml` 的 `requires-python` 已锁死；Ubuntu 24.04 自带 **3.12**，必须先装 3.13 |
| Docker | daemon 可用 | 应用通过宿主 `docker` CLI 调它 |
| 运行账号 | 在 `docker` 组 | 否则创建沙箱容器报 Permission denied |
| 磁盘 | ≥ 5 GB | 沙箱镜像约 700 MB |

**Step 0：先把 Python 3.13 验证/装上**（这一步不过，后面全部免谈）：

```bash
python3.13 --version || {
  sudo apt-get install -y software-properties-common
  sudo add-apt-repository -y ppa:deadsnakes/ppa
  sudo apt-get update -qq
  sudo DEBIAN_FRONTEND=noninteractive apt-get install -y python3.13 python3.13-venv python3.13-dev
}
```

## 1. 落代码与依赖

```bash
sudo mkdir -p /opt/data-analyst-agent && sudo chown ubuntu:ubuntu /opt/data-analyst-agent
git clone https://github.com/Yoshino-0721/data-analyst-agent.git /opt/data-analyst-agent
cd /opt/data-analyst-agent
python3.13 -m venv .venv
.venv/bin/pip install -q --upgrade pip
.venv/bin/pip install -e .
```

> ⚠️ **坑一（必炸）：`python-multipart` 没被声明，但 `/api/upload` 需要它。**
> 该端点用 `File(...)`，FastAPI 在**注册路由**时就要求 python-multipart，
> 缺了直接抛 `RuntimeError: Form data requires "python-multipart" to be installed`，服务起不来。
> 本机因为别处装过所以测试全绿，换台干净机器必炸。
> ```bash
> .venv/bin/pip install python-multipart
> ```
> 根治应在 `pyproject.toml` 的 `dependencies` 补 `python-multipart>=0.0.9`。

## 2. 构建沙箱镜像

```bash
docker build -t data-analyst-sandbox:latest src/sandbox/image
```

> ⚠️ **坑二（国内服务器必卡）**：容器里 pip 走默认源 `pypi.org`，实测 **24 分钟零进展**
> （连基础镜像都没拉下来），健康检查会一直卡在 `executor_ok:false`。
> Dockerfile 已支持构建时注入 pip 源，直接传参即可：
> ```bash
> docker build \
>   --build-arg PIP_INDEX_URL=http://mirrors.tencentyun.com/pypi/simple \
>   --build-arg PIP_TRUSTED_HOST=mirrors.tencentyun.com \
>   -t data-analyst-sandbox:latest src/sandbox/image
> ```
> 不传这两个参数时行为与原来完全一致（走 PyPI 默认源）。注入后整个构建约 **45 秒**完成。
>
> 若手上是没有该 ARG 的旧版 Dockerfile，可临时改一份（**别改仓库里的**）：用 `awk`
> 只在**行首为 `FROM `** 的指令后插入 `ENV` —— 注意原文件注释里也出现了同样的字符串，
> 直接做字符串整体替换会插错位置、写出语法损坏的 Dockerfile。

镜像内含 pandas / numpy / matplotlib / openpyxl / xlrd + Noto CJK 中文字体。

## 3. 配置 `.env`

落地 `/opt/data-analyst-agent/.env`，`chmod 600`：

```ini
APP_ENV=production                 # 非 dev → 强制 JWT_SECRET，且拒绝构造 local 执行器
JWT_SECRET=<secrets.token_urlsafe(48)>
EXECUTOR=docker                    # 生产唯一合法值
ZHIPUAI_API_KEY=<线上独立限额 Key>
ZHIPU_BASE_URL=https://open.bigmodel.cn/api/paas/v4/
CHAT_MODEL=glm-5.2
ADMIN_PASSWORD=<强口令>             # 留空则随机生成 + 首次强制改密
ALLOW_REGISTRATION=false           # 公网部署建议关掉自助注册
STORAGE_DIR=/opt/data-analyst-agent/storage
```

配置既读进程环境也读仓库根 `.env`，**进程环境优先**；交给 systemd 的
`EnvironmentFile=` 指向它，即为单一真源。

## 4. systemd 单元

`/etc/systemd/system/data-analyst-agent.service`：

```ini
[Unit]
Description=data-analyst-agent
After=network-online.target docker.service
Wants=docker.service

[Service]
Type=simple
User=ubuntu
Group=ubuntu
WorkingDirectory=/opt/data-analyst-agent
EnvironmentFile=/opt/data-analyst-agent/.env
ExecStart=/opt/data-analyst-agent/.venv/bin/python -m uvicorn src.server:app --host 127.0.0.1 --port 8080
Restart=on-failure
RestartSec=3

# 安全提示：运行本服务的账号必须在 docker 组，而 docker 组 ≈ 宿主 root
# （可读写 /var/run/docker.sock、可挂载宿主任意目录提权）。
# 该账号只用来跑本服务，切勿把普通账号加进 docker 组。
NoNewPrivileges=true
PrivateTmp=true
ProtectSystem=full
ProtectHome=read-only
ReadWritePaths=/opt/data-analyst-agent/storage

[Install]
WantedBy=multi-user.target
```

```bash
sudo systemctl daemon-reload && sudo systemctl enable --now data-analyst-agent
journalctl -u data-analyst-agent -f
```

`--host` 取什么值决定暴露面，见第 7 节（默认建议 `127.0.0.1` + 隧道/反代；
确认要走裸端口暴露时再改成 `0.0.0.0`）。
未设 `ADMIN_PASSWORD` 时，初始口令从这里取：`journalctl -u data-analyst-agent -n 60`。

## 5. 健康检查（注意字段名）

```bash
curl -s http://127.0.0.1:8080/api/health
```

返回（**是 `ok`，不是 `status`**）：

```json
{"ok":true,"has_api_key":true,"model":"glm-5.2","executor":"docker","executor_ok":true,"executor_note":"ok"}
```

`executor_ok:false` 表示沙箱镜像没就位，`executor_note` 会给出 `docker build` 提示。

## 6. 端到端验收

```bash
B=http://127.0.0.1:8080
TOKEN=$(curl -s -X POST $B/api/auth/login -H 'Content-Type: application/json' \
  -d '{"account":"admin","password":"<口令>"}' \
  | python3 -c "import sys,json;print(json.load(sys.stdin)['token'])")
printf '类别,金额\n华东,120\n华北,95\n华南,143\n西南,78\n' > /tmp/sample.csv
curl -s -X POST $B/api/upload -H "Authorization: Bearer $TOKEN" -F "files=@/tmp/sample.csv"
curl -s -X POST $B/api/ask -H 'Content-Type: application/json' -H "Authorization: Bearer $TOKEN" \
  -d '{"question":"按类别统计金额，画柱状图"}'
# 取 *.png 后访问 $B/api/artifact/<name> 应得到 PNG
```

容易记错的三个字段名：上传是 **`files`**（列表，不是 `file`）；提问体是
`{"question": ..., "session_id": ...}`（**没有** `dataset_id`，数据走用户 workspace）；
登录是 **`account`**（用户名或邮箱，不是 `username`）。

**只有取回 PNG 才算真通过** —— 它同时证明：Schema 提取正确、`EXECUTOR=docker` 能创建兄弟容器、
`/data` `/out` 挂载没退化成空目录、中文字体没渲染成方块。前几步全绿都证明不了这些。

> 排障提醒：若用 `sudo` 跑过一次脚本，会在 `/tmp` 留下 root 属主的同名文件，
> 后续非 root 写入会 Permission denied，导致读到**上一次的陈旧响应**。换用 `mktemp -d` 即可。

## 7. 公网访问

三种方式，按暴露面从小到大排列。登录态是 `Authorization: Bearer <token>`，
**明文 HTTP 下 token 与口令都会被中间人截获、等同于账号泄露**，所以后两种方式都必须上 HTTPS。

### 7.1 Cloudflare Tunnel（暴露面最小，推荐）

应用保持 `--host 127.0.0.1`，源站零入站端口，`cloudflared` 把公网流量转到本机 8080。
临时隧道无需账号（`cloudflared tunnel --url http://127.0.0.1:8080`，URL 每次重启会变）；
命名隧道需 Cloudflare 免费账号，URL 固定。
`cloudflared.service` 与 `data-analyst-agent.service` **互不依赖、无启动顺序要求**。

### 7.2 Nginx 反代 + HTTPS

别忘了 `client_max_body_size`（否则大 CSV 会被 Nginx 先返 413）。

### 7.3 直连裸端口（当前部署采用）

把 unit 里的 `--host` 改成 `0.0.0.0`，直接 `http://<公网IP>:8080` 访问：

```bash
sudo sed -i 's/--host 127\.0\.0\.1/--host 0.0.0.0/' /etc/systemd/system/data-analyst-agent.service
sudo systemctl daemon-reload && sudo systemctl restart data-analyst-agent
```

⚠️ **"防火墙放行"要过两道关，别只做一道：**

1. **云厂商安全组**（控制台操作，机器内部改不了）——放行 TCP 8080；
2. **主机层 ufw** —— 安全组放行不等于主机放行，务必确认：
   ```bash
   sudo ufw status          # Status: active 时须再放行
   sudo ufw allow 8080/tcp
   ```

验证要**从另一台机器**（不是服务器本机）访问，`curl` 走代理时记得绕过：

```bash
curl -s --noproxy '*' http://<公网IP>:8080/api/health
```
本机 `curl 127.0.0.1` 通不代表公网通，这一步不能省。

这条路是**明文 HTTP**，仅适合短期演示或内网；正式使用请回到 7.1 / 7.2 上 HTTPS。

## 8. 回滚

- 应用：`git checkout <上一稳定 commit>` → `systemctl restart data-analyst-agent`
  （`.env` / `storage/` 均 gitignore，不受影响）。
- 沙箱镜像：重建前先 `docker tag data-analyst-sandbox:latest data-analyst-sandbox:prev`，
  异常时 tag 回去再重启服务。
- 停对外：`systemctl stop cloudflared`，应用本身照常在本地 8080 提供服务。
