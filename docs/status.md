# 项目状态交接（写给下一轮会话）

> 最后更新：2026-09-14 深夜。**读这一份就够开工**；细节在各自的 commit message 与
> `docs/design-tokens.md` / `docs/optimization-backlog.md` / `docs/migration-plan.md` 里。

## 0. 一句话现状

两个仓库都已完成「多用户团队平台改造 + 设计令牌 pass 1 + E2E 真实闭环 + T5/R4/T1/R3
四条最小修复」，测试全绿，工作区干净，**从未 push**。剩下的是"取值收敛 pass 2"、
两份清单里的其余条目、以及工作区迁移的执行。

| | rag-knowledge-base（p1） | data-analyst-agent（p2） |
|---|---|---|
| 分支 / HEAD | `main` `dedf84e` | `master` `6c72195` |
| 测试 | **460 通过** | **651 通过**（17 deselected，Docker 集成默认跳过） |
| 远程 | 配了 `origin`（github.com/Yoshino-0721/rag-knowledge-base）但**从未 push**，`refs/remotes` 为空 | 无 remote |
| 端口 | 8000 | 8123 |

> ⚠️ `.git` 是历史孤本。任何迁移/清理前先 `git bundle create --all`（见
> `docs/migration-plan.md` 的 Phase 0.4）。

## 1. 已完成

| 模块 | 内容 |
|---|---|
| C1 / C2 | 多用户改造：`src/auth/`（SQLAlchemy+SQLite+bcrypt+PyJWT）、按用户隔离、越权一律 404、登录节流、上传双闸门、token_version 失效 |
| 6.5 / 7 / 8 / 9 | 管理员建号（一次性口令 + 强口令校验）、孤儿会话、上传体积上限、安全加固（无默认口令 / JWT_SECRET / 本地执行器声明） |
| C3 | 前端强制改密闸门（login / index / admin 三页）+ 后台「新建账号」表单；3 份 Node 行为断言（17 / 20 / 41 项） |
| 模块 11 pass 1 | 设计令牌**颜色层**收敛：6 个页面 `:root` 之外的颜色字面量 **222 + 6 → 0**；`docs/design-tokens.md` 定稿（三项决策已拍） |
| 模块 12 | 只读审计 18 条 → `docs/optimization-backlog.md` |
| 模块 13 | 工作区迁移方案 → `docs/migration-plan.md` |
| E2E | `scripts/e2e_real.ps1`：真实服务闭环（随机管理员口令 → 强制改密 → 注册/建号 → 真实上传 → 真调 LLM → 越权 404 → 运行期文件确认）。**归档版实测 p1 31/31、p2 30/30** |
| T5 | 登录节流表按 `last_seen` LRU 淘汰，内存上限真的生效（原回收条件永不命中） |
| R4 | OOM 判定去掉裸词 `"killed"`（`/data/killed.csv` 曾被判成 OOM）；先红后绿 |
| T1 | 模型调用显式 timeout/max_retries；上传端点改同步 `def`；查询端点加并发闸门（满员 429） |
| R3 | 精排降级可见：`rerank_failed` / `rerank_error`（分类词）进响应，细节只进日志 |

## 2. 未完成

1. **模块 11 pass 2（取值收敛）** —— 间距 / 字号 / 圆角 / 画布的**取值**尚未收敛。
   当前只有颜色层收敛了；`docs/design-tokens.md` §1.3–§1.5 与 §3 给了映射表与目标值
   （圆角 8/12/16、间距 7 档、字号 6 档、浅色画布 `#eef1f8`、深色强调色 `#818cf8`）。
   ⚠️ pass 2 **会**产生可见观感变化，用户要求"先看登录页再决定"——**动手前先确认拿到反馈**。
   做法照 §2.1：一次一页，每页只改一页、跑全量、提交；`CONVERGED_PAGES` 已有棘轮
   （颜色层），pass 2 可考虑加"取值只能来自令牌"的检查。
2. **模块 12 剩余条目**：`docs/optimization-backlog.md` 里 T2/T3/T4/T6/T7/T8、R1/R2/R5、
   U1–U4、N1 都还没做。T3/T8 建议先补真机/集成测试再改（现有桩测试结构上覆盖不到）。
3. **模块 13 迁移未执行**：等网络恢复后 `git ls-remote` 核实 p1 远端是否已有内容（Q1），
   再按 Phase 0→4 走。目标根 `D:\dev-workspace\`、`deepseek-harness` 移到 `D:\tools\`。

## 3. 关键上下文（新会话最容易踩的三类）

### 3.1 两仓库刻意同构

`src/auth/{api,deps,db,security,throttle,models}.py`、`src/paths.py`、`src/upload_guard.py`、
`src/concurrency.py` 在两个项目里**逐字相同**（可 `Get-FileHash` 比对；改一处就要同步另一处）。
`docs/design-tokens.md`、`docs/optimization-backlog.md`、`docs/migration-plan.md`、
`scripts/e2e_real.ps1` 也是两份同一 sha256。
**但"同构的是架构与写法，不是每个端点的行为"** —— 例如"没有数据就提问"p1 返回引导语
(200)，p2 返回 400；照抄姊妹项目的语义写检查会造出假失败。

### 3.2 硬约束（详见两份 AGENTS.md）

- 每次改动**必须**提交，且提交前**全量 pytest 全绿**；只提交、**绝不 push**。
- commit message 只用 `feat` / `fix` / `docs` 前缀。
- 不引 LangChain / LangGraph（手写 FC 循环）；前端零 CDN、自包含。
- **绝不无差别杀进程**：只按记录的 PID / 专属 profile 过滤，执行前先列清单。
- 清理类动作一律 `try/except BaseException`：本机删除会被钩子重定向并可能在批量时抛
  `SystemExit`。
- 测试数漂移守卫：改测试数后要同步 README **徽章与正文**，且**别用裸数字替换**
  （README 里有 HTTP 429 这类同名数字），用带上下文的片段并断言恰好命中 1 处。

### 3.3 Windows / 本机环境坑（真实踩过）

- 工具链不在 PATH：`C:\Users\YoshinoCiallo\.workbuddy\binaries\{python\envs\default\Scripts\python.exe, node\versions\22.22.2-3\node.exe, PortableGit\versions\1.2.0\cmd\git.exe}`。
- **绝不在 PowerShell 里内联多行 Python/JS/SQL**：写成文件再执行；`.ps1` 必须
  **UTF-8 with BOM**，否则中文被按 GBK 解码、`-match '中文'` 静默不命中。
- `Get-Content` 读 UTF-8 日志必须 `-Encoding UTF8`；`curl.exe` 传 JSON 必须写文件走
  `--data-binary @file`（内联会被 PS 5.1 打坏 → 服务端 422）。
- **PowerShell 变量名不区分大小写**：`$ask` 与 `$Ask` 是同一个变量（曾把路径覆盖成响应
  对象，表现为"HTTP 0 + 读到上一次的响应体"，看起来像服务端鉴权坏了）。路径变量一律带
  `Path` 后缀。
- `[string]$null` 在 PS 5.1 下**不**转空串；`(Get-Content -Raw).Trim()` 对空文件会抛异常。
  用字符串插值 `"$(...)"`。
- 补丁脚本规约：每处替换断言"恰好命中 N 处"，**全部成功才写盘**。跨文件改动要按
  "不会留下坏中间态"的顺序写（先改被调方还是调用方，先想清楚）。
- Edge 在会话中运行时，headless 的 `--dump-dom`/`--screenshot` 不产出文件 →
  **像素级目视验收我这边做不了**，只能用静态+行为断言替代，并如实标注 SKIP。

## 4. 怎么跑

```powershell
$py = 'C:\Users\YoshinoCiallo\.workbuddy\binaries\python\envs\default\Scripts\python.exe'
# 全量测试（在各自仓库根目录）
& $py -m pytest -p no:cacheprovider -W ignore -q --tb=line
# 起服务（8000 / 8123）
& $py -m uvicorn src.server:app --port 8000
# 真实闭环（会写真实 storage、会调 LLM 花钱；要求 storage/app.db 不存在）
powershell -ExecutionPolicy Bypass -File scripts/e2e_real.ps1 -Project p1
```

**凭证状态**：两个项目的本地 `storage/app.db`、`secret.key` 已按"删号重建"清过；
管理员口令只在服务启动日志里随机生成一次（横幅）。**脚本里没有任何明文口令。**
临时工具与保险 zip 在 `%TEMP%\dsh-scratch\`（常驻：`smoke.ps1`、`auth_check.ps1`）。

## 5. 建议的下一步顺序

1. 拿到登录页目视反馈 → **pass 2**（一次一页：login → index → admin）。
2. 从 `docs/optimization-backlog.md` 挑下一批：建议 T2（上传非原子覆盖）、T7（重置口令
   也置 must_change_password）、R2（异常对外呈现两边各错一半）。
3. 网络恢复后做模块 13 的 Q1 核实与迁移。

## 6. 新会话自检清单（接手时先跑这三步）

在动任何代码之前，先确认"我接手的确实是同一份代码"：

1. **对 HEAD**：`git log --oneline -5`（两个仓库各跑一次）—— HEAD 应当与本文档 §0
   表格里的一致。**唯一例外**：如果 `git log` 显示 HEAD 比表格新，且多出来的那几条
   全是 `docs:` 前缀、只动了本文档，那也算一致（文档自己每改一次就会推进一次 HEAD，
   否则这份自检清单会自我失效）。其余情况一律先读新 commit，别按旧状态动手。
2. **对环境**：在各自仓库根目录跑一次全量测试（命令见 §4）—— 数字应当与 §0 一致
   （p1 460 / p2 651）。不一致说明代码或环境已经漂移，先查清楚再改，别在不确定的
   基线上做改动。
3. **对约定**：**先读 `AGENTS.md`，再读本文档** —— 两份合起来才完整：
   `AGENTS.md` 说"不许做什么、为什么"（架构决策 + 硬约束 + 踩过的坑），
   本文档说"现在到哪一步、还剩什么、下一步建议"。

三步都对齐，再开工。任何一步对不上，都是"先问清楚"的信号，而不是"猜一猜继续"。`
