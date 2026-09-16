> 本文由一次**只读**代码审计产出（2026-09-14），**只列不动手**。
> 条目里的 `文件:行号` 是审计当时的快照；动手修之前请先按当前代码复核。
> 落地状态见每条的一句话判断与第 6 节。

# 双项目只读代码审计 · 功能优化清单

审计对象（两个独立 Git 仓库，均**只读**，未修改/创建/删除仓库内任何文件，未执行 git 写操作，未启停任何进程）：

- 项目一 `rag-knowledge-base`（下文简称 **rag**）：FastAPI + ChromaDB，端口 8000
- 项目二 `data-analyst-agent`（下文简称 **daa**）：FastAPI + 手写 FC Agent + 沙箱，端口 8123

方法：先读两份 `AGENTS.md` 的架构决策（不引入 LangChain/LangGraph、沙箱策略只读配置、成本控制三样、清理动作不得影响主流程等），再逐文件读代码并交叉核对测试。清单中每条结论都先读到对应代码行才写；仅为推断的条目已显式标注「待确认」。

已核对的环境事实（用于判定影响面，非推测）：

- `openai 3.13.0`：`DEFAULT_TIMEOUT = Timeout(connect=5.0, read=600, write=600, pool=600)`、`DEFAULT_MAX_RETRIES = 2`
- `anyio 4.15.1`：默认线程池 `CapacityLimiter(40)`（`anyio/_backends/_asyncio.py:3162`）—— FastAPI 的同步 `def` 端点共享这 40 个槽位
- `starlette 1.6.0`：`FileResponse.media_type` 缺省时 `guess_type(filename)`，`.html → text/html`
- `chromadb 1.5.9` / `sqlalchemy 2.0.52` / `bcrypt 5.0.0` / `pyjwt 2.14.0`
- 两份 `src/auth/{api,deps,throttle}.py`、`src/paths.py`、`src/upload_guard.py` **字节完全相同**（SHA256 一致），`db.py/models.py/security.py` 仅路径字段与模型名有差异 → 认证类问题对两个项目**同时成立**，下表不重复列两遍

架构约束遵守情况：本清单**未**建议引入任何编排/框架类依赖；所有建议都在既有架构（手写循环、策略模式、SQLite 单机自持）内。

---

## 一、真风险（正确性 / 安全 / 数据丢失）

| # | 文件:行号 | 问题 | 一句话判断（影响面 + 建议动作） |
|---|---|---|---|
| T1 | rag `src/llm.py:25`；rag `src/server.py:178,208-231,357`；daa `src/llm/client.py:136,164`；daa `src/server.py:243-275,279`；`anyio/_backends/_asyncio.py:3162` | 长阻塞任务既没有时长上限、也没有并发隔离：rag 的 `OpenAI(...)` 未传 `timeout`/`max_retries`（SDK 默认 read=600s、重试 2 次，一次 chat 最坏约 30 分钟）；`/query`、`/api/ask` 是同步 `def`，跑在只有 40 个槽的 anyio 线程池；rag `/upload` 反而是 `async def`，却在事件循环里同步跑 `ingest()` → 分块 + **阻塞式调用 embedding 接口**（`src/server.py:221-226`），一次 50 MiB 上传可以把整个事件循环堵死（连 `/health`、别人的登录一起卡） | 任何一个已登录用户开几十个并发提问（或传一个大文件）就能让全站不可用，且成本随重试次数翻倍——建议给两个项目都显式设 `timeout`（读 30–60s）与 `max_retries`（0/1），把 `/upload` 改成同步 `def`（或 `run_in_threadpool`）让阻塞任务离开事件循环，并对 `/query`、`/api/ask` 加接口级并发信号量。 |
| T2 ✅ | rag `src/server.py:204-213` | 上传落盘是 `target.write_bytes(content)`：直接覆盖同名文件、**非原子**；且文件名归一化是手写的 `Path(upload.filename).name`，没有复用仓库里唯一的 `src/paths.py:safe_target_name` | 重名上传静默覆盖旧文档（旧内容在向量库重建前一直"存在但已不是它"），写盘中途失败（磁盘满/中断）会留下半个文件把好文件顶掉——建议先写临时文件再 `os.replace`，并改用 `safe_target_name`，重名时明确提示或自动改名。 |
| T3 | daa `src/sandbox/docker_executor.py:100-108,137,329,332`；daa `src/sandbox/local_executor.py:213-222` | `subprocess.Popen(...PIPE)` + `communicate(timeout=...)` 会把子进程输出**全量读进宿主内存**，`max_output_bytes`（默认 8 KiB）只在读取完成之后由 `truncate_output` 事后截断 | 容器内一行 `while True: print("x"*100000)` 能在 30 秒超时窗口内往宿主内存灌进 GB 级数据（`--memory` 只约束容器，管不到宿主侧的管道缓冲）——这正是「桩测试全绿、真机才炸」的典型（现有测试注入的 `RunOutcome` 都是小字符串）：建议边读边累计字节、超限立即 kill 进程并标注截断。 |
| T4 | daa `src/server.py:213-218`；daa `src/sandbox/docker_executor.py:181-198` | 公开且无需鉴权的 `/api/health` **每次都真的去跑** `subprocess.run(["docker","info"])`（超时 15s）与 `docker image inspect`（超时 30s），`available()` 结果没有任何缓存 | 匿名请求即可反复占用那 40 个线程池槽位并持续 fork `docker` CLI（每个请求最坏 45 秒），是零成本的拒绝服务面——建议把探测改为启动期做一次 + 结果缓存（带 TTL 或独立刷新端点），`/api/health` 只读缓存。 |
| T5 | rag/daa `src/auth/throttle.py:28,49-55,72-73`（两文件字节相同） | 内存上限形同虚设：`_prune_locked()` 只回收「已解锁 **且** `failures == 0`」的条目，而攻击者每个请求换一个用户名时，新条目 `failures` 恒为 1，永远不满足回收条件，`_states` 无界增长 | 注释声称防的正是「换用户名撑爆内存」这类 DoS，实际挡不住（且没有任何测试覆盖该上限分支）——建议改成按 `last_seen` 的时间窗/LRU 淘汰，并补一条"灌 2 万个不同账号后表大小有界"的回归测试。 |
| T6 | rag `src/store.py:18`；rag `src/auth/db.py:113-115,126-127` + `src/auth/models.py:30`；rag/daa `src/auth/throttle.py:7-13`；rag `docs/DEPLOY.md:94-95`；rag `Dockerfile:25` | 全仓按「单进程」写，但部署文档明确鼓励多实例：`DEPLOY.md:94-95` 只说"多实例必须对齐 `JWT_SECRET`"，而 ① `get_collection()` 每个请求新建 `chromadb.PersistentClient`，多个 worker/副本共享同一 bind mount 时会同写一份 Chroma 库（跨进程无锁，**待确认**为数据损坏而非仅锁等待）；② `init_db()` 跑在每个 worker 的 lifespan 里，首次启动 users 表为空时两个 worker 会抢建同名 admin，后提交者撞 `users.username` 唯一约束（`models.py:30`）在启动阶段抛 IntegrityError；③ 登录节流是进程内计数，阈值被放大成 N 倍 | 多 worker 首次启动可能进 crash-loop、并发计数失效、向量库行为不可预期——建议二选一：钉死单进程（`--workers 1` + 文档明确禁止 `--scale`，并在启动时检测到多进程直接拒绝），或把共享状态（Chroma、引导动作、节流）挪出进程；（附带）`src/auth/security.py:104-140` 的 `_secret()` 无缓存，非 production（未设 `JWT_SECRET`）时每个请求都 stat+read 一次 `secret.key`，文件被删会静默换密钥让全站 token 失效。 |
| T7 ✅ | rag `src/admin_api.py:159-181`；daa `src/admin_api.py:150-172`；rag `tests/test_admin_api.py:221-235` | 重置口令走 `payload.password` 分支时只校验长度 ≥6（`PasswordReset`），既不查强度、也不置 `must_change_password`，与 `create_user` 的「一次性口令必须首登改密」承诺不一致；测试 `test_reset_password_with_explicit_password` 把"显式口令立即生效"钉死 | 「管理员设的口令是一次性的」这条安全属性在重置路径上失效，管理员图省事设 `123456` 会长期有效——建议重置一律置 `must_change_password=True`（或至少跑 `password_strength_problem`），并同步改这条测试的断言。 |
| T8 | daa `src/server.py:374-381`；`starlette/responses.py`（`FileResponse` 按扩展名猜 `media_type`）；daa `src/sandbox/image/Dockerfile:69`、`storage/session/artifacts/region_sales_bar.html` | `/api/artifact/{name}` 用 `FileResponse` 原样返回产物，`.html` 会被猜成 `text/html` **内联**返回，且全仓没有任何 `Content-Security-Policy` / `X-Content-Type-Options` / `Content-Disposition`（grep `add_middleware` 零命中） | 产物由模型生成的代码写入 `/out`（其输入又来自不可信的 CSV 内容），仓库里已经存在真实 `.html` 产物；任何人直接打开 `/api/artifact/x.html` 就在应用**同源**执行脚本，可读走 `localStorage.auth_token`（前端 `web/index.html:548,1088`）并发往外网——**待确认**可达性：前端只把产物渲染成 `<img>`，没有自动生成的链接，需要用户手输/粘贴 URL；即便如此也建议非图片类产物一律加 `Content-Disposition: attachment` + `nosniff`。 |

| T9 ✅ | rag `storage/chroma.sqlite3`（清库流程漏了它）；`scripts/e2e_real.ps1`；对照 `src/documents.py:125`、`src/ingest.py:113,144` | **被删/被替换的文档仍能被检索到**（幽灵文档）：清单与磁盘上都已没有 `p1-upload.pdf` / `e2e-p1-upload.pdf`，但向量库里还留着它们的旧分块 —— 实测 `source` 元数据：`FX.pdf` 18 条、`p1-upload.pdf` 6 条、`e2e-p1-upload.pdf` 6 条；后果是**引用来源里会显示已不存在的文件**（README 的效果图就带着这两个名字，见 `docs/demo.png`） | 应用自己的删除路径其实**是清向量的**（`documents.delete_document`、`ingest` 重灌前都调了 `collection.delete(where={"source": ...})`）；根因是**绕过应用的清库**：轮换凭证时删了 `app.db` / `manifests/` / `data/users/`，**唯独没删 `storage/chroma.sqlite3`**（E2E 的上传把这个状态制造了出来）。建议①把向量库补进清库清单（连同 `chroma/` 目录）；②启动时用 manifests 与向量库对账，清掉"清单里没有、向量库里还有"的 `source` 并打日志 —— 这样将来任何绕过应用的删除都不会留下幽灵文档。附带：`scripts/e2e_real.ps1` 跑完应清掉自己上传的文档与向量（或改用一次性临时用户/独立库）。 |
---

## 二、健壮性（失败处理与边界情况）

| # | 文件:行号 | 问题 | 一句话判断（影响面 + 建议动作） |
|---|---|---|---|
| R1 | daa `src/session.py:277-280`（`clear_artifacts` 全仓 **0 调用**）；daa `src/sandbox/docker_executor.py:424-441`；daa `src/workspaces.py:25,38-46` | 磁盘与内存只增不减：`_collect_artifacts` 把 `/out` 里**每个**文件无条件拷进持久产物目录（无单文件大小上限、无数量上限、无回收策略），只有运行目录被 `_prune_old_runs` 回收（`keep_recent_runs=5`），产物目录与 `_registry` 里的 `Session` 都永不淘汰 | 沙箱代码写一个超大 `/out/*.bin` 会被永久拷进 `storage/`（AGENTS.md 只说"保留最近 5 次运行"，没覆盖产物），用户一多内存也随 `Session` 常驻——建议给产物加单文件/总量上限 + 「保留最近 N 次」回收，并把 `_registry` 换成有容量上限的 LRU。 |
| R2 ✅ | rag `src/server.py:227,249,393`（全仓 grep `exception_handler` 零命中）；rag `web/index.html:1360-1365`；daa `src/server.py:88-105` | 未预期异常的对外呈现，两边各错一半：rag 只 `except RuntimeError`，而上游 LLM 抛的是 `openai.APIError`（非 RuntimeError）→ 交给 Starlette 默认处理器，返回 21 字节纯文本 `Internal Server Error`，前端只能显示"请求失败（HTTP 500）"；daa 有全局 JSON 兜底（好），但把 `f"服务器内部错误：{exc!s}"` 原样回给客户端 | rag 生产上最常见的故障（模型 429/超时、Chroma 报错、磁盘满）对用户和前端都**没有可行动信息**，而 daa 反向泄露内部异常细节（含路径/SQL）给任意登录用户——建议 rag 照搬 daa 的 JSON 兜底、daa 把 detail 换成固定文案 + 请求 id（细节只进日志）。 |
| R3 | rag `src/retrieve.py:104-109`（模块内无 `logger`、无计数） | 精排失败一律 `except Exception: return _take(candidates, limit)` 静默退回向量序：`RERANK_MODEL` 配错、额度耗尽、网络抖动在外部表现完全一样 | 系统"看起来正常"，只是检索质量静默降级，没人会去查（与 AGENTS.md「配置错误不吞」的精神冲突）——建议至少 `logger.warning` 一次并在响应里带 `rerank_failed` 标记，让前端/管理员看得见。 |
| R4 | daa `src/sandbox/analysis.py:109-116,146,155`；对照 `AGENTS.md:117-118` | OOM 判定词表里仍留着**裸词 `"killed"`**（AGENTS.md 自己立的规则是"判定词表不放裸词"，当初只修了 `oom` 这一条），`classify_execution` 对 stderr 做子串匹配 | stderr 里任何含 `killed` 的普通报错（如 `FileNotFoundError: /data/killed.csv`）都会被判成 OOM，模型据此去优化内存——正是他们记录过的"方向彻底跑偏"：建议只保留 `oom-kill` / `out of memory` 这类完整短语，再加上 `exit_code==137` 兜底。 |
| R5 | daa `src/server.py:349-365,427-440` | 每条 assistant 消息把完整执行轨迹（每步的**模型源码** `code`、`stdout`/`stderr`）JSON 塞进 `messages.meta`，而 `/api/sessions/{id}/messages` 一次性返回该会话全部消息、无分页无上限 | 单条 meta 最坏可达 MB 级（`max_code_bytes=20480`、每 outcome 输出 8 KiB、`AGENT_MAX_STEPS=8`），长会话一次拉取就是几十 MB JSON，SQLite 与浏览器内存同涨且永久保留——建议列表接口默认只回摘要（`steps` 折叠/裁剪）、详情按需分页，或把轨迹拆到独立表按需查。 |

---

## 三、体验优化（前端与 API 的用户可感知问题）

| # | 文件:行号 | 问题 | 一句话判断（影响面 + 建议动作） |
|---|---|---|---|
| U1 | daa `web/index.html:1142-1152` | 「清空数据」按钮：① 无二次确认（服务端 `src/server.py:384-392` 会立刻删掉用户全部数据文件）；② `catch(err){ /* 清空失败不阻塞使用 */ }` 吞掉错误后仍把 `state.files` 置空 → 界面显示"没有数据"、提问框被 `setBusy(false)` 灰掉，刷新后数据又回来了；③ 只清了聊天区 DOM（`:1148-1149`）却没重置 `state.sessionId`，服务端会话仍在 | 一个不可撤销的破坏性动作既没有确认、也可能与服务端状态分叉，用户会以为数据已删/已丢——建议加 `confirm`（列出将删除的文件名）、失败时调 `refreshWorkspace()` 回滚 UI、并明确"清空数据"与"清空对话显示"是两件事。 |
| U2 | daa `web/index.html:379-380,985-988`；daa `src/session.py:194-199,250-255` | 上传语义是**替换**（`replace_files` 会把 `files` 整体换掉，并"尽力"删掉不在新集合里的旧文件），但拖拽区文案只说"拖拽 Excel / CSV 到这里"，成功后提示也只说"已加载 N 个数据文件" | 用户以为在追加数据，实际第二次上传会**删掉第一份文件**（磁盘上一起删），属于静默数据丢失——建议文案写清"重新上传会替换当前数据集"，并在会删除文件时先回显将要移除的文件名。 |
| U3 | daa `web/index.html:373-382,1114`（`:1135` 只给 textarea 绑了 keydown） | 拖拽上传区是 `div`，只绑了 `click`，没有 `tabindex` / `role="button"` / Enter-Space 键处理，真正的 `<input type="file" hidden>` 又不可聚焦 | 纯键盘用户**完全无法上传数据**，而"没数据就不能提问"（`setBusy` 会禁用提问框），等于整站不可用——建议给拖拽区 `tabindex="0" role="button" aria-label="上传数据文件"` 并处理 Enter/Space。 |
| U4 | rag `web/login.html:236,477-496`；rag `src/auth/api.py:94-101`；rag `src/config.py:70,141` | 登录页的「注册」Tab 无条件显示，而自助注册默认关闭（`ALLOW_REGISTRATION=false`）：用户必须填完用户名+邮箱+口令提交后才拿到 403「本站已关闭自助注册」 | 默认配置下这是**必然踩到**的死路，且失败点太靠后——建议登录页启动时向一个公开端点（或扩展 `/health`）问一次 `allow_registration`，关闭时直接隐藏注册 Tab 并提示"请联系管理员开通"。 |

---

## 四、可做可不做（锦上添花，明确标注）

| # | 文件:行号 | 问题 | 一句话判断（影响面 + 建议动作） |
|---|---|---|---|
| N1 | rag `src/admin_api.py:187-203,206-220,236-246` | 管理后台的全站会话、单会话消息、全站文档三个列表都是**全量返回**，没有分页/时间范围/条数上限（`/stats` 已给了计数，但没有下钻用的分页参数） | **锦上添花**：单机自用/小团队规模下完全够用，只有数据涨到上千行时管理后台首屏才会明显变慢——想改就加 `limit/offset`（或按 `user_id` 已支持的过滤 + 时间窗），不改也不影响正确性。 |
| N2 | daa `src/sandbox/analysis.py`（hint 分类处，`classify_execution` 旁边） | 执行失败回填的 hint 只有粗分类（列名 / 超时 / 一般报错）。2026-09-15 实测：模型把 `FancyArrowPatch` 的 `mutation_scale` 写给 `plt.Rectangle`，`AttributeError: Rectangle.set() got an unexpected keyword argument` 拿到的是「若为 KeyError/列名相关错误，先用 get_schema 确认列名」这条**完全无关**的提示 | **锦上添花**：模型自己 1 步就改对了（step4 报错 → step5 去掉该参数即 OK），穷举 API 误用进 hint 属于过度设计、维护成本大于收益 —— **本轮明确不做**。真要做，只加判据稳定、可穷举的少数几类（例如 `unexpected keyword argument` → 「检查该参数是否属于这个 API / 这个版本」），并且必须有「不认识就退回原提示」的兜底。 |

---

## 附：审计中被排除的「疑似问题」（已确认非缺陷，避免后续重复排查）

- daa 不做跨轮上下文记忆（追问需把条件说全）：`README.md:184,442` 已明确列为当前设计，`run_agent()` 本就不接收 DB 历史 —— **不是缺陷**，不要按"漏传 history"去修。
- 多用户隔离本身经得起检查：rag/daa 的 `_own_session()` 一律 404 不泄露存在性、`user_paths()`/`get_workspace()` 按 uid 派生目录与集合名，`tests/test_isolation.py` 已覆盖"跨用户读/改名/删/提问"四类越权；本次未发现可利用的越权路径。
- 前端 XSS：两份 `web/index.html` 与两份 `web/admin.html` 的不可信文本（用户名、会话标题、消息正文、模型代码/输出）**逐处**走了 `escapeHtml`/`textContent`，`escapeHtml` 覆盖 `& < > " '`；未发现漏转义点（T8 的问题在服务端产物响应头，不在模板）。

---

## 落地记录

| 条目 | 状态 | commit | 说明 |
|---|---|---|---|
| T2 上传落盘非原子 | ✅ 已完成 | p1 `c086b66` | `write_bytes` → 同目录 `.incoming` 临时文件 + `os.replace`（原子；且**不涉及删除** —— 本机删除有钩子，见 AGENTS.md §2.9）；文件名归一化改复用 `src/paths.py:safe_target_name`，全仓只留一处实现。回归测试 3 条。p2 的写入走 `session.replace_files`，**不在本条范围**（待核它是否也需原子化）。 |
| T7 重置口令未置首登改密 | ✅ 已完成（范围 A） | p1 `5e1516c` / p2 `800dfa3` | 生成式与显式指定两条路径都置 `must_change_password=True`。**刻意不做强度校验**：安全边界在闸门上、不在初始口令的强度上，加强度校验会破坏"管理员下发口头临时码"这个合理场景。既有断言一条未改（重置后**登录**仍 200，闸门拦的是业务接口）；新增 2 条覆盖全链路。前端管理台重置抽屉补了"首次登录必须先修改密码"。 |
| R2 未预期异常对外呈现 | ✅ 已完成 | p1 `b982993` / p2 `974fd79`（第 1 条：请求 id 机制）+ p1 `4934b93` / p2 `bb571de`（第 2 条：统一错误结构） | 两项目统一成「**固定中文文案 + 请求 id**」，**异常细节只进日志**：p1 从 21 字节纯文本 `Internal Server Error` 升级为 JSON（`detail` + `request_id`），p2 去掉 `f"服务器内部错误：{exc!s}"` 的泄露。id 由新增的 `src/request_id.py` 提供（两仓库逐字相同）：`X-Request-ID` 外部可传但走 `^[A-Za-z0-9._-]{1,64}$` 白名单，非法值不回声，缺失则现生成。三条探针事实值得记住：① 错误路径上异常处理器**读不到**中间件的 ContextVar（`finally` 已复位），id 的真相只在 `request.state`；② 处理器必须**显式** `headers=`，中间件没机会给 500 响应加头；③ 测试必须 `TestClient(app, raise_server_exceptions=False)`。字段名沿用 `detail`（前端已按它解析），但语义从"异常详情"反转为"固定文案 + 可上报 id"。测试 +1/项目（471 / 661）：p1 新增 `tests/test_error_contract.py`，p2 新增 1 条并**改写** `tests/test_server.py:413` —— 原断言 `assert "boom" in body["detail"]` 钉死的正是旧泄露行为，见 AGENTS.md §5.4。 |
| （非清单条目）演示期新发现的三处缺陷 | ✅ 已完成 | p2 `3c76c2f` / `b47bfb9` / `2f1f280` / `0a42b48`（p1 无对应改动） | 都是 2026-09-15 在第二个项目网页上实测发现的，**不属于模块 12 原清单**：① **本地执行器下提示词仍教模型用 `/data`** → 首次读文件必失败、模型满盘找文件撞上 30 秒超时、8 步预算烧光（`3c76c2f`，机制与教训见 AGENTS.md §2.5）；② **产物图一律 401**：`<img src>` 不会带 Bearer 而接口要登录态，页面全是破图（`b47bfb9`，改走带 token 取回 → blob URL）；③ **画图字体按模式给说法**：本机 YaHei 排最前、容器别自己设（`2f1f280`），外加从 stderr 发现缺字形的运行时兜底（`0a42b48`）。 |
| T9 幽灵文档（向量库残留） | ✅ 已完成（建议 ①②） | p1 `07e1440` | 新增 `src/reconcile.py`：启动时按用户清掉「清单里没有、向量库里还留着」的 `source` 并打警告日志（`reconcile_all_vectors(all_user_ids())` 接在 lifespan 的 `init_db()` 之后；单账号异常只记日志，绝不让启动失败）。`src/store.py` 新增 `existing_collection()` —— 对账必须用**不创建集合**的读取方式：真机验证时正是 get_or_create 把从未上传过文档的账号的 `user_3_docs` 建了出来（空集合已删，并补了断言）。`docs/DEPLOY.md` 新增「清库清单」（`storage/chroma.sqlite3` 连同同目录的 UUID 子目录）。测试 +15（`tests/test_reconcile.py`，全量 507）。**真机验证**：清理前 `user_2_docs` = `FX.pdf` 6 + `p1-upload.pdf` 6 + `e2e-p1-upload.pdf` 6，启动后日志报「已清理幽灵向量 2 个」、库内只剩 `FX.pdf` 6 条，再次启动幂等且集合数不变。附带项（`scripts/e2e_real.ps1` 跑完自清）**明确未做**：这类残留现在由启动对账兜住，真要「跑完立刻干净」再单独立项。 |
