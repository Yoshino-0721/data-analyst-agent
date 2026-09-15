# 项目状态交接（写给下一轮会话）

> 最后更新：2026-09-14 深夜。**读这一份就够开工**；细节在各自的 commit message 与
> `docs/design-tokens.md` / `docs/optimization-backlog.md` / `docs/migration-plan.md` 里。

## 0. 一句话现状

两个仓库都已完成「多用户团队平台改造 + 设计令牌 pass 1 与 pass 2 + E2E 真实闭环 +
T5/R4/T1/R3/T2/T7/R2 七条最小修复 + 第二个项目演示期实测发现的三处缺陷（见落地记录末行）」，
测试全绿，工作区干净，**从未 push**；整仓 bundle 备份已放在 `D:\代码项目\_backup\`。
剩下的是 `docs/optimization-backlog.md` 里的其余条目（T3/T4/T6/T8、R1/R5、U1–U4、N1、N2）；
**模块 13（工作区迁移）已决定取消、不执行**（2026-09-14，理由见下方表格后的说明）。

| | rag-knowledge-base（p1） | data-analyst-agent（p2） |
|---|---|---|
| 分支 / HEAD | `main` `23f45b1` | `master` `efe7957` |
| 测试 | **476 通过** | **699 通过**（17 deselected，Docker 集成默认跳过） |
| 远程 | 配了 `origin`（github.com/Yoshino-0721/rag-knowledge-base）但**从未 push**；Q1（远端是否已有内容）等网络恢复后 `git ls-remote --heads origin` 核实：空则推、有内容则停下来给用户看 | remote 待加：`https://github.com/Yoshino-0721/data-analyst-agent.git`（用户建好空仓库后执行 `git remote add`）。**两仓库一律不用 `--force`** |
| 端口 | 8000 | 8123 |

> ⚠️ `.git` 是历史孤本。任何迁移/清理前先 `git bundle create --all`（见
> `docs/migration-plan.md` 的 Phase 0.4）。

> 🚫 **模块 13（工作区迁移）= 取消，不执行（2026-09-14 决策）**：收益只是路径整洁，
> 风险是 `.git` 历史孤本（两个仓库**从未 push**；p1 那个 `origin` 从未 fetch 过；
> 当时本机网络不可达，`git ls-remote` 核不了 Q1），而 `D:\代码项目\` 现状完全可用 ——
> 不值得为"好看"冒历史丢失的风险。`docs/migration-plan.md` **保留作参考、不删**：
> 它记录的是"若要迁移该怎么做"，不是待办；将来真要迁，仍按它的 Phase 0.4 先 bundle 备份。

## 1. 已完成

| 模块 | 内容 |
|---|---|
| C1 / C2 | 多用户改造：`src/auth/`（SQLAlchemy+SQLite+bcrypt+PyJWT）、按用户隔离、越权一律 404、登录节流、上传双闸门、token_version 失效 |
| 6.5 / 7 / 8 / 9 | 管理员建号（一次性口令 + 强口令校验）、孤儿会话、上传体积上限、安全加固（无默认口令 / JWT_SECRET / 本地执行器声明） |
| C3 | 前端强制改密闸门（login / index / admin 三页）+ 后台「新建账号」表单；3 份 Node 行为断言（17 / 20 / 41 项） |
| 模块 11 pass 1 | 设计令牌**颜色层**收敛：6 个页面 `:root` 之外的颜色字面量 **222 + 6 → 0**；`docs/design-tokens.md` 定稿（三项决策已拍） |
| 模块 11 pass 2 | **取值**收敛：admin（试金石）→ index，间距 / 字号 / 圆角全部走令牌（映射表在 `docs/design-tokens.md` §1.3 / §1.4）；`LENGTH_TOKEN_PAGES` 棘轮把 login / index / admin 三页锁死（含防"空转"的自测） |
| 模块 12 | 只读审计 18 条 → `docs/optimization-backlog.md` |
| 模块 13 | 工作区迁移**方案**已完成（→ `docs/migration-plan.md`）；**执行已取消**（2026-09-14 决策，理由见 §0） |
| E2E | `scripts/e2e_real.ps1`：真实服务闭环（随机管理员口令 → 强制改密 → 注册/建号 → 真实上传 → 真调 LLM → 越权 404 → 运行期文件确认）。**归档版实测 p1 31/31、p2 30/30** |
| T5 | 登录节流表按 `last_seen` LRU 淘汰，内存上限真的生效（原回收条件永不命中） |
| R4 | OOM 判定去掉裸词 `"killed"`（`/data/killed.csv` 曾被判成 OOM）；先红后绿 |
| T1 | 模型调用显式 timeout/max_retries；上传端点改同步 `def`；查询端点加并发闸门（满员 429） |
| R3 | 精排降级可见：`rerank_failed` / `rerank_error`（分类词）进响应，细节只进日志 |
| T2 | 上传落盘改原子写（同目录 `.incoming` + `os.replace`，**不涉及删除** —— 本机删除有钩子）；文件名归一化复用 `src/paths.py:safe_target_name`（全仓只留一处实现） |
| T7 | 重置口令两条路径一律置 `must_change_password=True`（**刻意不做强度校验**：边界在闸门上，"管理员下发口头临时码"是合理场景）；前端重置抽屉补了提示文案 |
| R2 | 未预期异常统一「固定文案 + 请求 id」（细节只进日志）：p1 从 21 字节纯文本升级为 JSON，p2 去掉异常细节泄露。第 1 条先落 `src/request_id.py` 机制（两仓库逐字相同），第 2 条再动错误结构 |

## 2. 未完成与已结清（✅ / 🚫 开头的条目已结清，原文留档）

> 本节保留原始编号（其它文档按内容引用它）。**未标 ✅ / 🚫 的才是真待办。**

1. ✅ **模块 11 pass 2（取值收敛）—— 已完成（2026-09-14）**：admin（试金石）→ index
   两页的间距 / 字号 / 圆角全部走令牌，取值表在 `docs/design-tokens.md` §1.3 / §1.4。
   **以下为当时的计划原文，已全部执行完，留档勿再照做。****前置两步已完成并提交**：取值棘轮守卫
   （`LENGTH_TOKEN_PAGES`，含防"空转"的自测）、index / admin 的尺度令牌定义
   （`3983f25` / `8142d14`，只加定义不改引用）。
   深色强调色**已按目视评审改回亮蓝** `#4c8dff`；浅色主题**已定** `#2563eb`
   （亮蓝 family 里最浅的正文达标档，见 design-tokens §3.1 / §3.1.1）。

   ### （留档，勿再照做）login 页取值收敛计划

   **前置**：先把 `login` 加进 `tests/test_multiuser_ui.py` 的 `LENGTH_TOKEN_PAGES`
   再跑一遍 —— **若它变红，说明 pass 1 有遗漏（正好当场修）**；
   不要为了让它变绿而放宽守卫。

   **做法**：正则只匹配**纯长度声明**（`padding` / `margin` / `gap` 及其
   `-top/-right/-bottom/-left` 变体、`border-radius`、`font-size`），
   把 px 值换成 `var(--space-k)` / `var(--fs-k)`；
   含 `var(` 的声明跳过；`0` 与 `1px` 保持原样（`1px` 是发丝线例外）。

   **间距映射**（文档 §1.3 原文）：

   | 旧值 | 1 / 2 / 3 / 5 | 6 | 7 / 9 | 10 / 11 / 13 | 14 / 15 / 18 | 20 / 22 | 24 | 30 / 32 |
   |---|---|---|---|---|---|---|---|---|
   | 新值 | `--space-1`（4px） | `--space-2`（6px） | `--space-3`（8px） | `--space-4`（12px） | `--space-5`（16px） | `--space-6`（24px） | 24px | `--space-7`（32px） |

   **字号映射**（文档 §1.4 原文）：

   | 旧值 | 11 | 12 | 13 / 14 | 14.5 / 15 / 15.5 / 16 / 17 | 19 / 20 | 24 |
   |---|---|---|---|---|---|---|
   | 新值 | `--fs-xs`（11.5px） | `--fs-sm`（12.5px） | `--fs-md`（13.5px） | `--fs-lg`（15px） | `--fs-xl`（19px） | `--fs-2xl`（24px） |

   **圆角**：login 在 pass 1 已全部走令牌，预计无需改。
   **画布**：login 是深色页，不涉及 §3.2 的浅色画布决策（那条只影响 index）。

   **pass 2 实际顺序（2026-09-14 决策）**：`admin`（**试金石**）→ `index`。
   **login 已无待办** —— 它的间距 / 字号 / 圆角在 **pass 1 就已收敛**（当时那个补丁
   就是照这两张映射表做的），本轮把 `login` 加进棘轮后**直接绿**即为证据；
   也就是说 "login → index → admin" 是文档与现实的一处漂移，已按实际改写。
   试金石之所以落到 admin 而不是 index：admin 与 login 同为深色页、长度声明更少；
   index 有 46 种 padding 取值，一次性大变不适合当第一站。

   **login 结果（2026-09-14，已完成）**：把 `login` 加进棘轮后**直接绿** ——
   pass 1 的登录页补丁当时就是照上面这两张表做的，间距 / 字号 / 圆角已全部走令牌，
   所以**本轮没有页面改动**，只是把它锁进棘轮（`LENGTH_TOKEN_PAGES`）。
   ⇒ 真正会产生可见变化的是 **index 与 admin**，"试金石"要落在它们身上。

   **收尾顺序**：改 → 跑全量 + 那条新棘轮 → 提交（`fix(web)`）→
   **停下等用户目视**。用户认可后按同一套推 index（**试金石已改为 admin**）；
   若不认可，用户会指出是"间距"还是"字号"哪一类 —— **只回退那一类**即可。

2. **模块 12 剩余条目**：`docs/optimization-backlog.md` 里 **T3/T4/T6/T8、R1/R5、
   U1–U4、N1、N2** 还没做（T2/T5/T7/R2/R3/R4 已完成，逐条证据见该文件的「落地记录」；
   N2「hint 细化分类」**已明确不做**，只留作可选）。
   **下一批候选：U2 → U4**（用户已点名，非必须；模块 13 取消后已无前置）。
3. 🚫 **模块 13（工作区迁移）—— 已取消，不执行（2026-09-14 决策）**：收益只是路径整洁，
   风险是 `.git` 历史孤本（两个仓库**从未 push**；当时本机网络不可达，`git ls-remote`
   核不了 Q1），而 `D:\代码项目\` 现状完全可用 —— 不值得为"好看"冒历史丢失的风险。
   `docs/migration-plan.md` **保留作参考、不删**（它写的是"若要迁移该怎么做"，不是待办）；
   将来真要迁，仍按它 Phase 0.4 先 `git bundle create --all`。
4. ✅ **R2 第 2 条（错误结构）—— 已完成（2026-09-14）**：两项目统一「固定中文文案 +
   请求 id」，异常细节只进日志（p1 从 21 字节纯文本升级为 JSON，p2 去掉 `{exc!s}` 泄露）。
   落地 commit 与实现事实见 `docs/optimization-backlog.md`「落地记录」；测试写法
   （`TestClient(app, raise_server_exceptions=False)`）见 `tests/test_request_id.py` 的
   模块 docstring。

   ⚠️ **上一版本节的交接文字是错的，留作错题**：它断言 p2 既有测试
   `tests/test_server.py:391-412` 的三条断言「在新结构下仍然成立、**无需改动**」——
   而紧挨着的**第 413 行**有一条 `assert "boom" in body["detail"]`，正是把旧泄露行为
   钉死的断言，必须改写成 `assert "boom" not in body["detail"]`。错因不是判断失误，是
   **按行号截断读取**（读到 412 就停了）；已沉淀为 AGENTS.md §5.4。

## 3. 关键上下文（新会话最容易踩的三类）

### 3.1 两仓库刻意同构

`src/auth/{api,deps,db,security,throttle,models}.py`、`src/paths.py`、`src/upload_guard.py`、
`src/concurrency.py`、`src/request_id.py` 在两个项目里**逐字相同**（可 `Get-FileHash` 比对；
改一处就要同步另一处）。`docs/status.md`、`docs/design-tokens.md`、
`docs/optimization-backlog.md`、`docs/migration-plan.md`、`scripts/e2e_real.ps1`
也是两份同一 sha256（`docs/status.md` 本身就是"一份文档描述两个仓库"）。
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
- **读日志先认编码**：本机自己写的日志是 UTF-8（`-Encoding UTF8` 读它），但
  **uvicorn 重定向出来的日志是本机 ANSI/GBK**。拿 UTF-8 去读它会变成
  `����Ա��ʼ����`，`Select-String '中文'` **静默不命中** —— 真实踩过：连续两次
  "找不到管理员口令横幅"，其实横幅就在文件里。这类日志用 `-Encoding Default`
  （PS 5.1 下即 GBK）。
  两个附带坑：① 服务还在写日志时**不要**用 `[System.IO.File]::ReadAllText`（文件被
  占用 → `IOException`），用 `Get-Content`；② 启动横幅里是 `口  令：`（两个空格做
  对齐），正则得写 `口\s*令：`，写 `口令：` 匹配不到。
- `curl.exe` 传 JSON 必须写文件走 `--data-binary @file`（内联会被 PS 5.1 打坏 →
  服务端 422）。
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

**服务状态（2026-09-14 收尾）**：8000 端口那个 dev server 已按记录过的 PID 停掉
（`Stop-Process -Id 103264,101100` —— **只按 PID，不按进程名**，见 AGENTS.md 一、通用工程规约）。
要再起就照上面的命令；确认端口占用用 `Get-NetTCPConnection -LocalPort 8000 -State Listen`。

## 5. 建议的下一步顺序

1. **U2 / U4**（用户已点名的下一批，非必须）：U2 上传语义文案 —— 说清"重新上传会
   **替换**当前数据集"，会删文件时先回显文件名；U4 注册 Tab 按 `allow_registration`
   默认隐藏（默认配置下现在这条是必然踩到的死路）。
2. 其余按影响面挑：R1/R5（磁盘与消息体只增不减）、T4（健康检查每次真跑 docker）、
   T6（多 worker 假设）、U1/U3、N1。
3. **T3 / T8 要先补真机 / 集成测试再改**（现有桩测试结构上覆盖不到，见 §3.2）。
4. **模块 13 已取消**，不再作为待办（`docs/migration-plan.md` 只作参考）。

## 6. 新会话自检清单（接手时先跑这三步）

在动任何代码之前，先确认"我接手的确实是同一份代码"：

1. **对 HEAD**：`git log --oneline -5`（两个仓库各跑一次）—— HEAD 应当与本文档 §0
   表格里的一致。**唯一例外**：如果 `git log` 显示 HEAD 比表格新，且多出来的那几条
   全是 `docs:` 前缀、只动了本文档，那也算一致（文档自己每改一次就会推进一次 HEAD，
   否则这份自检清单会自我失效）。其余情况一律先读新 commit，别按旧状态动手。
2. **对环境**：在各自仓库根目录跑一次全量测试（命令见 §4）—— 数字应当与 §0 一致
   （p1 476 / p2 699）。不一致说明代码或环境已经漂移，先查清楚再改，别在不确定的
   基线上做改动。
3. **对约定**：**先读 `AGENTS.md`，再读本文档** —— 两份合起来才完整：
   `AGENTS.md` 说"不许做什么、为什么"（架构决策 + 硬约束 + 踩过的坑），
   本文档说"现在到哪一步、还剩什么、下一步建议"。

三步都对齐，再开工。任何一步对不上，都是"先问清楚"的信号，而不是"猜一猜继续"。`
