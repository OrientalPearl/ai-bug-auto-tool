# 能力清单与接口协议

禅道 Bug 自动修复闭环系统（本机 `<项目目录>`）对外提供的能力、调用方式与协议约定。
面向三类使用者：**主人（Web 页面）**、**AI/Trae（CLI）**、**二次开发（SQLite + Flask 路由）**。

---

## 1. 能力总览

| 领域 | 能力 | 入口 |
| --- | --- | --- |
| 禅道接入 | 会话登录（绕过被网关拦截的 `api.php`）、按 `assignedTo=我 + active` 分页拉 bug、拉产品列表与产品名 | `sync` / `/sync` |
| 禅道详情 | 逐条抓 `/bug-view-{id}.json`：完整重现步骤（含图片）、备注/操作记录、附件；图片下载落盘并改写成本地路径 | `detail` / `/bug/<id>/detail` |
| 禅道回填 | 提交后自动评论（分支 + 修订号 + 待审查）、阻塞时评论「AI 阻塞」；**不提供** resolve/close | `commit` / `block` / `comment` |
| 分析结论落库 | 每条 bug 收尾必须写回结构化分析（现象/根因/定位依据/链路/改动/影响面/验证/未验证/回退/结论 + 闸门逐条），`REQUIRE_ANALYSIS` 让无分析的 commit 直接失败 | `analyze` / `commit --analysis-file` / `/review` |
| 任务编排 | SQLite 队列 + 7 态状态机；优先级 = 已答复 > 已打回 > 待处理，再 pri 升序 / severity 降序 | `tasks` / `claim` / 看板 |
| 无人值守连跑 | 一条 bug 跑完自动取下一条、卡点回填后不阻塞、断线可续跑；下达「提交闸门 G1–G12」清单 | `AUTO_LOOP.md` |
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
- `ref` 参数同时接受**禅道 ID**与**本地 bug 主键**，优先按禅道 ID 解析
- 时间统一为本地时区 `YYYY-MM-DD HH:MM:SS` 字符串

**命令一览**

| 命令 | 参数 | 副作用 | 关键输出字段 |
| --- | --- | --- | --- |
| `init` | — | 建库建表 | `db`, `tables` |
| `sync` | `--details N`（`-1`=全量） | 读禅道 + 写 `bugs`/`sync_log` | `created`, `updated`, `skipped`, `total`, `products`, `errors`, `details` |
| `detail` | `<禅道ID>` \| `--limit N` \| `--all`，`--no-images` | 读禅道详情 + 下载附件 + 写库 | 单条：`steps_length`, `comments`, `images`, `images_saved`, `images_failed`, `module`, `product_name`, `bug`；批量：`target`, `done`, `failed`, `images_saved`, `comments`, `errors` |
| `tasks` | `--limit N`（默认 5） | 只读 | `count`, `tasks[]`（含 `queue_rank`、`open_need_id`） |
| `claim` | `<ref>` | 状态 → `fixing` | `bug` |
| `branch` | `<ref>`，`--dry-run` | 状态 → `fixing` 并记分支；真实执行时 `svn copy` + `switch` | `branch`, `product_id`, `product_name`, `repo`, `repo_source`, `trunk`, `working_copy`, `svn` |
| `analyze` | `<ref>` `--kind[commit\|block\|manual]` `--analysis-file <json>` `--analysis-stdin`，或单字段 `--symptom --root-cause --evidence --chain --change --impact --verify-result --unverified --rollback --conclusion --gates "G1=pass,G5=未验证:xxx"` | 写 `analyses`（一条 bug 可累积多份） | `analysis_id`, `kind`, `gates`, `analysis` |
| `note` | `<ref>` `--summary` `--verify` `--files`（也可带全套分析参数） | 只写说明字段，不改状态；带分析则追加 `analyses` | `bug`, `analysis_id` |
| `commit` | `<ref>` `--message` `--files` `--revision` `--summary` `--verify` `--author` `--extra` `--need-done <id>…` `--no-svn` + 全套分析参数 | 存 `analyses` + 写 `svn_revisions` + 状态 → `await_review` + 禅道评论；**不带 `--no-svn`/`--revision` 时才真的跑 `svn commit`**；`REQUIRE_ANALYSIS=true` 时无分析直接失败 | `revision`, `branch`, `repo`, `repo_source`, `working_copy`, `status`, `analysis_id`, `analysis_fields`, `zentao_comment`, `note` |
| `i18n-up` | `<ref>` `--files`（逗号分隔，留空=整个正式副本） | 只读地跑 `svn update`（G12 的「改前先 up」）；文件必须全部命中 `I18N_FILE_PATTERNS` | `working_copy`, `updated[]`, `conflict`, `output`, `note` |
| `i18n-commit` | `<ref>` `--files`（必填） `--message` `--author` `--branch` `--extra` `--dry-run` | 校验白名单→`svn update`→`svn commit`（只提这几个文件，含 trunk）→ 写 `svn_revisions`（`r<号>`，branch 默认 `i18n-direct`）+ 禅道评论；**不改 bugs.status** | `revision`, `committed_files[]`, `skipped_unchanged[]`, `working_copy`, `url`, `patterns`, `revision_record`, `status`, `zentao_comment` |
| `block` | `<ref>` `--question`（必填）`--options` `--advice` + 全套分析参数 | 写 `need_solution` + 状态 → `need_solution` + 禅道评论；带分析则同时存 `analyses` | `need`, `status`, `analysis_id`, `zentao_comment` |
| `need-done` | `<need_id>` | 阻塞项 → `done` | `need` |
| `comment` | `<ref>` `--text` | 仅回写禅道评论 | `result{ok,detail}` |
| `status` | `<ref>` | 只读 | `bug`（全文 + `steps`/`comments`/`attachments`/`product_*`/`files_changed`/历史/`analysis`+`analyses`） |
| `report` | — | 只读 | `counts`, `submitted_await_review[]`, `need_owner_solution[]`, `resumed_after_reply[]`, `remaining_queue[]`, `remaining_work` |
| `repos` | — | 只读 | `count`, `global_repo`, `unbound_products`, `repos[]`（`product_id`, `product_name`, `bugs`, `pending`, `bound`, `repo_url`, `effective_source`, `trunk`, `branch_root`, `working_copy`, `svn_working_copy`, `build_command`, `test_command`） |
| `bind-repo` | `<产品ID> [仓库地址]` `--name` `--trunk-path` `--branch-root` `--working-copy` `--svn-working-copy` `--branch-prefix` `--build` `--test` `--note` `--disabled` `--unbind` | 写/删 `product_repos` | `repo`, `trunk`；解绑时 `unbind`, `note` |
| `svn-check` | `--all` \| `--product <pid>` | 只读（`svn --version` / `svn info`） | `ok`, `steps[]{name,ok,detail}`（含「多语言工作副本」一步）, `hint`（`ok` 为 `false` 是失败，`null` 是提示） |
| `doctor` | — | 只读探测禅道 | `ok`, `auth_mode`, `steps[]`, `hint` |
| `session-probe` | — | 登录并抓原始样本到 `session_dump/` | `steps[]`, `files[]` |
| `config` | — | 只读 | 脱敏配置 |
| `demo` | `--clear` | 写入/清除 `[DEMO]` 演示数据 | `action`, `count` |

## 3. Web 接口协议（`http://127.0.0.1:5000`）

页面路由返回 HTML；带 `POST` 的表单路由成功后 `302` 回来源页并用 flash 消息说明结果。

| 方法 | 路径 | 用途 | 入参 |
| --- | --- | --- | --- |
| GET | `/` | 看板（7 列状态 + 产品标签 + 截图画廊） | — |
| GET | `/api/bug/<id>` | **单条 bug 完整 JSON**（本地主键） | — |
| GET | `/sync` | 同步页：统计、日志、待抓详情数 | — |
| POST | `/sync` | 立即同步禅道 bug | — |
| POST | `/sync/details` | 批量抓截图/备注/附件 | `limit`（`0` 或空 = 全量） |
| GET | `/sync/doctor` | 页面内展示禅道分步诊断 | — |
| GET | `/need` | 需方案清单 | `status=awaiting\|replied\|done\|all` |
| POST | `/need/<id>/reply` | 主人答复阻塞项 | `owner_reply` |
| POST | `/need/<id>/done` | 关闭阻塞项 | — |
| GET | `/review` | 审查队列（每条含 AI 写回的分析结论与闸门逐条着色） | — |
| POST | `/review/<bug_id>/pass` | 审查通过 → `merged` + 禅道评论 | `merged_revision` |
| POST | `/review/<bug_id>/reject` | 打回 → `rejected` 重回队列 + 禅道评论；**不回滚改动**，AI 在同一草稿分支上继续改 | `reject_reason`（必填） |
| POST | `/bug/<bug_id>/reject-rollback` | 拒绝该修改：删除这条的草稿分支（提交先存进 `refs/rejected/<分支>` 以便取回）→ `closed` + 留痕 + 禅道评论；镜像 HEAD 正停在该分支时拒绝执行且不改状态 | `reject_reason`（必填） |
| POST | `/bug/<bug_id>/svn-push` | 主人一键把该条的 git 草稿正式推入 SVN（`mode=dry` 只预检不写入）；成功登记真实 r 号 → `merged`，失败只追加人工留痕、状态与 `git:<哈希>` 记录不动 | `message`（正式 svn log）、`mode`（`push`/`dry`） |
| POST | `/bug/<bug_id>/close` | 人工结案 → `closed` + 禅道评论 | — |
| POST | `/bug/<bug_id>/detail` | 抓单条截图/备注/附件 | — |
| GET | `/files/<zentao_id>/<filename>` | 输出已下载的禅道附件（图片/文件） | — |
| GET | `/svn` | 按 bug 分组的提交记录 | — |
| GET | `/svn/check` | SVN 自检结果 | `all=1` 全部仓库；`product=<pid>` 单产品 |
| GET | `/repos` | 产品 ↔ 仓库列表与绑定表单 | `edit=<pid>` 预填编辑 |
| POST | `/repos/bind` | 保存绑定 | `product_id`、`repo_url`（必填）+ 其余字段，`enabled` 复选 |
| POST | `/repos/<pid>/unbind` | 解绑 | — |
| GET | `/config` | 当前配置（密码脱敏） | — |
| GET | `/config/reload` | 重读 `.env` + 迁移表结构 | — |
| GET | `/healthz` | 健康检查 | — |

机器可读输出示例：

```
GET /healthz -> {"ok":true,"db":"C:\\...\\bug_system.db","counts":{"pending":100,...}}
GET /api/bug/9 -> {"id":9,"zentao_id":51579,"product_id":25,"product_name":"DPDKUAC&NGFW",
                   "steps":"...","comments":[...],"attachments":[...],"status":"pending",...}
```

无鉴权：仅监听 `127.0.0.1`，面向本机单人使用；如需对外，请自行加反代 + 认证。

## 4. 数据协议（SQLite `bug_system.db`）

| 表 | 作用 | 关键列 |
| --- | --- | --- |
| `bugs` | 任务主表（禅道镜像 + 本地工作流） | `zentao_id` UNIQUE、`status`、`pri`、`severity`、`product_id`/`product_name`、`steps`/`steps_html`/`comments`/`attachments`、`branch`、`fix_summary`/`verify_steps`/`files_changed`、`detail_synced_at`、`raw_json` |
| `product_repos` | 产品 → 代码库 | `product_id` PK、`repo_url`、`trunk_path`、`branch_root`、`working_copy`（= 执行器改码的 git 镜像目录）、`svn_working_copy`（= 正式 SVN 工作副本，只有 G12 多语言通道可写）、`branch_prefix`、`build_command`、`test_command`、`enabled` |
| `analyses` | 执行器写回的分析结论（一条 bug 可多份） | `kind[commit\|block\|manual]`、`symptom`、`root_cause`、`evidence`、`call_chain`、`change_desc`、`impact`、`verify`、`gates`(JSON)、`unverified`、`rollback`、`conclusion`、`author` |
| `need_solution` | AI 阻塞与主人答复 | `question`/`ai_options`/`ai_advice`/`owner_reply`/`status` |
| `svn_revisions` | 提交记录 | `revision`、`branch`、`message`、`author`、`files` |
| `reviews` | 人工审查结论 | `result[pass\|reject]`、`merged_revision`、`reject_reason` |
| `sync_log` | 禅道读写留痕 | `action[sync\|update\|comment\|error]`、`zentao_id`、`payload` |
| `meta` | 杂项（最近同步时间等） | `key`、`value` |

状态机：

```
pending ──claim/branch──> fixing ──commit──> await_review ──pass──> merged ──close──> closed
   ↑                        │                     │
   │                        block                 reject
   └────────────────────────┴──── need_solution ──┘（打回后重回队列）
                       need_solution ──人工答复──> replied ──> 优先处理
```

`bugs.status` 枚举：`pending / fixing / need_solution / await_review / rejected / merged / closed`。
建表与列迁移全为 `IF NOT EXISTS` / `ALTER TABLE ADD COLUMN`，不会改动已有数据。

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
| `rest` | `POST /api.php/v1/tokens` 换 token → 后续请求带头 `Token:`；读 `GET /api.php/v1/products`、`/api.php/v1/products/{id}/bugs?status=active`；写 `POST /api.php/v1/bugs/{id}/comments` |
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
```

响应信封：`{"status":"success","data":"<字符串化 JSON>","md5":...}`；密码发送有硬约束——
每个进程实例最多发一次，`auto` 模式不发密码。

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

约束：分支名 = `branch_prefix + 禅道ID`；`SVN_ALLOW_TRUNK_WRITE=false` 时提交目标指向 trunk 直接报错
（**唯一例外**：`i18n-commit` 提的是 `I18N_FILE_PATTERNS` 白名单词条文件，允许落在副本当前路径含 trunk）；
工作副本 URL 与产品仓库不匹配时自检 FAIL；输出解码按 UTF-8 → GBK 回退（中文 Windows 的 svn 输出为 GBK）。

多语言通道（G12）的调用面被刻意收得很窄：只有 `i18n-up` / `i18n-commit` 两条命令会跑
`svn update` / `svn commit`，且 commit 永远带显式文件列表（不整目录提交）、
路径必须落在该产品的 `svn_working_copy` 内、每个文件都必须命中白名单，任一不满足即拒绝且不提交。

## 6. 环境与健康

- 依赖：Python 3.11、Flask 3、requests、python-dotenv；`svn` 命令行（本机 1.6.16-SlikSvn，注意 1.7+ 工作副本不兼容）
- 启动：`start_web.bat`（或 `python run_web.py [--port 8811] [--sync] [--no-browser]`）；端口占用时提示已运行并打开页面
- 自检顺序建议：`doctor` → `sync` → `detail --all` → `repos` → `svn-check --all`
- 敏感信息：`.env` 已被 `.gitignore`；`config`/`/config` 输出对密码脱敏；禅道原始响应样本只落在 `session_dump/`（也已忽略）
