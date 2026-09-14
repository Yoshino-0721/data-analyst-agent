# 工作区迁移方案（只读盘点 · 模块 13）

- **盘点对象**：`D:\代码项目`
- **观测时刻**：2026-09-14 20:05 ~ 20:18（+0800）
- **执行约束**：全程只读。未执行任何 `Move-Item` / `Remove-Item` / `New-Item` / `git commit|checkout|reset|clean|gc`。仅使用 `Get-ChildItem` / `Select-String` / `git status|log|ls-files|config|rev-parse|count-objects|worktree|check-ignore|bundle verify(未执行)` 等读操作。
- **唯一写入**：本文件（位于 `C:\Users\YoshinoCiallo\AppData\Local\Temp\dsh-scratch\`，仓库之外）。

---

## 0. 三个与任务前提不一致的发现（先看这段）

盘点结果与任务书里的假设有 **3 处偏差**，会直接改变迁移范围：

### 0.1 工作区里有 **三个** Git 仓库，不是两个

`D:\代码项目\deepseek-harness` 也是一个独立仓库（`master`、16511 个 commit、`.git` 193.23 MB、`node_modules` 1.49 GB），占整个工作区体积的 **99.3%**。

```
D:\代码项目\deepseek-harness  remote origin = https://github.com/deepseek-ai/deepseek-harness.git
HEAD = c291e7961a  branch = master  commits = 16511
```

它是 DSH 本体的源码 checkout（桌面快捷方式 `DeepSeek Harness.cmd` 里 `DSH_REPO` 指向它）。**任何"把工作区收敛到新目录"的方案都必须把它算进去**，否则迁移等于没做。任务书只提了两个自建项目，所以我把它单列，方案里给它独立的处置策略。

### 0.2 `rag-knowledge-base` **配了远程地址**，但从未 push（"没有远程"的说法需要修正）

```
$ git -C D:\代码项目\rag-knowledge-base remote -v
origin  https://github.com/Yoshino-0721/rag-knowledge-base.git (fetch)
origin  https://github.com/Yoshino-0721/rag-knowledge-base.git (push)

$ git -C ... for-each-ref refs/remotes     # ← 输出为空
$ git -C ... branch -vv
* main 5a7196a feat(web): 登录页收敛到设计令牌（模块 11 第一页）   # ← 没有 [origin/main] 上游标记
```

- `remote.origin.url` **存在**（写在 `.git/config` 里）；
- `refs/remotes/` **完全为空**，分支没有 upstream → 这台机器上的这份 clone **从未成功 fetch 或 push 过**。

**结论**：git 历史确实只在本机。`data-analyst-agent` 连 remote 都没有（`git config --get-regexp '^remote\.'` 无输出），风险更高。

> **待确认**：GitHub 上 `Yoshino-0721/rag-knowledge-base` 远端是否已有内容。我用 `git ls-remote --heads <url>`（只读网络探测，已设 `GIT_TERMINAL_PROMPT=0`）尝试核对，返回 `exit=128 / Recv failure: Connection was reset` —— **本机当前网络不可达**，因此无法确认。若远端其实有内容，则"历史只在本机"对 rag 不成立；但**对本方案的最坏假设（无远程可恢复）必须按成立处理**。

### 0.3 工作区根还有一个非仓库目录 `会话记录\`

`D:\代码项目\会话记录\` 下 2 个 md（`2026-09-13.md` 6.7 KB、`两个项目 · 全周期会话记录（2026-09-11 ~ 2026-09-13）.md` 21.4 KB），不属于任何仓库，但内容是两个项目的开发档案。

### 0.4 补充：盘点期间工作区**正在被写入**

我第一次读 git 状态是 20:05（`main` = `5a7196a`），20:18 再读已经变成 `da7ad7f`，且新增 commit 的时间戳是 `2026-09-14 20:15:40`。同时 `~/.dsh/task-board/` 的 mtime 是 20:12，`Temp\dsh-scratch\` 里有 20:15 才生成的文件。

**含义**：迁移必须在一个**显式冻结的窗口**里做，快照前后各记一次 HEAD。这条写进了第 5 节 Phase 0。

---

## 1. 任务一：`D:\代码项目` 第一层盘点

第一层共 **5 个条目，全部是目录，没有任何散落文件**。

| # | 名称 | 类型 | 体积 | 是否属于两个自建项目 | 归属分类 |
|---|------|------|------|----------------------|----------|
| 1 | `rag-knowledge-base` | DIR | **3.46 MB** | ✅ 项目一（仓库） | **项目相关** |
| 2 | `data-analyst-agent` | DIR | **8.43 MB** | ✅ 项目二（仓库） | **项目相关** |
| 3 | `deepseek-harness` | DIR | **1845.65 MB** | ❌ 第三方仓库（任务书未提） | **项目相关**（独立第 3 仓库） |
| 4 | `.workbuddy` | DIR | **0.07 MB** | ❌ 不属于任何仓库 | **工具环境相关** |
| 5 | `会话记录` | DIR | **0.03 MB** | ❌ 不属于任何仓库 | **项目相关**（开发档案） |

**合计 ≈ 1857.6 MB。**

### 1.1 `rag-knowledge-base`（3.46 MB）逐项归属

| 条目 | 体积 | 归属 | 说明 |
|------|------|------|------|
| `.git` | 1.31 MB | 项目相关 | 43 commit；`count-objects`：204 loose / 196 in-pack，pack 689.82 KiB |
| `tests` | 1.17 MB | 项目相关 | 含 `__pycache__`（可再生） |
| `storage` | 0.30 MB | **工具/运行期** | 见 1.3 —— 但这个"运行期"里有必须带走的向量库 |
| `src` | 0.27 MB | 项目相关 | 含 `__pycache__` |
| `data` | 0.14 MB | 混合 | `data/.gitkeep`（入库）+ `data/FX.pdf`（**被 ignore，必须带走**） |
| `web` | 0.13 MB | 项目相关 | `index.html` / `admin.html` / `login.html` |
| `docs` | 0.09 MB | 项目相关 | `DEPLOY.md` / `design-tokens.md` / `demo.png` |
| `.pytest_cache` | 0.02 MB | **工具产物** | 可再生 |
| `README.md` `AGENTS.md` `requirements.txt` `docker-compose.yml` `Dockerfile` `LICENSE` `pytest.ini` `.gitignore` `.dockerignore` `.env.example` | < 0.03 MB | 项目相关 | 全部入库 |
| `.env` | ~0 MB（513 B） | **项目相关但入库不了** | 被 `.gitignore:9` 忽略，含真实 API Key，**必须手搬** |

入库文件数：**71**（`git ls-files | Measure-Object`）。

### 1.2 `data-analyst-agent`（8.43 MB）逐项归属

| 条目 | 体积 | 归属 | 说明 |
|------|------|------|------|
| `storage` | 4.71 MB | **工具/运行期** | 见 1.3 |
| `tests` | 1.41 MB | 项目相关 | 含 `__pycache__` |
| `.git` | 1.32 MB | 项目相关 | 40 commit；**`in-pack: 0` —— 全是 354 个松散对象，没有 packfile**，备份时尤其不能漏 `objects/` 子目录 |
| `src` | 0.47 MB | 项目相关 | `agent/ auth/ llm/ sandbox/ schema/` |
| `docs` | 0.29 MB | 项目相关 | |
| `web` | 0.11 MB | 项目相关 | |
| `.pytest_cache` | 0.04 MB | **工具产物** | 可再生 |
| `scripts` | 0.02 MB | 项目相关 | 4 个 demo/probe 脚本 + `__pycache__` |
| `__pycache__`（根） | < 0.01 MB | **工具产物** | 可再生 |
| `data` | ~0 MB | 项目相关 | 只有 `.gitkeep` |
| `.env` | ~0 MB（93 B） | **项目相关但入库不了** | 被 `.gitignore:2` 忽略，3 行，**必须手搬** |
| 其余根文件 | < 0.04 MB | 项目相关 | |

入库文件数：**87**。

### 1.3 运行期数据目录明细（关键）

**`rag-knowledge-base\storage\`（0.30 MB）—— 实际存在的东西只有 3 项：**

```
storage/
├─ chroma.sqlite3          311,296 B (0.297 MB)   ← Chroma 向量库，真实数据在这里
├─ manifest.json               122 B              ← 内容: {"FX.pdf": {"hash":"64fbfdb8…","chunks":6}}
└─ 8a3b0463-fed9-4964-a0cb-e801ce9b8a09/         ← 空目录（递归枚举 count=0）
```

**`data\`：** `data/FX.pdf` 139 KB（被 `data/*` 规则忽略，是唯一一份入库文档）+ `data/.gitkeep`。

**明确不存在（已用 `Test-Path` 逐个确认，全部 `False`）：**

| 路径 | 代码里的默认位置 | 状态 |
|------|------------------|------|
| `storage/app.db` | `src/auth/db.py:49` → `settings.storage_dir / "app.db"` | **不存在** |
| `storage/secret.key` | `src/auth/security.py:128` | **不存在** |
| `storage/manifests/` | `src/userspace.py:31` → `storage/manifests/user_<id>.json` | **不存在** |
| `data/users/` | `src/userspace.py:30` → `data/users/<uid>/` | **不存在** |

→ 全盘搜索 `app.db` / `secret.key`（排除 `.git`、`node_modules`）**零命中**。
→ **待确认**：RAG 的多用户运行时是否在本机真实 `storage/` 上跑起来过。从证据看，测试全部用 pytest tmp 夹具，真实目录里没有用户库痕迹。若确实没跑过，迁移时"必须带走"的 RAG 运行期数据就只有上面 3 项（合计 ~0.44 MB）。

**`data-analyst-agent\storage\`（4.71 MB）：**

```
storage/
├─ session/
│  ├─ data/          Inhouse Lab Test Summary June 2024.xlsx (81 KB)
│  ├─ artifacts/     17 个 PNG/HTML 图表产物（约 2.4 MB）
│  └─ runs/          19 个 run_<8hex>/{data,out,work}/ 目录（script.py + 输入副本 + 输出）
└─ p2_debug.log      0 B
```

同样**不存在**：`storage/app.db`、`storage/secret.key`、`storage/session/users/`（`Test-Path` 均为 `False`）。

⚠️ **重要细节**：当前代码写入的是 `storage/session/users/<uid>/{data,artifacts,runs}/`（见 `src/workspaces.py:35`），而现在磁盘上是 `storage/session/{data,artifacts,runs}/` —— 这是**单用户时期的遗留布局**。迁移后旧图表**不会**出现在新用户的 artifacts 列表里，需要手工搬到 `users\<uid>\` 下（见 5.3 与"待确认"清单）。

### 1.4 工具产物清单（可再生，不该跟着迁移）

| 类别 | 位置 | 体积 | 可再生方式 |
|------|------|------|-----------|
| pytest 缓存 | `rag\.pytest_cache`、`daa\.pytest_cache` | 0.02 + 0.04 MB | 跑一次 pytest |
| Python 字节码 | `rag\src\__pycache__`、`rag\tests\__pycache__`、`daa\__pycache__`、`daa\scripts\__pycache__`、`daa\src\**\__pycache__`、`daa\tests\__pycache__` | < 1 MB | import 时自动重建 |
| 依赖树 | `deepseek-harness\node_modules` | **1492.7 MB** | `pnpm install` |
| TS 增量编译信息 | `tsconfig.host.tsbuildinfo` 1.43 MB + `tsconfig.client.tsbuildinfo` 0.62 MB | 2.05 MB | 重新 build |
| 客户端构建产物 | `deepseek-harness\packages\client\*\lib\*`（**37 个文件内嵌了 `D:\代码项目\...` 绝对路径**） | 含在 `packages` 112.37 MB 内 | 重新 build；`lib/` 被 `.gitignore:4` 忽略，**不入库** |
| dsh 构建环境戳 | `.dsh-build\client-build-environment.json`（290 B，记着 `c291e79` / `0.1.5-rc.2` / hash） | ~0 MB | 重新 build |
| dsh 临时垃圾 | `deepseek-harness\_tmp_75568_986533eea756fc14d60e051f21527d07`（0 B，git status 里唯一一条 `??`） | 0 | 删除 |

**虚拟环境：两个项目里都没有 `.venv` / `venv`** —— 依赖装在**工作区之外**的托管 venv `C:\Users\YoshinoCiallo\.workbuddy\binaries\python\envs\default`。这是本次迁移的一个**重大利好**：迁工作区不需要碰解释器、不需要重建虚拟环境。

### 1.5 三分类汇总

| 分类 | 条目 | 体积 | 处置 |
|------|------|------|------|
| **项目相关** | `rag-knowledge-base`、`data-analyst-agent`、`deepseek-harness`、`会话记录` | ≈ 1857.5 MB（其中 1494.75 MB 是可再生的 `node_modules` + tsbuildinfo） | 复制到新工作区（排除可再生部分） |
| **工具环境相关** | `.workbuddy`（工作区级记忆，6 个 md） | 0.07 MB | 复制（它不属于任何仓库，但属于**这个工作区**） |
| **不确定 / 需用户确认** | `C:\…\Temp\dsh-scratch\`（~100 个临时脚本与产物，**在仓库之外**）、`D:\tmp_daa_backup\`（0.03 MB，2026-09-13 的调试散件）、`D:\_tmp_75568_*`（dsh 临时物） | — | 见第 6 节 |

> 我没有把 `会话记录` 放进"不确定"：读了内容（两个项目的全周期开发记录），它明确是项目档案，只是没有纳入任何 git 仓库。

---

## 2. 任务二：不该进仓库、也不该跟着迁移的东西

### 2.1 已经有了正确的 ignore 规则（现状是好的）

用 `git check-ignore -v` 实测确认：

```
rag-knowledge-base:  .gitignore:9:.env          → .env
                     .gitignore:12:data/*       → data/FX.pdf
                     .gitignore:14:storage/     → storage/chroma.sqlite3
data-analyst-agent:  .gitignore:2:.env          → .env
                     .gitignore:7:storage/      → storage/p2_debug.log
```

`.env` 在两个仓库里都**没有**被跟踪（`git ls-files | Select-String '\.env'` 只命中 `.env.example`）。这与工作区记忆里"提交前确认 `.env` 未被跟踪"的说法一致。

### 2.2 不该跟着迁移的清单

| 东西 | 现在在哪 | 为什么不该跟着搬 | 新工作区里放哪 |
|------|----------|------------------|----------------|
| `node_modules`（1492.7 MB） | `deepseek-harness\node_modules` | 抽查前 2000 个文件里 **1952 个带 `LinkType`（硬链接）**，指向 `D:\.pnpm-store\v11`。普通复制会把硬链接**解链成 1.5 GB 真实副本**，且可能破坏 pnpm 的符号链接结构 | **不搬**。新位置执行 `pnpm install` 重建（store 仍在 D: 盘 → 命中，不重新下载） |
| `tsconfig.*.tsbuildinfo`（2.05 MB） | `deepseek-harness\` 根 | 增量编译状态，路径一变就失效 | 不搬，重新 build 生成 |
| `packages/client/*/lib/*` 构建产物 | `deepseek-harness\packages\client\` | 37 个文件内嵌 `D:\代码项目\...` 绝对路径（`\0dsh-css:` region 标记）；`.gitignore:4 lib/` 已忽略 | 不搬（或搬后重建） |
| `.dsh-build\` | `deepseek-harness\.dsh-build` | 记录旧 HEAD/版本/内容 hash 的构建戳 | 不搬，重新 build |
| `_tmp_75568_…` | `deepseek-harness\` 根 | 0 B 未跟踪临时文件 | 不搬 |
| `__pycache__` / `*.pyc` | 两仓库多处 | 字节码缓存，与绝对路径绑定的 `co_filename` 会失效 | 不搬，自动重建 |
| `.pytest_cache` | 两仓库根 | 测试缓存（`lastfailed` / `nodeids`） | 不搬 |
| **运行期数据** | `<repo>\storage\`、`<repo>\data\` | SQLite 用户库、JWT 密钥、上传文件、向量库、执行产物、日志 —— 体积大、变动频繁、绝不该进 git | **必须搬**（但只搬"必须带走"的那部分，见 2.3）；位置上**建议仍留在各自仓库内的 `storage/`**，理由见 3.3 |

### 2.3 运行期数据：必须带走 vs 可以重新生成

| 数据 | 位置 | 体积 | 判定 | 理由 |
|------|------|------|------|------|
| Chroma 向量库 | `rag\storage\chroma.sqlite3` | 297 KB | **必须带走** | 丢了要重新上传 PDF + 重新调 embedding API（花钱、花时间） |
| 索引清单 | `rag\storage\manifest.json` | 122 B | **必须带走** | 与向量库配对；不带会导致增量索引判定错乱（它记录 FX.pdf 的 hash 与 6 个 chunk） |
| 源文档 | `rag\data\FX.pdf` | 139 KB | **必须带走** | 唯一入库文档；但**也可再生**（重新上传），属"带了更好" |
| SQLite 用户库 | `rag\storage\app.db` | — | **不存在** | 迁移前不存在，迁后首次启动按代码逻辑新建 |
| JWT 密钥 | `rag\storage\secret.key` | — | **不存在** | 同上；注意它的存在性决定"已签发 token 是否失效"（`src/auth/security.py:108` 有相关注释） |
| 多用户目录 | `rag\data\users\`、`rag\storage\manifests\` | — | **不存在** | 待确认是否曾跑过多用户 |
| 用户上传数据 | `daa\storage\session\data\` | 81 KB | **必须带走** | 用户上传的 xlsx，丢了没法复现 |
| 图表产物 | `daa\storage\session\artifacts\` | ~2.4 MB | **建议带走** | 17 个 PNG/HTML；可再生但需要重跑 Agent（花 API 额度） |
| 执行运行记录 | `daa\storage\session\runs\`（19 个 run_*） | ~2.2 MB | **可重新生成** | 每次运行的中间副本（输入副本 + script.py + 输出）；诊断价值 > 存储价值，建议带走（才 2 MB） |
| 调试日志 | `daa\storage\p2_debug.log` | 0 B | 可丢 | 空文件 |
| SQLite 用户库 / 密钥 | `daa\storage\app.db`、`storage\secret.key` | — | **不存在** | 同 RAG |
| 多用户目录 | `daa\storage\session\users\` | — | **不存在** | 见 1.3 的布局错位警告 |

**结论**：两个项目"必须带走"的运行期数据合计 **≈ 5.15 MB**，全部在两个 `storage/` 加 `rag\data\FX.pdf` 里。相对 1857.6 MB 的工作区，这是极小的一份，**没有任何理由为了省事而丢弃**。

---

## 3. 任务三：目标布局提议

### 3.1 建议的新工作区根：`D:\dev-workspace\`

**理由（每条都有观察支撑）：**

1. **必须在 D 盘。** `D:\.pnpm-store\v11` 占 1460.8 MB，`node_modules` 靠硬链接复用（实测 1952/2000 文件带 `LinkType`）。pnpm 硬链接**不能跨卷** —— 工作区一旦落到 C 盘，`pnpm install` 会退化成重新下载 ~1.46 GB。而本机网络刚刚才验证过不可达（`ls-remote` 连接被重置）。
2. **容量。** D: 剩 580 GB，C: 剩 247.6 GB；光 `deepseek-harness` 一个仓库就要 ~1.9 GB。
3. **用纯 ASCII、短、无空格的名字。** 这个项目组在中文路径上已经踩过坑，且仓库里留了证据：
   - `data-analyst-agent\src\sandbox\docker_executor.py:371-372`：Windows Docker Desktop 要求盘符小写正斜杠 `d:/…`，直接给 `D:\\…` 会**静默挂载失败**；
   - `rag-knowledge-base\AGENTS.md:78-79`：单引号 here-string 会吃掉双引号，`pathlib.Path(r"D:\x")` 变成 `pathlib.Path(rD:\x)` → SyntaxError；
   - `deepseek-harness` 的 37 个构建产物里，`\0dsh-css:` region 注释直接内嵌了 `D:\代码项目\...`。
   把中文父目录换掉，能一次性消掉一整类"路径带非 ASCII"的隐患。
4. **保留三个仓库的目录名不变**（`rag-knowledge-base` / `data-analyst-agent` / `deepseek-harness`）。这不是审美问题：`rag-knowledge-base\tests\test_docs.py:65-76` 有一条硬守卫 ——
   ```python
   refs = re.findall(r"D:/[^\s`*]+", doc)          # 第 72 行
   for ref in refs:
       assert ref.rstrip("/").endswith(ROOT.name)   # 第 76 行：末级目录名必须等于仓库目录名
   ```
   **改父目录不影响它，改仓库目录名必然让它变红。**
5. **保持"工作区 = 多项目容器"的现有语义与层级深度**（今天是 `D:\代码项目\<repo>`，新的是 `D:\dev-workspace\<repo>`）—— 深度不变 → 相对路径、`docker-compose.yml`、`Dockerfile` 的上下文假设一律不动。

### 3.2 推荐布局（方案 A：扁平，改动最小）

```
D:\dev-workspace\                      ← 新工作区根（= 桌面 .cmd 里的 DSH_WORKSPACE）
│
├─ .workbuddy\                         ← 工作区级记忆，从旧根整体搬来（不属于任何仓库）
│  └─ memory\
│     ├─ MEMORY.md                     ← 迁移后在这里加一条"路径变更公告"
│     └─ 2026-09-11.md … 2026-09-14.md
│
├─ docs\                               ← 工作区级文档（原「会话记录\」的内容迁到这里）
│  ├─ 2026-09-13.md
│  └─ 两个项目-全周期会话记录-2026-09-11~13.md
│
├─ rag-knowledge-base\                 ← 仓库①，整目录复制（含 .git、含 .env）
│  ├─ .git\  src\  tests\  web\  docs\  AGENTS.md  README.md  .env …
│  ├─ data\        ← FX.pdf 留在这里
│  └─ storage\     ← chroma.sqlite3 + manifest.json 留在这里
│
├─ data-analyst-agent\                 ← 仓库②，整目录复制（含 .git、含 .env）
│  ├─ .git\  src\  tests\  web\  docs\  scripts\  AGENTS.md  README.md  .env …
│  └─ storage\
│     └─ session\{data,artifacts,runs}\   ← 旧产物；按需手工搬到 users\<uid>\
│
└─ deepseek-harness\                   ← 仓库③，整目录复制（含 .git，**不含 node_modules**）
   ├─ .git\  packages\  apps\  docs\  scripts\  python\  …
   └─ node_modules\                    ← 迁移后由 pnpm install 重新生成
```

**为什么扁平、不加一层 `projects\`**：加 `projects\` 只多一层，功能上无害（两项目的路径都由 `__file__` 推导，见 3.4），但**多一层就多一处可能漏改的假设**（脚本、快捷方式、记忆文档、docker 挂载）。收敛工作区的目标是"路径可控"，不是"目录好看"。如果用户希望代码与数据在视觉上分开，再考虑 3.3 的方案 B。

### 3.3 可选方案 B：运行期数据抽到仓库外

两个项目的运行期数据**都可以不改一行代码**地重定向，因为都读环境变量：

- RAG：`rag-knowledge-base\src\config.py:134-135`
  ```python
  data_dir    = Path(_get("DATA_DIR",    str(PROJECT_ROOT / "data")))
  storage_dir = Path(_get("STORAGE_DIR", str(PROJECT_ROOT / "storage")))
  ```
- Analyst：`data-analyst-agent\src\config.py:129-131`
  ```python
  storage_root = Path(os.environ.get("STORAGE_DIR", "").strip()
                      or str(Path(__file__).resolve().parent.parent / "storage"))
  ```

对应的方案 B 布局：

```
D:\dev-workspace\
├─ projects\{rag-knowledge-base, data-analyst-agent, deepseek-harness}\
├─ runtime\
│  ├─ rag\{chroma.sqlite3, manifest.json, data\FX.pdf}
│  └─ analyst\session\{data,artifacts,runs}\
└─ scripts\        ← 跨项目临时脚本的新家（可选）
```

做法：在各自的 `.env` 里加 `STORAGE_DIR=D:\dev-workspace\runtime\rag` / `DATA_DIR=...`。

**我的建议：先做方案 A（数据留在仓库内），把方案 B 留作后续可选项。** 因为方案 B 把"数据位置"变成了 `.env` 里的隐式约定 —— 而 `.env` **不在 git 里**（已用 `check-ignore` 证实），一旦它丢了或没搬过去，服务会**静默地**在新的空 `storage/` 上启动，看起来像"数据全没了"。方案 A 的失败模式是可预测的（数据就在仓库里，跟着走）。

### 3.4 一个关键的好消息：**两个项目的路径逻辑天生是迁移安全的**

| 项目 | 路径来源 | 迁移后是否需要改源码 |
|------|----------|----------------------|
| `rag-knowledge-base` | `src/config.py:15` `PROJECT_ROOT = Path(__file__).resolve().parent.parent`；`src/server.py:43` `WEB_DIR = PROJECT_ROOT / "web"` | **不需要** |
| `data-analyst-agent` | `src/server.py:62` `PROJECT_ROOT = Path(__file__).resolve().parent.parent`；`src/config.py:96-97` `storage_root` 默认 `Path(__file__).resolve().parent.parent / "storage"` | **不需要** |
| `deepseek-harness` | 标准 pnpm workspace，无自建绝对路径 | **不需要** |

且两个 `.env` 里**都没有**设 `DATA_DIR` / `STORAGE_DIR`（RAG `.env` 20 行、Analyst `.env` 3 行，已逐行读完）→ 全部走 `__file__` 相对推导。**所以"迁移工作区"对源码是零改动。**

唯一的取舍是 `data-analyst-agent\src\sandbox\docker_executor.py` 走 Docker 挂载路径（`_host_path()` 把盘符小写化），新路径仍在 D 盘、仍走同一套规则 → 不受影响。

---

## 4. 任务四：必须一起改的路径引用

搜索范围：两个仓库 + `.workbuddy\memory` + `会话记录`（排除 `.git`、`node_modules`、`__pycache__`、`.pytest_cache`），模式 `代码项目|workbuddy|dsh-scratch|C:\Users|D:\|D:/`。

### 4.1 仓库内（会被 git 跟踪）

| 文件:行号 | 引用内容 | 是否必须改 | 说明 |
|-----------|----------|-----------|------|
| `rag-knowledge-base\docs\DEPLOY.md:82` | `# scp -r D:/代码项目/rag-knowledge-base/* root@服务器IP:/opt/rag/` | **建议改**（非功能性） | 被注释掉的部署示例。不改也不会挂：`tests/test_docs.py:76` 只断言末级目录名等于 `rag-knowledge-base`，与父目录无关。但文档里留着旧路径会误导 |
| `rag-knowledge-base\tests\test_docs.py:72` | `re.findall(r"D:/[^\s`*]+", doc)` | **绝对不要改** | 这是路径守卫正则本身，不是路径引用 |
| `rag-knowledge-base\tests\test_multiuser_ui.py:371-373` | `MANAGED_NODE = Path(r"C:\Users\YoshinoCiallo\.workbuddy\binaries\node\versions\22.22.2-3\node.exe")` | **不需要改** | 指 **C 盘工具链**，与工作区位置无关。且它是**回退**：`_node_binary()` 先 `shutil.which("node")`（第 377 行），PATH 里有 node 时永不触发。风险：托管 Node 版本目录改名后会失效（**不是迁移引入的**） |
| `data-analyst-agent\tests\test_multiuser_ui.py:421-423` | 同上（同一份 `MANAGED_NODE` 回退） | **不需要改** | 同上 |
| `data-analyst-agent\tests\test_web_ui.py:25-27` | 同上（`node_binary()` 第 36-42 行） | **不需要改** | 同上 |
| `rag-knowledge-base\.gitignore:17`、`.dockerignore:10` | `.workbuddy/` | 不需要改 | 相对模式。但**前提**：新工作区根仍然有一个叫 `.workbuddy` 的目录 |
| `data-analyst-agent\.gitignore:28` | `.workbuddy/` | 不需要改 | 同上 |

**说明性/示例性引用（不是真实工作区路径，一律不需要改）** —— 这些是文档里讲路径处理的例子，容易被误当成待改项：

| 文件:行号 | 内容性质 |
|-----------|----------|
| `rag-knowledge-base\AGENTS.md:78-79` | here-string 吃双引号的坑，示例 `pathlib.Path(r"D:\x")` |
| `data-analyst-agent\AGENTS.md:219` | 讲执行层盘符路径映射 `D:\...` |
| `data-analyst-agent\AGENTS.md:244-245` | 同上（here-string 示例） |
| `data-analyst-agent\.env.example:71`、`docker-compose.yml:40`、`Dockerfile:9` | 讲"容器内不可能出现 `D:\...`" |
| `data-analyst-agent\src\agent\tools.py:5` | 讲"不给模型 `data_path` 参数，免得它猜 `D:\\data\\sales.csv`" |
| `data-analyst-agent\src\sandbox\analysis.py:51` | 注释里的 traceback 样例路径 |
| `data-analyst-agent\src\sandbox\docker_executor.py:371-372` | 讲 Windows Docker 盘符小写化 |
| `data-analyst-agent\src\schema\extractor.py:24,168` | 讲宿主 `D:\...` vs 容器 `/data/...` |
| `data-analyst-agent\tests\test_docker_executor.py:519`、`tests\test_executor_contract.py:152` | 测试里的路径字符串样例（`C:\Users\alice\...`） |

### 4.2 仓库外 / 本机（不入库，但迁移后会失效）

| 位置 | 引用内容 | 影响 | 处置 |
|------|----------|------|------|
| `C:\Users\YoshinoCiallo\Desktop\DeepSeek Harness.cmd` **第 19 行** | `set "DSH_REPO=D:\代码项目\deepseek-harness"` | 🔴 **必须改** | 不改则 `if exist "%DSH_REPO%\pnpm-workspace.yaml"` 判定失败 → 走 `:fromnpm` 分支，用全局 npm 装的 dsh，而不是新工作区里的源码版 |
| 同上 **第 22 行** | `set "DSH_WORKSPACE=D:\代码项目"` | 🔴 **必须改** | 不改则 Agent 的工作区仍指向旧目录（第 35 行 `cd /d "%DSH_WORKSPACE%"`） |
| 同上 第 12 行 | `set "DSH_NODE_HOME=C:\Users\YoshinoCiallo\.workbuddy\binaries\node\versions\22.22.2-3"` | ⚪ 不改 | C 盘工具链，与迁移无关 |
| `C:\Users\YoshinoCiallo\.dsh\storages\workspace.json` | `"path": "D:\\代码项目"`（workspace id `13641251-…`，绑着 2 个 session） | 🟡 会失效 | **不要手改**。在新目录启动一次 dsh，让它自己写新的 workspace 记录；旧记录留着可回溯历史会话 |
| `C:\Users\YoshinoCiallo\.dsh\settings.yaml` | 无工作区路径（只有模型/主题配置） | ⚪ 无影响 | 不用动 |
| `C:\Users\YoshinoCiallo\.dsh\profiles\`、`task-board\` | 无绝对工作区路径（`ledger-v2.json` 223 B、`scheduler-v2.json` 28 B） | ⚪ 无影响 | 不用动 |
| `D:\代码项目\.workbuddy\memory\*.md`（4 天记录 + `MEMORY.md`） | **40+ 处** `D:\代码项目\…` 的叙述（如 `MEMORY.md:1,3,4,5,6`、`2026-09-11.md:4,136-139,148,153`、`2026-09-12.md:56,208,244`、`2026-09-14.md:15,24,34`） | 🟡 历史档案 | **不建议批量改**（它们是当时的事实记录）。迁移后在 `MEMORY.md` 顶部加一条"路径已变更"公告即可 |
| `D:\代码项目\会话记录\*.md` | 多处 `D:\代码项目\rag-knowledge-base` 等 | 🟡 历史档案 | 同上，不改 |
| `C:\Users\YoshinoCiallo\AppData\Local\Temp\dsh-scratch\` | ~100 个临时脚本/产物（`smoke.ps1`、`m9_*.py`、`login-p1.png`、`p1-*.txt` …） | ⚪ **不属于迁移范围** | 它们在仓库外、在工作区外；与工作区位置无关。**待确认**其中是否有需长期保留的 |
| `C:\Users\YoshinoCiallo\.workbuddy\binaries\{python\envs\default, node\versions\22.22.2-3, PortableGit\versions\1.2.0}` | 托管解释器 / Node / Git | ⚪ **完全不受影响** | 都在 C 盘工作区外，**不需要迁移、不需要改路径** |
| `D:\.pnpm-store\v11`（1460.8 MB） | pnpm 内容寻址 store | 🟢 **应保持原位** | 新工作区只要也在 D 盘，`pnpm install` 就能继续硬链接复用；把它一起搬走反而会打断硬链接 |
| `D:\tmp_daa_backup\`（0.03 MB，2026-09-13） | `.incoming_*.csv` ×5、`repro_restore.py`、`repro_upload.py`、`销售数据.csv` 等调试散件 | ⚪ 非仓库、非工作区 | **待确认**可删 |
| `D:\_tmp_75568_8dab46290bfa26c9980a840ddc909f7d\`、`D:\_tmp_75568_2327831c64e6ee87932d05a32ea87fdd` | dsh 临时物 | ⚪ 无关 | 可清理 |

> **搜索完整性说明**：我用 `Select-String` 对两个仓库 + 两个工作区级目录做了全量文本扫描（按扩展名白名单 + `.env*` + `Dockerfile*`，排除 `.git`/`node_modules`/`__pycache__`/`.pytest_cache`）。对 `deepseek-harness` 也做了扫描：命中的 37 个文件全部是 `packages/client/*/lib/*.js` 里的 `//#region \0dsh-css:D:\代码项目\...` **构建产物注释**，`lib/` 已被 `.gitignore:4` 忽略、**不入库**（`git ls-files 'packages/client/*/lib/*'` 返回 0），重建即消失。

---

## 5. 任务五：迁移步骤清单

> 全程为**复制**而非移动；旧目录在所有验证通过之前保持原样。命令以 PowerShell 给出；`$PY` / `$GIT` 为本机实际路径。

```powershell
$PY  = 'C:\Users\YoshinoCiallo\.workbuddy\binaries\python\envs\default\Scripts\python.exe'
$GIT = 'C:\Users\YoshinoCiallo\.workbuddy\binaries\PortableGit\versions\1.2.0\cmd\git.exe'
$OLD = 'D:\代码项目'
$NEW = 'D:\dev-workspace'
$BAK = 'D:\backup-dsh-20260914'      # 备份落点（可改）
```

### Phase 0 — 冻结与备份（**必须先做，且有一步是硬性要求**）

| 步 | 动作 | 命令 / 验证 | 风险点 |
|----|------|-------------|--------|
| 0.1 | **停止所有会写工作区的进程** | 现在 8000 / 8123 **无监听**（`Get-NetTCPConnection -State Listen` 实测），说明两个应用已停；但 dsh web 仍在跑（PID 83168 → `127.0.0.1:3080`）。迁移前关掉它 | 🔴 服务在跑时复制 `chroma.sqlite3` / `app.db` 会拿到**半写状态**的坏库。SQLite 有 `-wal`/`-shm` 时尤其危险 |
| 0.2 | **确认工作区干净** | `& $GIT -C "$OLD\rag-knowledge-base" status --short` → 空；对 `data-analyst-agent` 同样。`deepseek-harness` 会剩一条 `?? _tmp_75568_…`（0 B，可忽略） | 🔴 我实测 20:18 时三者**都是干净的**；但 **20:05→20:18 期间 HEAD 从 `5a7196a` 变成了 `da7ad7f`**，说明有并发提交。**必须显式确认冻结窗口**，否则复制到的是漂移中的快照 |
| 0.3 | **记录基线 HEAD** | `main=da7ad7f`（43 commit）/ `master=e3eb8e9`（40）/ `master=c291e7961a`（16511） | — |
| 0.4 | 🔴 **备份 git 历史（本方案最重要的一步）** | `& $GIT -C "$OLD\rag-knowledge-base" bundle create "$BAK\rag.bundle" --all`<br>`& $GIT -C "$OLD\data-analyst-agent" bundle create "$BAK\daa.bundle" --all`<br>验证：`& $GIT bundle verify "$BAK\rag.bundle"` | 🔴🔴 **两个自建仓库没有任何可用的远程副本**：`data-analyst-agent` 无 remote；`rag-knowledge-base` 配了 origin URL 但 `refs/remotes` 为空、从未 push，且远端状态**因本机断网无法核实**。`.git` 是 43 + 40 个 commit 的**唯一副本**。特别注意 `data-analyst-agent` 的 `.git` **`in-pack: 0`（354 个松散对象，无 packfile）** —— 任何漏拷 `objects/` 子目录的复制方式都会静默毁掉历史 |
| 0.5 | 顺手把 `deepseek-harness` 的临时垃圾记为"可删" | `_tmp_75568_986533eea756fc14d60e051f21527d07`（0 B） | ⚪ 非阻塞 |
| 0.6 | （可选）整理仓库 | `git gc` 可以把 `daa` 的 354 个松散对象打包，让后续复制更快更稳 | ⚠️ `gc` 是**写操作**，我没有执行；由用户决定 |

### Phase 1 — 复制到新根（旧目录一律不动）

| 步 | 动作 | 命令 | 风险点 |
|----|------|------|--------|
| 1.1 | 建新根 | `New-Item -ItemType Directory -Path $NEW` | — |
| 1.2 | 复制两个自建仓库（体积小，用 `-Force` 确保带上隐藏的 `.git`） | `Copy-Item -LiteralPath "$OLD\rag-knowledge-base" -Destination $NEW -Recurse -Force`<br>`Copy-Item -LiteralPath "$OLD\data-analyst-agent" -Destination $NEW -Recurse -Force` | 🔴 `Copy-Item` **必须加 `-Force`**，否则漏掉隐藏项（`.git`、`.env`、`.gitignore`）；不加就等于丢掉全部历史 |
| 1.3 | 复制 `deepseek-harness`（**排除 `node_modules` 与 tsbuildinfo**） | `robocopy "$OLD\deepseek-harness" "$NEW\deepseek-harness" /E /COPY:DAT /DCOPY:DAT /XD node_modules /XF *.tsbuildinfo` | 🔴 **绝不要连 `node_modules` 一起 robocopy**：1492.7 MB 里 97.6% 是硬链接到 `D:\.pnpm-store\v11` 的，复制会解链成真实副本（白占 1.5 GB，且 pnpm 结构可能损坏）。<br>🟡 robocopy 对隐藏项的默认行为和 `xcopy` 不同，**必须**在 1.6 显式验证 `.git` 在位 |
| 1.4 | 复制运行期数据（"必须带走"清单） | 包含在 1.2/1.3 的整目录复制里（`storage/`、`data/FX.pdf` 都在仓库内）。**若采用方案 B（3.3），则改为复制到 `$NEW\runtime\...`** | 🔴 若只挑了部分文件复制（比如只想带 `src/`），务必确认 `rag\storage\chroma.sqlite3` + `manifest.json` + `rag\data\FX.pdf` 三项都在 |
| 1.5 | 复制工作区级非仓库内容 | `Copy-Item "$OLD\.workbuddy" $NEW -Recurse -Force`<br>`Copy-Item "$OLD\会话记录" "$NEW\docs" -Recurse -Force`（或保留原目录名） | 🟡 `.workbuddy` 是**隐藏目录**，同样需要 `-Force` |
| 1.6 | 🔴 **验证 `.git` 与 `.env` 真的过来了** | `Test-Path "$NEW\rag-knowledge-base\.git"` → `True`；三个仓库逐个 `Test-Path ...\.git`；`Test-Path "$NEW\rag-knowledge-base\.env"`、`Test-Path "$NEW\data-analyst-agent\.env"` | 这一步花 5 秒，能挡住本方案最致命的失败模式 |
| 1.7 | `pnpm install` 重建依赖 | `cd $NEW\deepseek-harness; & "$env:USERPROFILE\.workbuddy\binaries\node\versions\22.22.2-3\pnpm.cmd" install` | 🟡 必须在新路径仍在 D 盘的前提下做，才能命中 store；本机网络当前不可达，**若 store 未命中会直接失败** —— 这也是"先在 D 盘内搬"的另一个理由 |
| 1.8 | 重建 dsh 客户端构建产物 | 按仓库标准流程启动 / build 一次 | 🟡 不重建的话，`packages/client/*/lib/*.js` 里仍留着 `D:\代码项目\...` 的 region 注释（纯注释，功能影响小，但属于"路径残留"） |

### Phase 2 — 在新位置验证（**旧目录仍原封不动**）

| 步 | 动作 | 命令 | 通过标准 |
|----|------|------|----------|
| 2.1 | HEAD 比对 | `& $GIT -C "$NEW\rag-knowledge-base" rev-parse HEAD` → `da7ad7f4573971b10b2759fe92f2cb493969bd2c`<br>`… data-analyst-agent` → `e3eb8e9615441f3f5f878e3d5daf7189ead07e38`<br>`… deepseek-harness` → `c291e7961a515f6d7af9304e7fd1d257929aef26` | 三个都**逐字相等** |
| 2.2 | commit 数比对 | `rev-list --count HEAD` → `43` / `40` / `16511` | 相等 |
| 2.3 | 仓库完整性 | `& $GIT -C <new> fsck --full --no-progress` | 无 `error` / `missing` |
| 2.4 | 忽略规则仍生效 | `& $GIT -C "$NEW\rag-knowledge-base" check-ignore -v .env storage/chroma.sqlite3 data/FX.pdf` | 三条都命中 |
| 2.5 | **跑测试** | `& $PY -m pytest -q`（在 `$NEW\rag-knowledge-base` 与 `$NEW\data-analyst-agent` 各跑一次） | 全绿。这两个仓库的测试大量使用 pytest tmp 夹具、不写真实 `storage/`，所以迁移后应保持不变 |
| 2.6 | 🔴 红线检查 | 若把仓库目录**改名**了，`tests/test_docs.py::test_deploy_doc_local_path_examples_follow_repo_location` 必红 | 所以：**别改仓库目录名** |
| 2.7 | 起 RAG 服务 | `cd "$NEW\rag-knowledge-base"; & $PY -m uvicorn src.server:app --port 8000`<br>`curl http://127.0.0.1:8000/health` | 迁移是否生效看两处：① `/api/admin/system/health`（**需要管理员 token**）返回的目录类字段应指向 `D:\dev-workspace\rag-knowledge-base\...`；② 页面上传一次真实文件后能在列表看到。**不要用 `/health`** —— 它只返回 `{"status":"ok"}`，路径情报是刻意移除的（`src/server.py` 的 docstring：公开接口不该给匿名调用者提供路径情报） |
| 2.8 | 起 Analyst 服务 | `cd "$NEW\data-analyst-agent"; & $PY -m uvicorn src.server:app --port 8123`<br>浏览器打开 `http://127.0.0.1:8123` | 能登录、能上传、能出图。<br>🟡 **预期差异**：旧 `storage\session\{artifacts,data,runs}` 是单用户布局，新代码读 `storage\session\users\<uid>\...`（`src/workspaces.py:35`）→ **旧图表大概率不会出现在 UI 里**。先验证"服务能跑 + 新上传能出图"，旧产物搬运另做（见 3 与待确认清单） |
| 2.9 | 起 dsh | 改完桌面 `.cmd`（Phase 3）后双击 | 输出必须是 `[源码模式] D:\dev-workspace\deepseek-harness`，**不是** `[npm 模式]` |

### Phase 3 — 切换

| 步 | 动作 | 细节 |
|----|------|------|
| 3.1 | 改桌面启动器 | `C:\Users\YoshinoCiallo\Desktop\DeepSeek Harness.cmd` 第 19 行 `DSH_REPO` → `D:\dev-workspace\deepseek-harness`；第 22 行 `DSH_WORKSPACE` → `D:\dev-workspace`。第 12 行 `DSH_NODE_HOME` **不动** |
| 3.2 | 重建 dsh workspace 记录 | 在新根启动一次 dsh web，让它自己往 `~\.dsh\storages\workspace.json` 写新条目。**不要手改那个 json**（它会随运行重写，手改会被覆盖；旧 workspace 记录留着可回溯会话） |
| 3.3 | 更新工作区记忆 | 在 `D:\dev-workspace\.workbuddy\memory\MEMORY.md` 顶部加一条"路径变更公告"：旧 `D:\代码项目` → 新 `D:\dev-workspace`，并注明旧路径已弃用。**历史记忆文件内容不要改动**（它们是当时的事实记录） |
| 3.4 | 修正文档里的旧路径 | `rag-knowledge-base\docs\DEPLOY.md:82` 的 `D:/代码项目/...` 改为新路径（改完跑 `pytest -q tests/test_docs.py` 确认守卫仍绿） |
| 3.5 | 处理 Analyst 旧产物（可选） | 若要在新 UI 里看到旧图表，把 `storage\session\{artifacts,data,runs}` 搬到 `storage\session\users\<uid>\` 下。**先确认 uid**，别猜 |

### Phase 4 — 观察后清理

| 步 | 动作 | 风险点 |
|----|------|--------|
| 4.1 | 观察 1–2 周 | — |
| 4.2 | 旧目录**改名保留**而非删除：`D:\代码项目` → `D:\代码项目.old-20260914` | 🔴 删除不可逆。改名是可逆的中间态 |
| 4.3 | 确认无误后再删除 | 🔴 这一步我不执行，也不建议在本轮执行 |
| 4.4 | 清理 `D:\tmp_daa_backup\`、`D:\_tmp_75568_*`、`deepseek-harness\_tmp_75568_…` | 🟡 需用户确认 |

### 5.1 风险总表

| # | 风险 | 触发条件 | 后果 | 规避 |
|---|------|----------|------|------|
| R1 | 🔴🔴 **丢 git 历史（不可恢复）** | 复制时漏掉 `.git`：用了不带 `-Force` 的 `Copy-Item`、`xcopy` 不带 `/H`、`robocopy /XA:H`，或"clone 一份到新目录然后删旧的" | 43 + 40 个 commit、reflog、未跟踪文件**永久丢失**。`data-analyst-agent` 连 remote 都没有；`rag-knowledge-base` 虽有 origin URL 但从未 push，且当前断网无法核实远端 | Phase 0.4 先 `git bundle create --all`；Phase 1.6 逐个 `Test-Path .git`；Phase 2.1/2.2/2.3 三重比对 |
| R2 | 🔴 丢未提交改动 | 复制时工作区 dirty | 改动丢失 | 复制前 `git status --short` 必须为空（当前为空）；注意 0.2 说的并发提交问题 |
| R3 | 🔴 丢 `.env`（含真实密钥） | `.env` 被 `.gitignore` 忽略，git 不会带它 | 新位置服务起不来（缺 API Key）；若已有签发 token 则全部失效 | 手搬 `.env`，核对行数（RAG 20 行 / Analyst 3 行） |
| R4 | 🔴 丢向量库 | 漏搬 `rag\storage\chroma.sqlite3` | 需重新上传 FX.pdf 并重新 embedding（消耗 API 额度） | 明确"必须带走"清单（本文件 2.3） |
| R5 | 🔴 复制出坏数据库 | 迁移时有进程持有 `storage/` | SQLite/Chroma 半写状态 | Phase 0.1 停进程 + 确认端口无监听 |
| R6 | 🟠 **白搬 1.5 GB 且可能损坏依赖树** | 把 `node_modules` 一起复制 | 硬链接解链成 1492.7 MB 真实副本；pnpm 结构可能失效 | 排除 `node_modules`，迁后 `pnpm install` |
| R7 | 🟠 依赖重建失败 | 新工作区不在 D 盘 → 硬链接不可用 → 需重新下载 ~1.46 GB | 迁移卡住（**本机网络当前不可达**，实测连接被重置） | 目标根必须在 D 盘；`D:\.pnpm-store\v11` 保持原位 |
| R8 | 🟠 破坏"测试即真相"的守卫 | 改了仓库**目录名** | `rag-knowledge-base\tests\test_docs.py:65-76` 必红 | 保留 `rag-knowledge-base` 目录名 |
| R9 | 🟠 dsh 启动静默降级 | 忘改桌面 `.cmd` 的 `DSH_REPO` | 走 `:fromnpm` 用全局 dsh；`DSH_WORKSPACE` 仍指旧路径 | 改完后看输出是否为 `[源码模式]` |
| R10 | 🟡 构建产物残留旧路径 | 37 个 `packages/client/*/lib/*.js` 内嵌 `D:\代码项目\...` | 客户端插件行为/缓存错配（`lib/` 不入库，git 层面看不出） | Phase 1.7/1.8 重新 install + build |
| R11 | 🟡 旧运行期产物在新 UI 里"消失" | Analyst 从 `session/*` 布局切到 `session/users/<uid>/*` | 看起来像数据丢失（实际是布局错位） | Phase 2.8 明确这是**预期现象**；搬运见 3.5 |
| R12 | 🟡 快照漂移 | 有 agent 正在并发提交 | 复制到不一致的快照 | 冻结窗口；迁移前后各记一次 HEAD |

### 5.2 一句话版执行顺序

> 停服务 → 确认 `git status` 干净 → **`git bundle` 备份三个仓库** → 按 D 盘内复制（含 `.git`、含 `.env`、不含 `node_modules`）→ **逐个 `Test-Path .git`** → HEAD/commit 数/`fsck` 三重比对 → `pnpm install` → 跑 pytest → 起 8000/8123 验证 → 改桌面 `.cmd` 两行 + 新位置启动 dsh → 观察 1~2 周 → 旧目录**改名**保留 → 确认后再删。

---

## 6. 待确认清单（我无法在只读 + 断网条件下定论）

| # | 待确认项 | 现有证据 | 建议的确认方式 |
|---|----------|----------|----------------|
| Q1 | GitHub `Yoshino-0721/rag-knowledge-base` 远端是否已有内容 | 本机 `refs/remotes` 为空；`ls-remote` 返回 `exit=128 / Connection was reset` | 恢复网络后 `git ls-remote --heads <url>`；若远端有内容，R1 的严重性下降一档 |
| Q2 | RAG 多用户是否在本机真实 `storage/` 跑过 | `storage/app.db`、`storage/secret.key`、`storage/manifests/`、`data/users/` 全部 `Test-Path = False`；全盘搜 `app.db`/`secret.key` 零命中 | 用户回忆 or 看 `~\.workbuddy\memory` 里是否有相关记录 |
| Q3 | Analyst 旧产物是否需要搬到 `storage/session/users/<uid>/` | 磁盘是 `session/{data,artifacts,runs}`，代码读 `session/users/<uid>/…` | 起服务后看 UI 是否空；再决定是否搬运 |
| Q4 | `C:\…\Temp\dsh-scratch\` 的 ~100 个脚本/产物哪些要留 | 全部在仓库外、工作区外，与迁移无关 | 用户挑；建议整体 zip 归档，不必进工作区 |
| Q5 | `D:\tmp_daa_backup\`（2026-09-13，0.03 MB）可否删 | 内容是调试散件（`.incoming_*.csv`、`repro_*.py`），非仓库 | 用户确认 |
| Q6 | 新工作区根名是否就用 `D:\dev-workspace` | 我的建议基于：同盘保留 pnpm store、D 盘余量 580 GB、ASCII 短路径规避已记录的路径坑 | 用户拍板 |
| Q7 | `deepseek-harness` 是否真的需要留在工作区内 | 它是第三方源码 checkout（有真 remote，可重新 clone），却占 99.3% 体积 | 若只为跑 dsh web，可考虑移到 `D:\tools\deepseek-harness` 而不进工作区。**但这会改变桌面 `.cmd` 的两处路径**，需用户决定 |
| Q8 | pnpm 硬链接重建后的真实新增占用 | store 已占 1460.8 MB；`pnpm install` 的净增量未测 | 迁移后 `Get-PSDrive D` 前后对比 |

---

## 附录 A：本次盘点用到的关键命令（可复现）

```powershell
# 体积
Get-ChildItem -LiteralPath 'D:\代码项目' -Force | ForEach-Object {
  $s = 0
  if ($_.PSIsContainer) {
    $s = (Get-ChildItem -LiteralPath $_.FullName -Recurse -Force -File -ErrorAction SilentlyContinue |
          Measure-Object -Property Length -Sum -ErrorAction SilentlyContinue).Sum
  } else { $s = $_.Length }
  [PSCustomObject]@{ Name=$_.Name; MB=[math]::Round($s/1MB,2) }
} | Sort-Object MB -Descending

# git 元数据
& $GIT -C <repo> remote -v
& $GIT -C <repo> for-each-ref refs/remotes          # rag/daa 均为空 → 从未 push
& $GIT -C <repo> rev-parse HEAD
& $GIT -C <repo> rev-list --count HEAD
& $GIT -C <repo> count-objects -vH
& $GIT -C <repo> status --short
& $GIT -C <repo> check-ignore -v .env storage/chroma.sqlite3

# 硬编码路径搜索
Get-ChildItem -LiteralPath <root> -Recurse -File -Force -ErrorAction SilentlyContinue |
  Where-Object { $_.FullName -notmatch '\\\.git\\|__pycache__|\\node_modules\\|\.pytest_cache' } |
  Select-String -Pattern '代码项目|workbuddy|dsh-scratch|C:\\+Users|D:/'
```

## 附录 B：基线快照（迁移前必须与之一致）

| 仓库 | 分支 | HEAD（完整） | commit 数 | 工作区状态 | 远程 |
|------|------|--------------|-----------|------------|------|
| `rag-knowledge-base` | `main` | `da7ad7f4573971b10b2759fe92f2cb493969bd2c` | 43 | clean | origin URL 已配，`refs/remotes` 空 |
| `data-analyst-agent` | `master` | `e3eb8e9615441f3f5f878e3d5daf7189ead07e38` | 40 | clean | 无 remote |
| `deepseek-harness` | `master` | `c291e7961a515f6d7af9304e7fd1d257929aef26` | 16511 | clean（仅 1 个 0 B 未跟踪文件） | origin = deepseek-ai（官方） |

- pnpm store：`D:\.pnpm-store\v11`（1460.8 MB）
- 托管工具链（**迁移不涉及**）：`C:\Users\YoshinoCiallo\.workbuddy\binaries\{python\envs\default, node\versions\22.22.2-3, PortableGit\versions\1.2.0}`
- 目标盘余量：D: 580 GB / C: 247.6 GB
- 需要复制的净体积估算：**≈ 362 MB**（= 1857.6 − 1492.7 `node_modules` − 2.05 tsbuildinfo − ~0.8 缓存）
