# 能力清单与接口协议

禅道 Bug / 任务 自动修复闭环系统（本机 `<项目目录>`）对外提供的能力、调用方式与协议约定。
面向三类使用者：**主人（Web 页面）**、**AI/Trae（CLI）**、**二次开发（SQLite + Flask 路由）**。
条目有两种：**缺陷（bug）** 与 **任务（task）**，同一条流水线，唯一差别是任务第一轮只交方案（G13）。

---

## 1. 能力总览

| 领域 | 能力 | 入口 |
| --- | --- | --- |
| 禅道接入 | 会话登录（绕过被网关拦截的 `api.php`）、按 `assignedTo=我 + active` 分页拉 bug、**同样拉指派给我的 task**（默认 `wait,doing`，见 `ZENTAO_TASK_STATUS`）、拉产品列表与产品名。任务列表不带产品号，逐条 `/task-view-<id>.json` 从动作记录 `",25,"` 里解出所属产品（产品决定落哪个仓库），解不出产品的任务不入库 | `sync [--kinds]` / `/sync` |
| 禅道详情 | 逐条抓 `/bug-view-{id}.json` 或 `/task-view-{id}.json`：完整重现步骤（含图片）、备注/操作记录、附件；**任务的正文其实挂在关联需求上**（`storySpec`），抓详情才拼得全（正文里标 `[需求说明 storySpec]`）；图片下载落盘并改写成本地路径（任务落 `attachments/zentao-T<id>/`，与同号缺陷不撞）；**落盘时量像素尺寸**，1x1 之类的占位图标 `unreadable` 并移出「AI 必须看图」清单（喂给模型会 400 打断整轮） | `detail [<ID>\|T<ID>]` / `img-gate` / `/bug/<id>/detail?kind=` |
| 镜像草稿通道 | 执行器看镜像、开/切分支、落草稿提交、跑完对账全走本系统命令（内部是 SSH 侧带 `core.ignorecase=false` 的 git），不必自己拼裸 `ssh … git …`，也就少一处会冻住无人值守的授权弹窗。工作区清洁度只认**真实改动**：这些镜像常年带 90+ 行结构性噪音（`.trae/` 符号链接树、第三方树的 CRLF 与 `$Id$` 差异），按「有输出即脏」拦会让第一条 bug 都开不了工 | `mirror` / `checkout` / `draft` / `reconcile`（`AUTO_LOOP.md` §1.8） |
| 交接文件自回收 | 执行器把长文本写进 JSON 交给 `--analysis-file` / `--block-file`，命令**入库成功后由本系统删掉那份文件**（返回 `handoff_removed`），跑完再统一 `tmp-clean` 清场。删除动作是无人值守最常弹授权、最容易冻住整轮的一步，这条把它从执行器的动作表里去掉；分析没入库的那份 `a_<ID>.json` 一律留着只报告 | `--keep-handoff` / `tmp-clean` / `report` 的 `handoff_leftovers` |
| 禅道回填 | 提交后自动评论（分支 + 修订号 + 待审查）、阻塞时评论「AI 阻塞」、任务出方案时评论「AI 已给出任务方案，等主人确认」；**不提供** resolve/close；缺陷与任务分别走 `/bug-comment-<id>` 与 `/task-comment-<id>`（后者会被禅道用 200 + `user-deny` 页拒绝，系统识别拒绝页、不再假报成功） | `commit` / `block` / `plan` / `comment` |
| 分析结论落库 | 每条 bug 收尾必须写回结构化分析（现象/根因/定位依据/链路/改动/影响面/验证/未验证/回退/结论 + 闸门逐条），`REQUIRE_ANALYSIS` 让无分析的 commit 直接失败 | `analyze` / `commit --analysis-file` / `/review` |
| 陈旧 index.lock 自回收 | 会话被 IDE 崩溃/模型报错打断在写索引中途，会留下 0 字节 `.git/index.lock`，git 自己不清，此后该镜像上每条 bug 都撞同一堵墙（实测一天三次、最长躺 8 小时）。写类命令（`checkout`/`draft`）自动回收，条件三者同时成立：恰好 0 字节、本机 `pgrep -x git` 为 0、已静默 `LOCK_STALE_MINUTES`（默认 5 分钟）。非 0 字节 / 太新 / 有活进程一律不动，只拒绝并给出该删的文件；`mirror` 永远只报告。`LOCK_STALE_MINUTES=0` 关闭自动回收 | `lock.state` / `lock.swept` |
| 任务编排 | SQLite 队列 + 7 态状态机，缺陷与任务**共用同一套状态与同一条队列**；优先级 = 已答复/已确认方案 > 已打回 > 待处理（任务首轮标 `plan_needed`），再 pri 升序 / severity 降序（任务无 severity）；已是终态（`closed`/`merged`）或待审查的条目不会因历史阻塞项有答复而被重新排队 | `tasks [--kinds]` / `claim` / 看板 |
| 任务两轮流程（G13） | 禅道任务第一轮**只出方案**：`plan T<id>` 落 `task_needs(need_kind='plan')` + 置 `need_solution` + 禅道留言；主人在 `/need` 确认（`owner_reply`）后条目回队列（`queue_kind=plan_approved`），才允许 `checkout` / `draft` / `commit`。**未确认时这三条命令与 `app/svn_promote.py` 都直接拒绝**（代码级闸门，不靠执行器自觉）；任务草稿分支 `bugfix/zentao-T<id>`、提交说明 `feat #<id>`，与同号缺陷在镜像里不撞车 | `plan` / `/need` / `status` 的 `plan_approved` / `AUTO_LOOP.md` G13 |
| 结论式结案（人工） | 需方案的答复允许**不是方案**：`/need` 或详情弹窗选「无法重现 / 不是缺陷 / 重复单 / 禅道已关闭」直接结案，条目停止下发、阻塞项一并收口、留一条 `author=owner` 的人工分析；不删草稿分支、不动 trunk，之后仍可重开补修。执行器无此能力（G11） | `/bug/<id>/close` |
| 无人值守连跑 | 一条条目跑完自动取下一条、卡点回填后不阻塞、任务方案登记后不空等、断线可续跑；下达「提交闸门 G1–G13」清单 | `AUTO_LOOP.md` |
| 代码库知识体系优先 | 目标库把 `AGENTS.md` + `.trae/`（`agents/` 目录级知识、`doc/ARCHITECTURE.md`、`skills/` 流程）提交在 SVN 里，随 git-svn 镜像一起落下来；执行器定位前必须按其入口下钻，得出可复用结论时按同规则追加回目录级 `AGENT.md` | `AUTO_LOOP.md` §1.6 |
| 一键下达 | Web 页现场从 `AUTO_LOOP.md` 抽取提示词（永不与规则分叉）+ 开跑前检查清单 + 按落码目录归组的串行队列；可勾选产品生成已填好产品 ID / 目录 / 串行要求的下达语，每段一个复制按钮 | `/dispatch` |
| 多代码库 | 一个禅道产品 = 一个 SVN 仓库；仓库归属用于闸门判定与审查展示，工作副本默认按产品独立（共用同一镜像的产品必须并入同一串行队列） | `repos` / `bind-repo` / `/repos` |
| 多语言直连提交（G12） | 词条文件（`I18N_FILE_PATTERNS` 白名单）不草稿化、不排队：改前系统代跑 `svn update`，改完单独 `svn ci` 进正式库并登记真实 r 号；夹带非白名单文件直接拒绝 | `i18n-up` / `i18n-commit` |
| SVN 操作（可选方式） | 建/切分支（`svn copy trunk→branches`，自动补 `/branches` 根）、提交、状态、diff、log、info —— **默认不启用**，提交动作归目标代码库自己的提交细则 | `branch` / `commit --no-svn` |
| 安全护栏 | trunk 写保护、禁止 AI 结案、工作副本归属校验、`.env` 密码脱敏、只读诊断 | 内置 + `svn-check` |
| 诊断 | 禅道分步诊断（配置/站点/会话/端点/产品/Bug）、会话原始样本落盘、SVN 逐仓库自检 | `doctor` / `session-probe` / `svn-check` |
| 管理平台 | 7 个页面：看板、禅道同步、需方案、审查、SVN 记录、产品仓库、配置 | `http://127.0.0.1:5000` |

## 2. CLI 接口协议（AI/Trae 使用）

调用形式：`python -m app.cli <命令> [参数]`，工作目录为项目根。

**输出约定**

- 成功：stdout 输出**单个 JSON 对象**，`ok: true`（部分只读命令带 `action`），退出码 `0`
- 失败：stderr 输出 `{"ok": false, "error": "中文原因"}`，退出码 `1`
- `ref` 参数同时接受**禅道 ID**与**本地主键**，优先按禅道 ID 解析
- **编号写法区分两种条目**：缺陷写纯数字（`5417`），任务必须带前缀（`T5417`，也接受 `t5417`/`B5417`）。
  禅道的 bug 与 task 各自独立编号，`5417` 与 `T5417` 是两条不同条目、落在两张表；写错种类会直接报
  「找不到条目」而不会静默改到同号缺陷。裸数字按缺陷解析
- 多条目命令（`sync` / `detail` / `tasks` / `reconcile`）带 `--kinds bug,task`（默认两种都跑）；
  单条目命令不需要 `--kinds`，种类由 `ref` 的前缀决定，输出的 `kind` 字段回显实际种类
- 时间统一为本地时区 `YYYY-MM-DD HH:MM:SS` 字符串

**命令一览**

| 命令 | 参数 | 副作用 | 关键输出字段 |
| --- | --- | --- | --- |
| `init` | — | 建库建表 | `db`, `tables` |
| `sync` | `--details N`（`-1`=全量）、`--kinds bug,task` | 读禅道 + 写 `bugs`/`tasks`/`sync_log`（两种条目各自拉取，**一种失败不拖另一种**，逐类结果在 `by_kind`） | `created`, `updated`, `skipped`, `total`, `by_kind`（逐类计数或该类 `error`）, `products`, `errors`（`kind: 原因` 形式）, `details` |
| `detail` | `<禅道ID>\|T<任务ID>` \| `--limit N` \| `--all`，`--kinds`，`--no-images` | 读禅道详情 + 下载附件（**落盘时量图片尺寸**，1x1 之类标 `unreadable`）+ 写库；任务的正文取自关联需求 `storySpec`，附件落 `attachments/zentao-T<id>/` | 单条：`kind`, `steps_length`, `comments`, `images`, `images_saved`, `images_unreadable`, `images_failed`, `module`, `product_name`, `bug`；批量：`target`, `done`, `failed`, `images_saved`, `comments`, `errors` |
| `img-gate` | `--check`（只报告） | 离线重量已下载附件的像素尺寸并回写 `attachments`，不连禅道 | `bugs_scanned`, `bugs_updated`, `pictures_flagged`, `applied` |
| `tasks` | `--limit N`（默认 5）、`--kinds bug,task` | 只读 | `count`, `tasks[]`（每条带 `target`=`bug\|task`、`zentao_id`、`queue_rank`、`queue_kind`、`open_need_id`、任务另带 `plan_approved`；`queue_kind` 取值 `plan_approved / replied / reopened / rejected / pending / plan_needed / stale`。**打回项必带 `reject_reason` + `prior_fix`**：既被打回又留着已答复阻塞项时，标签按 `rejected`/`reopened` 算，`owner_reply` 同时下发，以更晚的打回原话为准） |
| `claim` | `<ref>` | 状态 → `fixing` | `kind`, `zentao_id`, `bug` |
| `branch` | `<ref>`，`--dry-run` | 状态 → `fixing` 并记分支；真实执行时 `svn copy` + `switch`；任务分支名 `bugfix/zentao-T<ID>` | `kind`, `branch`, `product_id`, `product_name`, `repo`, `repo_source`, `trunk`, `working_copy`, `svn` |
| `mirror` | `<ref>` | **只读**探测镜像上这条的草稿分支（SSH 侧，不 fetch 不 checkout、不删锁）；工作区脏不脏只判**真实改动**，该库常年结构性噪音另计；任务不受 G13 限制（只读不写码） | `kind`, `status`, `present`, `tip`, `subject`, `ahead`, `base`, `base_source`, `commits[]`, `files[]`, `mirror_head`, `trunk_head`, `mirror_ready`, `index_ok`, `worktree_clean`, `dirty_real`, `dirty_noise`, `dirty_untracked`, `dirty_paths`, `dirt{state,git,why}`, `lock{state,size,age_seconds,git_procs}`, `summary` |
| `checkout` | `<ref>` `--base` | 在镜像里建/切这条的 `bugfix/zentao-<ID>`（任务为 `bugfix/zentao-T<ID>`，默认基线 `refs/remotes/origin/trunk`）；状态 → `fixing` 并记分支。镜像未就绪、工作区有别人未提交的**真实**改动、或**工作区状态读不出来**（`dirt.state=failed`，未知不当干净）→ 拒绝且不改状态（噪音不拦：未跟踪、`.trae/**`、CRLF/`$Id$` 类差异一律放行）；git 本体拒绝切分支时它的 stderr 原文随 `error` 返回；上一轮崩掉的 0 字节陈锁自动回收；**任务未确认方案 → 拒绝（G13）**，`error` 里带可直接执行的 `plan` 命令 | `kind`, `branch`, `mode`(created\|switched\|already), `base`, `tip`, `status`, `dirty_real`, `dirty_noise`, `dirty_untracked`, `dirty_paths`, `dirt{state,git,why}`, `lock{...,swept}`, `summary`, `error` |
| `draft` | `<ref>` `--message`（必填，缺陷须以 `fix #<ID>` 开头、任务须以 `feat #<ID>` 开头） `--files`（必填，逗号分隔） | 在镜像的这条分支上 `git add` 清单内文件并 `git commit`（**只本地，不推任何远端**）。分支不存在 / HEAD 不在这条分支 / 路径越界 / 暂存为空 → 拒绝；陈锁同样先自动回收；**任务未确认方案 → 拒绝（G13）** | `kind`, `commit`（短哈希，登记时写成 `--revision git:<哈希>`）, `staged[]`, `subject`, `nothing`, `lock{...,swept}`, `summary`, `next` |
| `reconcile` | `[禅道ID\|T<任务ID>]` `--apply` `--kinds` | 对账「镜像上有草稿提交、库里没登记」；默认只报告，`--apply` 才登记 `git:<哈希>` + 写回分支与文件清单 + 状态 → `await_review` + 追加一条 `kind=manual` 分析 | `scanned`, `apply`, `unregistered[]`（`tip`/`subject`/`ahead`/`files`/`applied`）, `skipped[]` |
| `analyze` | `<ref>` `--kind[commit\|block\|manual\|plan]`（`plan` 仅任务） `--analysis-file <json>` `--analysis-stdin` `--keep-handoff`，或单字段 `--symptom --root-cause --evidence --chain --change --impact --verify-result --unverified --rollback --conclusion --gates "G1=pass,G5=未验证:xxx"` | 写 `analyses`/`task_analyses`（一条条目可累积多份）；**入库成功后回收 `--analysis-file` 那个文件**（除非 `--keep-handoff`） | `analysis_id`, `kind`, `gates`, `handoff_removed[]`, `analysis` |
| `note` | `<ref>` `--summary` `--verify` `--files`（也可带全套分析参数） | 只写说明字段，不改状态；带分析则追加一份分析记录 | `kind`, `bug`, `analysis_id` |
| `commit` | `<ref>` `--message`（缺陷 `fix #<ID>`、任务 `feat #<ID>` 开头） `--files` `--revision` `--summary` `--verify` `--author` `--extra` `--need-done <id>…` `--no-svn` + 全套分析参数 | 存分析 + 写 `svn_revisions`/`task_revisions` + 状态 → `await_review` + 禅道评论（缺陷 `/bug-comment`、任务 `/task-comment`）；**不带 `--no-svn`/`--revision` 时才真的跑 `svn commit`**；`REQUIRE_ANALYSIS=true` 时无分析直接失败；**任务未确认方案 → 拒绝（G13）**；成功后回收交接文件 | `kind`, `revision`, `branch`, `repo`, `repo_source`, `working_copy`, `status`, `analysis_id`, `analysis_fields`, `handoff_removed[]`, `zentao_comment`, `note` |
| `i18n-up` | `<ref>` `--files`（逗号分隔，留空=整个正式副本） | 只读地跑 `svn update`（G12 的「改前先 up」）；文件必须全部命中 `I18N_FILE_PATTERNS` | `working_copy`, `updated[]`, `conflict`, `output`, `note` |
| `i18n-commit` | `<ref>` `--files`（必填） `--message` `--author` `--branch` `--extra` `--dry-run` | 校验白名单→`svn update`→`svn commit`（只提这几个文件，含 trunk）→ 写 `svn_revisions`（`r<号>`，branch 默认 `i18n-direct`）+ 禅道评论；**不改 bugs.status** | `revision`, `committed_files[]`, `skipped_unchanged[]`, `working_copy`, `url`, `patterns`, `revision_record`, `status`, `zentao_comment` |
| `block` | `<ref>` `--question`（或整份写进 `--block-file <json>`：`question`/`options`/`advice`，可再带 `analysis_file`）`--options` `--advice` + 全套分析参数 | 写 `need_solution`/`task_needs`（`need_kind='block'`）+ 状态 → `need_solution` + 禅道评论「AI 阻塞」；带分析则同时存一份分析；成功后回收交接文件 | `action`=`block`, `kind`, `need`, `status`, `analysis_id`, `handoff_removed[]`, `zentao_comment` |
| `plan` | `T<禅道ID>` `--question`（需求理解 + 修改细则；或整份写进 `--plan-file <json>`）`--options` `--advice` + 全套分析参数 | **任务首轮的唯一产出**：写 `task_needs(need_kind='plan')` + 状态 → `need_solution` + 禅道评论「AI 已给出任务方案，等主人确认」；条目此后**不进队列**，直到主人在 `/need` 确认；对缺陷使用会被拒绝 | `action`=`plan`, `kind`, `need`, `status`, `analysis_id`, `handoff_removed[]`, `zentao_comment` |
| `tmp-clean` | `--apply` | 回收执行器留在项目根的交接文件（`a_<数字>.json` / `a_T<数字>.json` / `blk_<数字>.json` / `py_<数字>.py` / `_tmp_*`）；**分析未入库的写稿一律留着只报告**，`.env`、库文件、源码不在候选里；默认 dry-run | `apply`, `removed[]`, `left[]`, `kept_unregistered[]`, `bytes_freed`, `note` |
| `need-done` | `<need_id>`（需方案记录主键；缺陷侧记录写数字、任务侧记录写 `T<id>`） | 阻塞项/方案项 → `done`（按前缀选表） | `need` |
| `comment` | `<ref>` `--text` | 仅回写禅道评论（按条目种类选端点） | `kind`, `result{ok,detail}` |
| `status` | `<ref>` | 只读 | `kind`, `bug`（全文 + `steps`/`comments`/`attachments`/`product_*`/`files_changed`/历史/`analysis`+`analyses`；任务另带 `project_name`/`story_title`/`deadline`/`plan_approved`） |
| `report` | — | 只读 | `counts`（缺陷+任务合并计数）, `submitted_await_review[]`（任务写成 `T<id>`）, `need_owner_solution[]`, `resumed_after_reply[]`（任务侧记录带 `T` 前缀）, `remaining_queue[]`（含 `kind`、`queue_kind`）, `remaining_work` |
| `repos` | — | 只读 | `count`, `global_repo`, `unbound_products`, `repos[]`（`product_id`, `product_name`, `bugs`, `tasks`, `pending`, `task_pending`, `bound`, `repo_url`, `effective_source`, `trunk`, `branch_root`, `working_copy`, `svn_working_copy`, `build_command`, `test_command`） |
| `bind-repo` | `<产品ID> [仓库地址]` `--name` `--trunk-path` `--branch-root` `--working-copy` `--svn-working-copy` `--branch-prefix` `--build` `--test` `--note` `--disabled` `--unbind` | 写/删 `product_repos` | `repo`, `trunk`；解绑时 `unbind`, `note` |
| `svn-check` | `--all` \| `--product <pid>` | 只读（`svn --version` / `svn info`） | `ok`, `steps[]{name,ok,detail}`（含「多语言工作副本」一步）, `hint`（`ok` 为 `false` 是失败，`null` 是提示） |
| `doctor` | — | 只读探测禅道 | `ok`, `auth_mode`, `steps[]`, `hint` |
| `session-probe` | — | 登录并抓原始样本到 `session_dump/` | `steps[]`, `files[]` |
| `config` | — | 只读 | 脱敏配置 |
| `demo` | `--clear` | 写入/清除 `[DEMO]` 演示数据 | `action`, `count` |

## 3. Web 接口协议（`http://127.0.0.1:5000`）

页面路由返回 HTML；带 `POST` 的表单路由成功后 `302` 回来源页并用 flash 消息说明结果。

**种类参数**：凡按本地主键定位条目的路由都接受 `kind=bug|task`（query 或表单字段均可，缺省 `bug`，
所以历史缺陷链接一字未改仍可用）。同一主键在 `bugs` 与 `tasks` 里是两条不同条目，
**操作任务时忘带 `kind=task` 会命中同号缺陷**，因此页面上所有表单都由模板自带该参数（`app.js` 统一拼接）。

| 方法 | 路径 | 用途 | 入参 |
| --- | --- | --- | --- |
| GET | `/` | 看板（7 列状态 + 产品标签 + 截图画廊；「全部 / 只看缺陷 / 只看任务」三档，任务卡片带紫色「任务」徽标、编号写成 `T<id>`） | `kind=all\|bug\|task` |
| GET | `/api/bug/<id>` | **单条条目完整 JSON**（本地主键） | `?kind=task` 取任务 |
| GET | `/sync` | 同步页：统计（缺陷数 + 任务数）、日志、待抓详情数 | — |
| POST | `/sync` | 立即同步禅道条目（勾选决定同步哪几种） | `kinds`（复选 `bug`/`task`） |
| POST | `/sync/details` | 批量抓截图/备注/附件 | `limit`（`0` 或空 = 全量）、`kinds` |
| GET | `/sync/doctor` | 页面内展示禅道分步诊断 | — |
| GET | `/need` | 需方案 + 待确认方案清单（两种条目合表，按徽标区分「阻塞」与「方案」） | `status=awaiting\|replied\|done\|all`、`kind=all\|bug\|task` |
| POST | `/need/<id>/reply` | 主人答复阻塞项，或**确认任务方案**（确认后任务才回队列） | `owner_reply`、`kind` |
| POST | `/need/<id>/done` | 关闭阻塞项/方案项 | `kind` |
| GET | `/review` | 审查队列（每条含 AI 写回的分析结论与闸门逐条着色） | `kind=all\|bug\|task` |
| POST | `/review/<bug_id>/pass` | 审查通过 → `merged` + 禅道评论 | `merged_revision`、`kind` |
| POST | `/review/<bug_id>/reject` | 打回 → `rejected` 重回队列 + 禅道评论；**不回滚改动**，AI 在同一草稿分支上继续改 | `reject_reason`（必填）、`kind` |
| POST | `/bug/<bug_id>/reopen` | **重开已合入/已结案的条目**：状态回到 `rejected` 重新入队，上一轮的结论、改动文件与修订号作为 `prior_fix` 随任务下发给 AI；已登记的 r 号与草稿分支都保留，trunk 不回退 | `reject_reason`（必填，写清哪里不完全）、`kind` |
| POST | `/bug/<bug_id>/reject-rollback` | 拒绝该修改：删除这条的草稿分支（提交先存进 `refs/rejected/<分支>` 以便取回）→ `closed` + 留痕 + 禅道评论；镜像 HEAD 正停在该分支时拒绝执行且不改状态 | `reject_reason`（必填）、`kind` |
| POST | `/bug/<bug_id>/svn-push` | 主人一键把该条的 git 草稿正式推入 SVN（`mode=dry` 只预检不写入）；成功登记真实 r 号 → `merged`，失败只追加人工留痕、状态与 `git:<哈希>` 记录不动。trunk 已被他人改动时预检会给出「谁改的 r 号」与可无损对齐的文件清单，`align=1`（弹窗勾选）对这些文件做三方合并后再推，重叠或增删仍整笔拒绝。**任务方案未经确认时这一层也会拒绝**（G13） | `message`（正式 svn log）、`mode`（`push`/`dry`）、`align`（可选 `1`）、`kind` |
| POST | `/bug/<bug_id>/close` | 人工结案 → `closed`。两种收尾：`merged` 是入 trunk 后的登记；`need_solution` / `pending` 是**答复=结论**（无法重现 / 不是缺陷 / 重复单 / 禅道已关闭 / 其他），这条不再下发给 AI、未答复的阻塞项一并标记已处理、追加一条 `author=owner` 的人工留痕（写明「未验证：主人判定不修」）、禅道留言。不删草稿分支、不动 trunk，事后仍可「重开补修」；`fixing` / `await_review` / `rejected` 拒绝直接结案并提示该走哪条 | `resolution`、`reason`（可空）、`kind` |
| POST | `/bug/<bug_id>/detail` | 抓单条截图/备注/附件 | `kind` |
| GET | `/files/<ref>/<filename>` | 输出已下载的禅道附件（图片/文件）；`ref` 为纯数字或 `T<id>`（任务附件目录 `zentao-T<id>/`），非法字符 404 | — |
| GET | `/svn` | 按条目分组的提交记录（任务组显示 `T<id>`） | — |
| GET | `/svn/check` | SVN 自检结果 | `all=1` 全部仓库；`product=<pid>` 单产品 |
| GET | `/repos` | 产品 ↔ 仓库列表与绑定表单（每个产品分别显示缺陷数 / 待处理 / 任务数 / 待出方案） | `edit=<pid>` 预填编辑 |
| POST | `/repos/bind` | 保存绑定 | `product_id`、`repo_url`（必填）+ 其余字段，`enabled` 复选 |
| POST | `/repos/<pid>/unbind` | 解绑 | — |
| GET | `/config` | 当前配置（密码脱敏） | — |
| GET | `/config/reload` | 重读 `.env` + 迁移表结构 | — |
| GET | `/healthz` | 健康检查（`counts` 为缺陷 + 任务合并计数） | — |

机器可读输出示例：

```
GET /healthz -> {"ok":true,"db":"C:\\...\\bug_system.db","counts":{"pending":103,...}}
GET /api/bug/9 -> {"id":9,"target":"bug","zentao_id":51579,"product_id":25,"product_name":"DPDKUAC&NGFW",
                   "steps":"...","comments":[...],"attachments":[...],"status":"pending",...}
GET /api/bug/1?kind=task -> {"id":1,"target":"task","zentao_id":3970,"title":"...",
                   "project_name":"...","story_id":...,"plan_approved":false,"status":"pending",...}
```

无鉴权：仅监听 `127.0.0.1`，面向本机单人使用；如需对外，请自行加反代 + 认证。

## 4. 数据协议（SQLite `bug_system.db`）

条目有**两种、两套表**：缺陷落 `bugs` 族，任务落 `tasks` 族，`zentao_id` 在各自表内 UNIQUE
（禅道 bug 与 task 编号互相独立，`5417` 缺陷与 `T5417` 任务可并存）。
两套表结构同构，读写侧由 `db.ItemTables` / `db.tables_for(target)` 选表，业务代码只传 `target`。

| 表 | 作用 | 关键列 |
| --- | --- | --- |
| `bugs` | 缺陷主表（禅道镜像 + 本地工作流） | `zentao_id` UNIQUE、`status`、`pri`、`severity`、`product_id`/`product_name`、`steps`/`steps_html`/`comments`/`attachments`、`branch`、`fix_summary`/`verify_steps`/`files_changed`、`detail_synced_at`、`raw_json` |
| `tasks` | 任务主表（结构与 `bugs` 同构，无 `severity`；多了执行上下文） | `zentao_id` UNIQUE、`status`（同一套 7 态枚举）、`pri`、`product_id`/`product_name`（**由 `/task-view-<id>.json` 的动作记录反解，列表里没有**）、`steps`（= `desc` + `[需求说明 storySpec]`，任务的真正正文在关联需求上）、`project_id`/`project_name`、`story_id`/`story_title`、`deadline`、`estimate`、`need_confirm`、`branch`、`comments`/`attachments`、`detail_synced_at`、`raw_json` |
| `product_repos` | 产品 → 代码库 | `product_id` PK、`repo_url`、`trunk_path`、`branch_root`、`working_copy`（= 执行器改码的 git 镜像目录）、`svn_working_copy`（= 正式 SVN 工作副本，只有 G12 多语言通道可写）、`branch_prefix`、`build_command`、`test_command`、`enabled` |
| `analyses` / `task_analyses` | 执行器写回的分析结论（一条条目可多份） | `kind[commit\|block\|manual]`，任务侧另有 `plan`（首轮方案的文字依据）、`symptom`、`root_cause`、`evidence`、`call_chain`、`change_desc`、`impact`、`verify`、`gates`(JSON)、`unverified`、`rollback`、`conclusion`、`author` |
| `need_solution` / `task_needs` | AI 提问与主人答复 | `question`/`ai_options`/`ai_advice`/`owner_reply`/`status[awaiting\|replied\|done]`；任务侧多一列 **`need_kind`**：`block`=实施中卡住、`plan`=待确认的修改细则（G13 只认这一类且 `status='replied'` 且 `owner_reply` 非空）；缺陷表无该列，跨表读取时投影成常量 `'block'` |
| `svn_revisions` / `task_revisions` | 提交记录 | `revision`、`branch`、`message`、`author`、`files`、`git_commit` |
| `reviews` / `task_reviews` | 人工审查结论 | `result[pass\|reject]`、`merged_revision`、`reject_reason` |
| `sync_log` | 禅道读写留痕 | `action[sync\|update\|comment\|error]`、`zentao_id`、`payload`（`update`/`error` 的 payload 带 `kind`） |
| `meta` | 杂项（最近同步时间等） | `key`、`value` |

任务闸门的判定口径（`db.approved_plan` / `db.plan_gate_reason`，`checkout`/`draft`/`commit` 与 `svn_promote` 四处共用）：

```
task_needs WHERE task_id=? AND need_kind='plan' AND status='replied'
             AND owner_reply 非空            -> 放行（可落码）
存在 need_kind='plan' AND status='awaiting'  -> 拒绝：方案等主人确认
都没有                                        -> 拒绝：首轮只许给方案
```

状态机（缺陷与任务共用同一套 `status` 枚举，任务只在入口多一道 G13）：

```
pending ──claim/branch──> fixing ──commit──> await_review ──pass──> merged ──close──> closed
   ↑                        │                     │
   │                        block                 reject
   └────────────────────────┴──── need_solution ──┘（打回后重回队列）
                       need_solution ──人工答复──> replied ──> 优先处理
       pending / need_solution ──人工判定「不修了」（无法重现/不是缺陷/重复单/禅道已关闭）──> closed
                       # 这条边不产生代码改动：草稿分支与已有提交原样保留，仍可重开补修

任务（T<id>）的入口多了两跳：
  pending --首轮--> plan_needed --`plan` 登记--> need_solution(未确认，不进队列)
        --主人在 /need 确认--> plan_approved --claim/branch--> fixing --> …（此后与缺陷同路）
```

`bugs.status` / `tasks.status` 枚举：`pending / fixing / need_solution / await_review / rejected / merged / closed`。
建表与列迁移全为 `IF NOT EXISTS` / `ALTER TABLE ADD COLUMN`，不会改动已有数据；任务 5 张表是新增，
缺陷表结构一字未动。

分析结论（`analyses` 一条记录）的输入协议 —— 走 `analyze` 独立写，或随 `commit` / `block` 一起写：

```json
{
  "symptom": "现象", "root_cause": "根因", "evidence": "真实读过的文件:行/日志/复现输出",
  "call_chain": "入口 -> 中间层 -> 最终实现", "change_desc": "改了什么",
  "impact": "影响面与同构路径自审", "verify": "验证方式与实际结果",
  "unverified": "未验证项", "rollback": "回退方式", "conclusion": "一句话结论",
  "gates": {"G1": "pass", "G5": "未验证:OEM 同名页未核对"}
}
```

落库后由 `/review`、看板卡片、`GET /api/bug/<id>`、`status` 四处展示；
`REQUIRE_ANALYSIS=true` 时 `commit` 缺分析会返回 `{"ok":false,"error":"提交前必须把分析结果写入系统…","hint":…}`。

## 5. 对外依赖协议

### 5.1 禅道（`ZENTAO_AUTH_MODE`）

| 模式 | 协议 |
| --- | --- |
| `rest` | `POST /api.php/v1/tokens` 换 token → 后续请求带头 `Token:`；读 `GET /api.php/v1/products`、`/api.php/v1/products/{id}/bugs?status=active`、任务侧 `/api.php/v1/products/{id}/tasks` 与 `/api.php/v1/tasks/{id}`；写 `POST /api.php/v1/bugs/{id}/comments`、`POST /api.php/v1/tasks/{id}/comments`（版本不带该路由时用 `ZENTAO_TASK_COMMENT_PATH` 覆盖） |
| `session`（本机在用） | `GET /user-login-{code}.html` 拿 `verifyRand` → `POST /user-login.html`（字段 `account/password=md5(md5(pwd)+verifyRand)/passwordStrength/referer/verifyRand/keepLogin` + `X-Requested-With`）→ 复用 `zentaosid` cookie 调内部 `.json` 接口 |
| `cookie` | 直接用浏览器 `ZENTAO_COOKIE`，不发密码 |
| `auto` | 有 cookie 用 cookie，否则 rest；**永不自动发密码**（防锁号） |

会话模式实际端点（PATH_INFO 风格，`requestFix=-`）：

```
GET  /product-index.json                        data.products = {id: 名称}
GET  /my-bug-assignedTo-pri_asc-0-100-{页}.json  data.bugs[]（含 steps）、data.pager.pageTotal
GET  /bug-view-{id}.json                        data.bug（steps/files/module/product）、data.actions（备注）
GET  /file-read-{id}.{ext}                      附件/截图下载（会话 cookie 鉴权）
POST /bug-comment-{id}.json                     写评论（失败退回 .html，可用 ZENTAO_COMMENT_PATH 覆盖）

GET  /my-task-assignedTo-pri_asc-0-100-{页}.json  tasks[]：字段是 name/desc/pri/project/story，
                                               **没有 product**，desc 常为空
GET  /task-view-{id}.json                        payload 直接在 root（不像 bug 挂在 data 下）：
                                               task（+storySpec=真正的需求正文与图片）、
                                               actions[].product（",25," → 产品号唯一可靠来源）、
                                               modulePath[].root（兜底）、needConfirm、files、users
POST /task-comment-{id}.json                     写任务评论，随后尝试 /task-comment-{id}-{actionId}.json
```

响应信封：`{"status":"success","data":"<字符串化 JSON>","md5":...}`；密码发送有硬约束——
每个进程实例最多发一次，`auto` 模式不发密码。

**写评论的假绿坑（实测）**：任务评论被禅道拒绝时返回的是 **HTTP 200 + 一段
`self.location='/user-deny-task-comment.html'` 的跳转页**，只看状态码会把「没写进去」报成「已回填」。
因此 `zentao_session.looks_refused()` 用 `user-deny` / `self.location='/user-…'` / 「权限不足」三类特征
判定拒绝页，命中即换下一个候选端点、全部失败则报 `ZentaoError`；会话失效同样单独报错，不当作评论成功。

### 5.2 SVN

仓库解析优先级：

```
1) product_repos 里该 bug 产品且 enabled=1 -> 用产品配置
   （working_copy 留空时自动分配 svn_workspace/p<产品ID>-<仓库地址哈希8>）
2) 回落 .env 的 SVN_REPO_URL / SVN_TRUNK_PATH / SVN_BRANCH_ROOT / BRANCH_PREFIX
3) 两者都空 -> branch/commit 直接失败并打印 bind-repo 命令
```

实际调用（每条都带 `--non-interactive`，配了 `SVN_USERNAME` 时带 `--username/--password/--no-auth-cache`）：

```
svn --version --quiet
svn info <repo> | <repo>/trunk | <repo>/branches | <工作副本>
svn copy <trunk> <branches>/bugfix/zentao-<ID> -m "..."     # 缺 /branches 时先 svn mkdir
svn switch <branch url> <工作副本>
svn checkout <trunk> <工作副本>                              # 工作副本不存在时
svn commit -m "fix #<ID> <说明>" [文件...]
svn update [文件...]                                        # 仅 i18n-up / i18n-commit 用到
svn st / diff / log -l N
```

约束：分支名 = `branch_prefix + 条目编号`（缺陷 `bugfix/zentao-5417`、任务 `bugfix/zentao-T5417`，
`T` 前缀让同号缺陷与任务在同一个镜像里各占一条分支，历史缺陷分支名一字未改）；
`SVN_ALLOW_TRUNK_WRITE=false` 时提交目标指向 trunk 直接报错
（**唯一例外**：`i18n-commit` 提的是 `I18N_FILE_PATTERNS` 白名单词条文件，允许落在副本当前路径含 trunk）；
工作副本 URL 与产品仓库不匹配时自检 FAIL；输出解码按 UTF-8 → GBK 回退（中文 Windows 的 svn 输出为 GBK）。

多语言通道（G12）的调用面被刻意收得很窄：只有 `i18n-up` / `i18n-commit` 两条命令会跑
`svn update` / `svn commit`，且 commit 永远带显式文件列表（不整目录提交）、
路径必须落在该产品的 `svn_working_copy` 内、每个文件都必须命中白名单，任一不满足即拒绝且不提交。

## 6. 环境与健康

- 依赖：Python 3.11、Flask 3、requests、python-dotenv；`svn` 命令行（本机 1.6.16-SlikSvn，注意 1.7+ 工作副本不兼容）
- 启动：`start_web.bat`（或 `python run_web.py [--port 8811] [--sync] [--no-browser]`）；端口占用时提示已运行并打开页面
  —— **启动时 `db.init_db()` 会建出任务那 5 张表**，老库直接重启即可，缺陷数据不动
- 与任务有关的两个可选项：`ZENTAO_TASK_STATUS`（默认 `wait,doing`，白名单外的任务不入库；禅道任务状态与
  缺陷状态是两套列表，别拿 `active` 填）与 `ZENTAO_TASK_COMMENT_PATH`（任务评论端点与默认候选不符时覆盖，
  占位符 `{item_id}`/`{task_id}`/`{bug_id}` 均可）；两者留空即用默认值，`/config` 页可见
- 自检顺序建议：`doctor` → `sync` → `detail --all` → `repos` → `svn-check --all`；
  确认任务通道用 `sync --kinds task` + `tasks --kinds task`（队首应出现 `queue_kind=plan_needed`）
- 敏感信息：`.env` 已被 `.gitignore`；`config`/`/config` 输出对密码脱敏；禅道原始响应样本只落在 `session_dump/`（也已忽略）
