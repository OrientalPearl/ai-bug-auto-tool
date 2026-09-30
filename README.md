# 禅道 Bug / 任务 自动修复闭环系统

本机跑的「禅道拉取 → SQLite 排队 → AI 自动修复提交分支 → 人工审查 → 合入 trunk → 结案」闭环。
条目有两种：**缺陷（bug）** 与 **任务（task）**，走同一条流水线；唯一差别是**任务第一轮只交「需求理解 + 修改细则」，
主人在「需方案」页确认之后 AI 才允许落码**（闸门 G13，代码里硬拦，不靠 AI 自觉）。

- 服务地址：<http://127.0.0.1:5000>
- 数据库：`bug_system.db`（已建好全部表）
- AI 执行器规则：`AGENTS.md`（Trae 读它干活）
- 自动连跑下达手册：`AUTO_LOOP.md`（一条跑完自动下一条 + 提交闸门 G1–G13 + 多语言直连通道 + 可复制指令）
- 能力清单 / 接口协议：`CAPABILITIES.md`
- 账号密码配置：`.env`（已被 `.gitignore` 忽略）

**编号写法**：缺陷写纯数字（`5417`），任务一律带 `T` 前缀（`T5417`）。禅道的 bug 与 task 各自独立编号，
`5417` 与 `T5417` 是两条不同条目，落在两张表；镜像里也各占一条分支（`bugfix/zentao-5417` / `bugfix/zentao-T5417`）。

---

## 1. 一键启动 / 停止

**最省事：双击项目根目录的 `start_web.bat`** —— 它会自检 Python、自动装依赖（缺才装）、
建库、启动服务并自动打开浏览器 <http://127.0.0.1:5000>。窗口关掉即停止服务。

| 方式 | 操作 |
| --- | --- |
| 一键启动 | 双击 `start_web.bat` |
| 启动并顺带同步禅道 | 双击 `start_web_sync.bat`（或命令行 `start_web.bat sync`） |
| 命令行启动 | `python run_web.py`（带浏览器）/ `python run_web.py --no-browser` |
| 换端口 | `python run_web.py --port 8080`，或改 `.env` 里的 `WEB_PORT` |
| 停止 | 关闭那个命令行窗口，或在该窗口按 `Ctrl+C` |
| 看是否在跑 | 浏览器开 <http://127.0.0.1:5000/healthz>，返回 `"ok": true` 即在运行 |

重复双击不会报错：检测到端口被占用时只提示「系统已在运行」并直接打开页面。
改完 `.env` 后需重启一次服务（关窗口再双击）才会生效。

## 2. 配置禅道（必做，否则同步会报错）

编辑 `<项目目录>\.env`：

```
ZENTAO_BASE_URL=http://你的禅道地址        # 例 http://zentao.example.com
ZENTAO_ACCOUNT=你的账号
ZENTAO_PASSWORD=你的密码
ZENTAO_PRODUCT_IDS=                        # 留空=全部产品；也可写 1,3,7
ZENTAO_BUG_STATUS=active                   # 缺陷状态（禅道 bug 一套枚举）
ZENTAO_TASK_STATUS=wait,doing              # 任务状态是另一套枚举（wait/doing/pause/done/closed），别拿 active 填

# 执行器行为（放在同一个 .env 里）
REQUIRE_ANALYSIS=true                      # 没写分析结论不许 commit（默认 true）
AI_BATCH_SIZE=5                            # 每批处理条数
RETRY_WAIT=300                             # 限流/5xx/连不上时等这么久再重试（秒）
RETRY_MAX=3                                # 总共试几次（读类请求；评论等写动作不重试）
STALE_CLAIM_MINUTES=40                     # claim 后这么久没回音的 bug 自动回队列（0=关闭）
LOCK_STALE_MINUTES=5                       # 0 字节且静默这么久、本机又无 git 进程的 index.lock 由 checkout/draft 自动回收（0=只报告不删）
```

SVN。**默认方式下本系统不执行 svn**（提交动作由目标代码库自己的提交细则负责），这里填的仓库地址只用于：
闸门 G3 判断「改的文件属不属于这个产品」、审查页展示归属、以及你想让本系统代跑 svn 时的可选方式。
**唯一例外是多语言词条（闸门 G12）**：词条攒着不提交必然冲突，而新增词条对版本没有影响，
所以 `i18n-up`（改前 `svn update`）+ `i18n-commit`（改完单独 `svn ci`）会代跑 svn，且只允许白名单文件。
各产品自己的仓库在「产品仓库」页或 `bind-repo` 里绑定（见第 3 节）：

```
SVN_REPO_URL=https://svn.example.com/svn/project   # 全局回落：没绑定的产品用它
SVN_WORKING_COPY=<项目目录>\svn_workspace
SVN_TRUNK_PATH=/trunk
SVN_BRANCH_ROOT=/branches
SVN_USERNAME= SVN_PASSWORD=
SVN_ALLOW_TRUNK_WRITE=false                # 保持 false，AI 禁止碰主干
BRANCH_PREFIX=bugfix/zentao-               # 分支名 = 前缀 + 禅道ID（产品可各自覆盖）
I18N_DIRECT_SVN_COMMIT=true                # G12：多语言单独直连提交（false = 关掉这条例外）
I18N_FILE_PATTERNS=*.po,*.mo,*.pot,*.qm    # 白名单：只有命中的文件能走 i18n-commit
# 每个产品另配「正式 SVN 工作副本」（多语言通道的唯一可写目录）：
#   bind-repo <pid> <仓库地址> --working-copy <镜像目录> --svn-working-copy <正式SVN工作副本>
```

改完在「配置」页点 **重新读取 .env**。「禅道同步」页点 **立即同步** 验证是否通（页面上可勾选同步缺陷还是任务）：
缺陷拉 `status=active` 且 `assignedTo=我` 的 bug（等价 browseType=assigntome），每条落库 id/标题/steps/severity/pri/module/assignedTo/openedBuild；
任务拉 `ZENTAO_TASK_STATUS` 白名单内且指派给我的 task，落库 id/标题/pri/迭代/关联需求/截止时间，
正文与**所属产品**要逐条抓详情才有（列表接口不带回产品，而产品决定这条落哪个仓库）。

### 认证通道 `ZENTAO_AUTH_MODE`

禅道有两种可通的通道，按你的环境选：

| 值 | 走法 | 适用 |
| --- | --- | --- |
| `rest` | `POST /api.php/v1/tokens` 换 token，请求头 `Token:` | 标准禅道，api.php 未被拦截 |
| `session` | 模拟浏览器登录 `/user-login.html`（`md5(md5(密码)+verifyRand)` + `verifyRand`/`passwordStrength`/`keepLogin`），再读 `/xxx.json` 内部接口 | api.php 被统一登录网关拦截时（**本机实测采用**） |
| `cookie` | 直接复用浏览器复制来的 `zentaosid`，**完全不发密码** | 走 LDAP/扫码/免密 SSO，或不想让脚本碰密码 |
| `auto` | 有 `ZENTAO_COOKIE` 用 cookie，否则用 rest（**不会**自动发密码，避免误试锁号） | 默认 |

会话模式读的是 `/product-index.json`（`data.products` 是 `{id:名称}`）、
`/my-bug-assignedTo-pri_asc-0-100-{页}.json`（`data.bugs` 已内含 `steps`，无需逐条取详情），
以及任务侧的 `/my-task-assignedTo-pri_asc-0-100-{页}.json`（`tasks[]` 只有 `name/desc/pri/project/story`，**没有产品号**）。
任务的 `/task-view-{id}.json` 是必需的而非可选：正文挂在关联需求的 `storySpec` 上，
所属产品挂在 `actions[].product`（形如 `,25,`）里 —— 解不出产品的任务不入库。

> **截图与备注要单独拉**：禅道列表接口只带回文字描述，描述里的截图
> （`<img src="/file-read-xxx.png">`）和「备注/操作记录」（在 `actions` 里）必须按条抓详情。
> 看板卡片展开后有「**拉取截图 / 备注 / 附件**」按钮，「禅道同步」页有 **批量抓取详情**（条数填 `0` = 全量）；
> 命令行对应：
>
> ```
> python -m app.cli detail <禅道ID>     # 单条缺陷
> python -m app.cli detail T<任务ID>    # 单条任务（正文取 storySpec，附件落 zentao-T<id>/）
> python -m app.cli detail --limit 20   # 优先级最高的 20 条
> python -m app.cli detail --all        # 全量（所有未关闭条目逐条抓；加 --kinds task 只抓任务）
> python -m app.cli sync --details 5    # 同步 + 抓队首 5 条
> python -m app.cli sync --details -1   # 同步 + 全量抓
> ```
>
> 图片落到 `attachments/zentao-<ID>/`，任务落到 `attachments/zentao-T<id>/`（均已 gitignore，
> 同号缺陷与任务不共用目录），`steps` 里的 `[图片N: /files/<ID或T ID>/xxx.png]` 直接指向本地文件，
> 页面上会显示成缩略图画廊。
> 全量抓取是逐条访问禅道详情页，100 条约 1~2 分钟，中途失败只影响单条（结果里 `failed`/`errors` 会列出）。
>
> 说明：会话模式下 `module` 存的是**模块 ID**（禅道只在各产品浏览页单独下发模块名，
> 同步时不值得为 60+ 产品各发一次请求）。
>
> 评论接口：缺陷在 REST 模式默认依次尝试 `POST /api.php/v1/bugs/{id}/comments`、`/comment`、
> `/api.php/v1/products/0/bugs/{id}/comments`，会话模式尝试 `POST /bug-comment-{id}.json`、`.html`；
> 任务把 `bug` 换成 `task`，且会话模式会再试带动作号的 `/task-comment-{id}-{actionId}.json`。
> 分别可用 `ZENTAO_COMMENT_PATH` / `ZENTAO_TASK_COMMENT_PATH` 覆盖。
> **注意禅道的写接口会用「HTTP 200 + 一段 `self.location='/user-deny-….html'` 的跳转页」表示拒绝**，
> 本系统识别这类拒绝页，不会把没写进去的评论报成成功（这条是实测踩出来的坑）。
> 失败不阻塞流程，评论原文会写进 `sync_log` 表留痕。
> 本系统**不提供** resolve/close 接口，AI 无法自动结案。
>
> 排查顺序：「禅道同步」页 → **一键诊断禅道**（分步回显配置/会话/端点/产品解析/Bug 解析），
> 或命令行 `python -m app.cli doctor`；要抓原始响应校准解析用
> `python -m app.cli session-probe`（样本写到 `session_dump/`）。

## 3. 按产品区分代码库（多仓库）

禅道里你的 bug 分布在多个产品上（本机实测：`DPDKUAC&NGFW`、`AC-10G`、`网络审计（DPDK）`… 6 个产品 100 条），
而**每个产品的代码在不同 SVN 仓库**。系统的处理方式是：

1. 同步时把每条 bug 的 `product_id / product_name` 一起落库（禅道列表接口的 `product` 字段）；
2. 「产品仓库」页（`/repos`）或 `bind-repo` 命令把 **产品 ID → 仓库根地址** 绑定，存 `product_repos` 表；
3. `branch` / `commit` 只要给禅道 ID，就自动解析出该 bug 产品的仓库；
4. 工作副本**默认**按产品独立（留空 `working_copy` 时为 `svn_workspace/p<产品ID>-<仓库哈希>`），多产品切换不会互相污染；
   但 `working_copy` 可以手工指定，**两个产品填同一个目录（例如共用一份 git-svn 镜像）时它们就不再隔离**，
   必须并入同一条串行队列 —— 串行队列的划分单位是镜像目录，不是产品 ID（见 `AUTO_LOOP.md` §5）；
5. 产品没绑定 → 回落 `.env` 的 `SVN_REPO_URL`；连它也为空 → 命令直接报错并打印该执行的 `bind-repo` 命令，
   **绝不猜仓库**。

### 绑定操作

网页：打开 <http://127.0.0.1:5000/repos> → 选产品 → 填仓库根地址（不含 `/trunk`）→ 保存绑定。

命令行（Trae / 脚本用这个）：

```
python -m app.cli repos                       # 看每个产品的 bug 数 / 待处理 / 任务数 / 待出方案，与生效仓库、工作副本
python -m app.cli bind-repo 25 svn://10.0.0.1/repo/dpdkuac --name "DPDKUAC&NGFW" --build "make all" --test "make test"
                                              # 编译/测试命令会一并下发给 AI；还可加 --trunk-path/--branch-root/--working-copy/--svn-working-copy/--branch-prefix/--note/--disabled
python -m app.cli svn-check --all             # 逐仓库只读自检（客户端/可达/trunk/工作副本归属）
python -m app.cli svn-check --product 25      # 只检查某个产品解析出来的仓库
python -m app.cli bind-repo 25 --unbind       # 解绑，回落到全局 SVN_REPO_URL
```

绑定时怎么拿到产品 ID：`repos` 命令 / 「产品仓库」页 / 看板卡片上的产品标签都直接显示；
禅道网址 `product-browse-0--byProject-0-...` 里也行，最准的是 `/product-view-{ID}.html` 里的 ID。

一条条目（缺陷或任务）会落到哪个仓库，可以先干跑确认（不会真的动 SVN）：

```
python -m app.cli branch 51452 --dry-run
{"branch":"bugfix/zentao-51452","product_id":25,"product_name":"DPDKUAC&NGFW",
 "repo":"svn://.../dpdkuac","repo_source":"product",
 "trunk":"svn://.../dpdkuac/trunk","working_copy":"...\\svn_workspace\\p25-fb94b59e"}

python -m app.cli branch T5417 --dry-run
{"branch":"bugfix/zentao-T5417","product_id":25,...}   # 任务的 product 来自详情抓取，编号带 T
```

`repo_source` 为 `product` 表示用了产品绑定，`global` 表示回落到 `.env`。

## 4. 给 Trae 的完整操作剧本（自动修复 + 回填）

系统跑通后，你在 Trae 里只需要说一句「按 AGENTS.md 处理禅道 bug 和任务」，Trae 会按下面的顺序做；
你本人只在三处介入：**需方案清单里答复（或直接判定不修了）**、**需方案清单里确认任务的修改细则**，
和 **审查/看板弹窗里通过 / 推入 SVN / 打回重做 / 拒绝并回滚 / 重开补修 / 结案**。

**职责边界**：本系统只做**队列与状态管理**（拉禅道、抓截图备注、排队、状态、登记修订号、审查、回写评论）。
「什么情况下允许提交、提交怎么走（SSH / 是否需同意）/ 真实修订号从哪来」这些**提交细则由目标代码库自己的知识体系决定**，
本系统只下达一份「提交闸门 G1–G13」清单，不复制也不解释那些细则。默认本系统**不执行 svn**
（唯一例外：闸门 G12 的多语言词条，由 `i18n-up` / `i18n-commit` 两条命令代跑 `svn update` / `svn ci`）。

**代码库自带的 AI 知识体系优先**：两条产品线的仓库根都把 `AGENTS.md` 与 `.trae/`（`agents/` 目录级知识、
`doc/ARCHITECTURE.md`、`skills/` 流程）**提交在 SVN 里**，所以它们会随 git-svn 镜像一起落下来 ——
执行器定位代码前必须先按它选入口（`AUTO_LOOP.md` §1.6），不许绕过它全库散搜；得出可复用的结论时
按同一套规则追加回 `.trae/agents/<相对路径>/AGENT.md`，跟代码一起进同一次审查。
导航与写码规范冲突时以代码库内这套为准，唯一不可被覆盖的是 G11（远端写入只有主人能做）。

> 「一条跑完自动下一条」的完整下达方式（三种执行方式、G1–G13 提交闸门、任务两轮流程、多语言直连通道、
> 可复制的主循环与子任务指令、并行约束）见 **`AUTO_LOOP.md`**。下面只是最小骨架。

```
① 准备（一次性）
   python -m app.cli doctor            # 禅道通道是否通
   python -m app.cli repos             # 各产品是否已绑定仓库（闸门 G3 要用）
   python -m app.cli bind-repo <pid> <仓库地址> --build "..." --test "..."

② 取活（每轮开始）
   python -m app.cli sync --details -1   # 全量拉缺陷 + 任务 + 全部截图/备注/附件（只要一种就加 --kinds bug|task）
   python -m app.cli tasks --limit 5     # 取本轮队列：已答复/已确认方案 > 已打回 > 待处理（任务首轮标 plan_needed）
        # 每条带 target=bug|task；任务编号写成 T<id>，后面所有命令照抄这个编号，别用裸数字

③ 逐条处理（每批 5 条，处理完自动取下一批，不问「是否继续」）
   python -m app.cli status <禅道ID|T任务ID>   # 全文 + 截图本地路径 + 备注 + 历史提交 + 主人答复 + 所属产品
        # images 才是能看的图；images_skipped 是 1x1 之类的占位图，模型喂了会 400，跳过即可（不算没看图）
   ── 任务是两轮活：第一轮只交方案，不许写码（闸门 G13）──
   python -m app.cli plan T<任务ID> --question "<需求理解 + 修改细则：要动哪些文件/入口/口径>" --options "<可选做法A…;B…>" --advice "<建议走哪条、为什么>"
        # = 写 task_needs(need_kind='plan') + 状态 need_solution + 回写禅道「AI 已给出任务方案，等主人确认」
        # 登记完立刻做下一条，不等回复；长文本可整份写进 --plan-file plan_T<id>.json（入库后由本系统回收）
        # 方案未被确认前，这条任务的 checkout / draft / commit 会被代码直接拒绝（不是提示，是拒绝），
        # 主人在「需方案」页确认之后它才以 plan_approved 回到队列 —— 那时才走下面的落码步骤
   ── 缺陷，以及方案已确认的任务 ──
   python -m app.cli claim  <禅道ID|T任务ID>   # 占住条目，避免被别的子任务重复领
   python -m app.cli mirror <禅道ID|T任务ID>   # 只读看镜像：草稿分支在不在、领先几笔、就绪与占用情况
   python -m app.cli checkout <禅道ID|T任务ID> # 在镜像里建/切这条的 bugfix 分支（任务分支名 bugfix/zentao-T<id>；
                                         # 只有别人未提交的真实改动才拒绝；
                                         # 该库常年那几十行结构性噪音不拦，见 mirror 的 dirty_real/dirty_noise）
   ... 在该库自己的知识体系下定位并改代码、跑该库的编译/测试 ...
   ... 改了词条就单独走 G12：i18n-up <ID> --files a.po（先 up）→ 改 → i18n-commit <ID> --files a.po（立即单独提交，拿 r<号>） ...
   ... 逐条核对 AUTO_LOOP.md 的提交闸门 G1–G13 ...
   闸门全过 → 只在落码位置本地提交，回本系统登记：
   python -m app.cli draft <ID> --message "fix #<ID> <根因>" --files a.c,b.c   # 镜像里落草稿提交，返回短哈希
                                         # 任务的提交说明前缀是 feat #<ID>（缺陷是 fix #<ID>），写错前缀会被拒
   python -m app.cli commit <ID> --message "..." --files a.c,b.c --summary "..." --verify "..." --no-svn --revision git:<短哈希> --analysis-file a.json
        # = 存分析结论 + 写提交记录 + 状态置 await_review + 回写禅道评论（不跑 svn）
        # mirror/checkout/draft 把执行器要用镜像的动作全收进本系统命令（AUTO_LOOP.md §1.8）：
        # 不必自己拼裸 ssh + git，也就少一处会把无人值守冻住的授权弹窗
        # --analysis-file 那个 JSON 在命令入库成功后由本系统自己回收（返回里的 handoff_removed）；
        # 执行器全程不需要、也不许去做「删除文件」这个动作 —— 它是实测最常见的授权触发点
   **分析结论是必交付物**：REQUIRE_ANALYSIS=true（默认）时没写分析的 commit 会被直接拒绝，
   字段规范见 `AUTO_LOOP.md` §2.1；也可先 `analyze <ID> --kind commit --analysis-file a.json` 再 commit
   还需你授权才提交：把 --revision 换成 PENDING 并用 --extra 说明「待主人按仓库细则提交」
   拿不定主意时（**任务实施中卡住也用这条**，它写的是 block 类，与首轮的方案类分开）：
   python -m app.cli block <ID> --question "..." --options "方案A…；方案B…" --advice "建议A"
        # = 写需方案记录 + 状态 need_solution + 回写禅道「AI 阻塞」，然后立刻做下一条

④ 人工环节（你）
   「需方案清单」/need  → 写答复提交 → 状态 replied，Trae 下轮优先处理
        # 任务方案在这一页确认：卡片带「方案」徽标，点「确认方案，开始实现」（或直接「一键确认（照 AI 建议做）」）
        # → 该任务以 plan_approved 排到队首，AI 才允许 checkout/draft/commit；要改口径就在答复框里写清楚再提交
        # 答复也可以不是方案：选「无法重现 / 不是缺陷 / 重复单 / 禅道已关闭」点「结案（不修了）」
        # → 这条变 closed、不再下发给 AI，未答复的阻塞项一并标记已处理；
        #   已建的草稿分支与提交一行不动，之后改主意还能「重开补修」
   「审查清单」/review  → 看 diff/说明 → 打开详情弹窗写提交说明 → 「预检（不写入）」→「正式推入 SVN」
        # 由 `app/svn_promote.py` 在 SSH 侧把该条目的 git 草稿落到稀疏 SVN 工作副本再 `svn ci` 进 trunk：
        # 成功 → 自动登记真实 r 号并置 `merged` + 回写禅道评论；失败 → 只写一条人工留痕，
        #        状态仍是 `await_review`，`git:<哈希>` 草稿记录不动（推不入库不等于改得不对，不莫名打回）
        # 分支上相对 trunk 的全部草稿提交会合并成一次 svn 提交（打回重做几轮也是最后一次推入）
        # 推之前会逐个文件比对 trunk，草稿基线已被别人改动就直接拒绝覆盖；全程 --non-interactive，不会弹密码
        # 被拦下时预检会说明「谁改的（r 号）」+ 哪些文件与本改动不重叠：勾「自动按 trunk 对齐」再推，
        # 脚本用 git merge-file 以 trunk 现内容为底合并（他人改动一行不少）；真撞上同一行仍整笔拒绝
        # 对齐了什么、跳过了哪些已入库文件，只写进审查留痕与禅道评论，不进 svn 的提交说明
      不想让它代推，就自己按仓库细则提交后回填真实修订号（`AUTO_LOOP.md` §3.3）；填 trunk 号点「通过」→ merged
   「审查清单」里的三种处置（都不碰 trunk）：
      「打回重做（保留草稿）」→ rejected，改动不回滚，AI 下一轮在原分支上接着改
      「拒绝并回滚 → 已结案」→ closed，删除本条的 `bugfix/*` 草稿分支；提交先存进 `refs/rejected/<分支>`，
        需要时用 `git branch <分支> refs/rejected/<分支>` 取回；HEAD 正停在该分支时拒绝执行（不动别人的工作区）
        # 回滚失败（分支删不掉）时状态一字不改，只在弹窗里报错
      「重开补修（入队列）」→ 已 merged/closed 的条目发现修得不完全：回到 rejected 重新入队，
        r 号与草稿分支都保留、trunk 不回退，上一轮的结论/改动文件/r 号作为 `prior_fix` 一起下发；
        下一轮推送以「上一轮推入的那个草稿提交」为增量基准，只提交新改动，trunk 上已有的文件自动跳过
   「标记已结案」→ closed（禅道侧 resolve/close 由你手动做，AI 无权限）：
      merged 的卡片 = 已推入 trunk 的收尾登记；need_solution / pending 的弹窗或 /need 页 = 「结案（不修了）」，
      带 `resolution`（无法重现/不是缺陷/重复单/禅道已关闭/其他）与可空 `reason`，会追加一条 `author=owner`
      的人工留痕并在禅道留言；fixing / await_review / rejected 不许这样直接结案（点了会被拒并说明该走哪条）

⑤ 收尾
   python -m app.cli report            # 已提交待审 / 需方案（含待确认方案）/ 续修 / PENDING 待提交
        # 任务在输出里一律写成 T<id>，remaining_queue 每条带 kind 与 queue_kind
   python -m app.cli reconcile         # 对账：镜像上有草稿提交、库里却没登记的（跑完没收口的那类）
        # 只报告不改东西；核对无误后 reconcile <禅道ID|T任务ID> --apply 认领成待审查，
        # 登记的 git:<哈希> 带真实哈希，分析与状态都会标明「结论来自提交信息，未验证」
   python -m app.cli tmp-clean         # 回收跑完留下的交接文件（a_<ID>.json / a_T<ID>.json / blk_<ID>.json / py_<ID>.py …）
        # 默认只报告，加 --apply 才删；分析没入库的那份写稿一律留着，不吞掉唯一一份结论。
        # 命令入库时已顺手回收自己读过的那份，这条是清场用的 —— 执行器不必自己去删文件
```

安全边界（系统强制，Trae 绕不过去）：AI 不能提交或合并 trunk、不能 resolve/close 禅道条目、
不能改与当前条目无关的文件、闸门未过不许提交、不许绕过仓库自己的提交门禁、
**任务方案未经主人确认就不许落码（G13：`checkout` / `draft` / `commit` 与「推入 SVN」四处都会拒绝）**；
所有改动都挂在 `bugfix/zentao-<ID>`（任务为 `bugfix/zentao-T<ID>`）名下等你审查。

## 5. 页面怎么用

| 页面 | 用途 |
| --- | --- |
| **看板** `/` | 三类纵向行块（要我处理 / AI 在跑 / 已收尾），点块头折叠且会记住；块内按状态分组，卡片上有产品标签；顶部有「全部 / 只看缺陷 / 只看任务」三档，任务卡片带紫色「任务」徽标、编号写成 `T<id>`、显示所属迭代；点卡片在**弹窗**里看描述、截图画廊、备注、分支、SVN 记录、需方案/方案记录、审查记录，后退键或「返回」回到列表；已合入/已结案的条目在弹窗里可点「重开补修（入队列）」把不完全的修复退回队列（带上一轮结论） |
| **下达任务** `/dispatch` | 开跑前一页看全：检查清单（禅道是否可用、详情是否抓全、哪些产品没绑定、哪些产品共用同一镜像必须串行、**有哪些任务还欠着方案**）+ 按落码目录归组的串行队列 + **从 `AUTO_LOOP.md` 现场抽取的 §0 / §3.1 / §3.2 / §9 提示词**；可勾选产品或点「选这组」生成**已填好产品 ID、落码目录与串行要求**的下达语（§0 / §3.1 二选一），每段一个复制按钮，粘给新开的 Trae 主对话即可 + 下一条会被处理的条目（任务显示 `T<id>` 与「待出方案」） |
| **禅道同步** `/sync` | 「立即同步」+「批量抓取详情（0=全量）」（两处都能勾选同步缺陷还是任务）+ 待抓详情条数 + 今日新增/更新 + 缺陷数/任务数 + 同步日志 + 一键诊断 |
| **需方案清单** `/need` | AI 卡住的问题（徽标「阻塞」）与**待确认的任务方案**（徽标「方案」）在同一页，可按种类筛选；阻塞项写答复提交 → `replied`，AI 下次优先处理；**方案项点「确认方案，开始实现」或「一键确认（照 AI 建议做）」→ 该任务才进队列**，要改口径就在答复框里写清；答复也可以是结论——选一个结案理由点「结案（不修了）」，这条直接 `closed` 并停止下发 |
| **审查清单** `/review` | AI 的分析结论（根因/定位依据/链路/影响面/验证结果/未验证项/闸门逐条）+ 修改文件列表 + SVN 提交号 + 修复说明与验证步骤 + 截图；可按「全部 / 缺陷 / 任务」筛选，任务条目标「任务」且编号带 `T`；详情弹窗里可「预检」/「正式推入 SVN」（草稿→真实 r 号，多轮草稿合并成一次提交，见 §3.3）；填 trunk 号点「通过」→ `merged`；填原因点「打回重做（保留草稿）」→ `rejected` 并重回 AI 队列（改动不回滚，下轮在原分支续改）；点「拒绝并回滚 → 已结案」→ 删除草稿分支（提交存 `refs/rejected` 可取回）并置 `closed` |
| **SVN 记录** `/svn` | 按条目分组，同一条目多次提交连续排列（任务组显示 `T<id>`）；「当前/全部仓库自检」按钮 |
| **产品仓库** `/repos` | 产品 ↔ 代码库绑定：每个产品的缺陷数 / 待处理 / **任务数 / 待出方案**、生效仓库来源、实际 trunk 与工作副本；绑定/编辑/解绑 |
| **配置** `/config` | 当前生效配置（密码脱敏）+ 重新读取 `.env` |

`merged` 的卡片展开后有「标记已结案」按钮 → 本地变 `closed`（禅道状态仍由你手动改）；
`need_solution` / `pending` 的条目同样能结案，只是理由换成「无法重现 / 不是缺陷 / 重复单 / 禅道已关闭」，
它不代表 AI 修过任何东西，也不删草稿分支。

## 6. AI 执行器命令一览

`AGENTS.md` 是给 AI 的协议，所有动作都通过带 JSON 输出的 CLI 完成，AI 不需要手写 SQL：

```
python -m app.cli init                    # 建库建表（幂等，启动时也会自动补列/补表；任务那 5 张表同样自动建）
python -m app.cli sync                    # 拉禅道缺陷 + 任务入库（文字）；--kinds bug|task 只拉一种
python -m app.cli sync --details 5        # 并抓队首 5 条的截图/备注/附件
python -m app.cli sync --details -1       # 并全量抓截图/备注/附件
python -m app.cli detail 1024             # 抓单条缺陷的截图/备注/附件到 attachments/
python -m app.cli detail T5417            # 抓单条任务（正文取关联需求 storySpec，附件落 zentao-T5417/）
python -m app.cli detail --limit 20       # 抓优先级最高的 20 条
python -m app.cli detail --all            # 全量抓（--kinds task 只抓任务）
python -m app.cli img-gate --check        # 重量已下载的截图：1x1 / 损坏图标为 unreadable（去掉 --check 才写库）
                                     # 这类图喂给视觉模型会 400 打断整轮，标出来后它们就不在「必须看图」清单里
python -m app.cli tasks --limit 5         # 取队列：已答复/已确认方案 > 已打回 > 待处理（任务首轮 plan_needed），再按 pri 升序 / severity 降序
                                     # 只要这条还挂着未答复的阻塞项（awaiting），就不进队列——老问题被答过不算数
                                     # 每条带 target=bug|task，任务编号一律写成 T<id>；--kinds task 只看任务
python -m app.cli status 1024             # 读缺陷全文 + 历史提交 + 主人答复 + 所属产品
python -m app.cli status T5417            # 读任务（另带 迭代/关联需求/截止时间/plan_approved）
python -m app.cli mirror 1024             # 只读探测镜像上这条的草稿分支：tip / 领先几笔 / 改了哪些文件 / 就绪与占用
                                     # 返回里的 lock.state = absent|stale|hold|busy|odd（只报告，mirror 从不删锁）
python -m app.cli checkout 1024 [--base <基线>]  # 在镜像里建/切 bugfix/zentao-1024（默认基线 origin/trunk）；任务用 T5417 → bugfix/zentao-T5417
                                     # **任务方案未经主人确认 → 直接拒绝（G13）**，error 里附可直接执行的 plan 命令
                                     # 镜像未就绪、或工作区有别人未提交的**真实**改动 → 拒绝且不改状态（不替你 stash）；
                                     # 该库那几十行常年结构性噪音（.trae/ 符号链接、CRLF、$Id$）不拦，另计入 dirty_noise
                                     # 判据自己没读成功时 dirt.state=failed 且一并拒绝（未知≠干净）；git 拒绝切分支的 stderr 原文随 error 返回（2.3 那台是 git 1.7.1）
                                     # 上一轮崩在写索引中途留下的陈锁（0 字节 + 本机无 git 进程 + 静默超 5 分钟）自动回收，返回 lock.swept
python -m app.cli draft 1024 --message "fix #1024 <根因>" --files a.c,b.c
                                     # 在镜像的这条分支上落草稿提交：只 add 列出的文件，返回短哈希当 git:<哈希>
                                     # 说明前缀：缺陷 fix #<ID>、任务 feat #<ID>（G8）；任务方案未确认 → 直接拒（G13）
                                     # 说明不以 fix #<ID>/feat #<ID> 开头 / 清单为空 / 路径越界 / HEAD 不在这条分支 → 直接拒
                                     # 陈锁同样先自动回收；不满足回收条件（非 0 字节 / 太新 / 有活 git）就拒绝并告诉你该删哪个文件
python -m app.cli reconcile [1024|T5417] [--apply]
                                     # 对账：镜像上有草稿提交、库里没登记的（跑完没收口）；--apply 才认领成待审查
                                     # 不带编号时两种条目都扫（--kinds 可只看一种）
python -m app.cli tmp-clean [--apply]
                                     # 回收留在项目根的交接文件（a_<ID>.json / a_T<ID>.json / blk_<ID>.json / py_<ID>.py / _tmp_*）
                                     # 默认只报告；分析未入库的那份一定留着。执行器不必自己去删文件
python -m app.cli repos                   # 每个产品的缺陷数/待处理/任务数/待出方案与生效仓库
python -m app.cli bind-repo 25 <仓库地址> --name "产品名" --build "..." --test "..."
                                     # 加 --working-copy <git 镜像> 指定改码目录，加 --svn-working-copy <正式SVN副本> 开多语言通道
python -m app.cli i18n-up 1024 --files i18n/zh_CN.po
                                     # G12 第 1 步：改词条前先 svn update（只碰白名单文件，返回 conflict 就别动手）
python -m app.cli i18n-commit 1024 --files i18n/zh_CN.po --message "补充 xx 词条" [--dry-run]
                                     # G12 第 2 步：改完立即单独 svn ci（含 trunk），真实 r 号自动登记 + 回写禅道评论
                                     # 白名单外的文件、副本外的路径、没有本地改动 —— 一律拒绝且不提交
python -m app.cli branch 1024 [--dry-run] # 在缺陷所属产品的仓库 svn copy trunk -> branches/bugfix/zentao-1024 并 switch
                                     # 任务写 branch T5417 → branches/bugfix/zentao-T5417（产品来自任务详情抓取）
python -m app.cli note 1024 --summary "..." --verify "..." --files a.py,b.py
python -m app.cli analyze 1024 --kind commit --analysis-file a.json
                                     # 写入分析结论（现象/根因/证据/链路/改动/影响面/验证/未验证/回退/结论 + gates）
                                     # 也支持 --analysis-stdin 与单字段 --root-cause/--evidence/--impact/--gates "G1=pass,..."
python -m app.cli commit 1024 --message "..." --files a.py --summary "..." --verify "..." --no-svn --revision r123 --analysis-file a.json
                                     # = 存 analyses + 写 svn_revisions + await_review + 回写禅道评论（默认不跑 svn）
                                     # REQUIRE_ANALYSIS=true 时：没有分析结论的 commit 会被直接拒绝
python -m app.cli block 1024 --question "..." --options "A..;B.." --advice "建议A"
                                     # = 写需方案记录 + need_solution 状态 + 回写禅道「AI 阻塞」
python -m app.cli plan T5417 --question "<需求理解 + 修改细则>" --options "A…;B…" --advice "建议A" [--plan-file plan_T5417.json]
                                     # 任务首轮的唯一产出（G13）：写 task_needs(need_kind='plan') + 状态 need_solution
                                     # + 回写禅道「AI 已给出任务方案，等主人确认」；确认后任务才回队列，长文本走 --plan-file
                                     # 方案没被确认前，这条任务的 checkout/draft/commit 与「推入 SVN」都会被代码拒绝
python -m app.cli need-done <need_id>     # 关闭已答复的阻塞项/已处理方案（任务侧记录写成 T<need_id>）
python -m app.cli comment 1024 --text "..."  # 仅回写一条禅道评论
python -m app.cli doctor                  # 禅道通道分步诊断（只读）
python -m app.cli session-probe           # 抓禅道原始响应到 session_dump/ 便于对齐字段
python -m app.cli svn-check [--all|--product 25]   # SVN 只读自检：客户端/可达/认证/trunk/工作副本/主干保护
python -m app.cli report                  # 停止时输出汇总
python -m app.cli config                  # 查看当前配置（密码脱敏）
python -m app.cli demo --clear            # 清除演示数据
```

`commit` 会拒绝指向 trunk 的工作副本；SVN 没配好时可用 `--no-svn --revision PENDING`
先登记为待审查，不阻塞后续 bug。

## 7. 数据库表

```
bugs(id, zentao_id UNIQUE, title, severity, pri, status, branch,
     steps, steps_html, comments, attachments, module,
     product_id, product_name, assigned_to, assigned_to_name,
     opened_by, opened_build, zentao_status, zentao_resolution, opened_date,
     fix_summary, verify_steps, files_changed, raw_json, detail_synced_at,
     synced_at, created_at, updated_at)
                   # comments = 禅道备注/操作记录 JSON；attachments = 截图与附件
                   # (file_id/ext/url/name/local_path/web_path/size)
                   # product_id = 禅道产品，决定这条 bug 用哪个代码库
tasks(id, zentao_id UNIQUE, title, pri, status, branch,
     steps, steps_html, comments, attachments, module,
     product_id, product_name, project_id, project_name, story_id, story_title,
     deadline, estimate, need_confirm, assigned_to, assigned_to_name,
     opened_by, opened_date, zentao_status,
     fix_summary, verify_steps, files_changed, raw_json, detail_synced_at,
     synced_at, created_at, updated_at)
                   # 与 bugs 同构（没有 severity），zentao_id 在两张表里各自独立编号
                   # 所以任务在系统内一律写成 T<id>：表不同、附件目录不同（zentao-T<id>/）、
                   # 镜像分支不同（bugfix/zentao-T<id>），同号缺陷与任务互不干扰
                   # product_id 只能从 /task-view-<id>.json 的动作记录里解出来（列表接口不带），
                   # 它决定这条任务落哪个仓库；steps 里的正文来自关联需求的 storySpec
product_repos(product_id PK, product_name, repo_url, trunk_path, branch_root,
              working_copy, svn_working_copy,
              branch_prefix, build_command, test_command, note,
              enabled, created_at, updated_at)
                   # 一个产品 = 一个仓库；enabled=0 或删行则回落全局 SVN_*
need_solution(id, bug_id, question, ai_options, ai_advice, owner_reply,
              status[awaiting|replied|done], created_at, replied_at, done_at)
task_needs(id, task_id, need_kind[plan|block], question, ai_options, ai_advice,
           owner_reply, status[awaiting|replied|done], created_at, replied_at, done_at)
                   # need_kind='plan' = 待主人确认的修改细则：只有它 status='replied' 且
                   # owner_reply 非空，这条任务才允许 checkout/draft/commit（闸门 G13）
                   # need_kind='block' = 实施中卡住，与缺陷的 need_solution 同义
                   # 缺陷表没有 need_kind 列，跨表读取时投影成常量 'block'
analyses(id, bug_id, kind[commit|block|manual], symptom, root_cause, evidence,
         call_chain, change_desc, impact, verify, gates(JSON G1–G13), unverified,
         rollback, conclusion, author, created_at)
task_analyses(id, task_id, kind[commit|block|plan|manual], …同上…)
                   # 执行器每条收尾必写的分析结论；commit 会校验其存在
                   # 任务的 plan 类记录 = 首轮方案的文字依据
svn_revisions(id, bug_id, revision, branch, message, author, files, git_commit, created_at)
task_revisions(id, task_id, revision, branch, message, author, files, git_commit, created_at)
reviews(id, bug_id, result[pass|reject], reject_reason, merged_revision, reviewer, created_at)
task_reviews(id, task_id, result[pass|reject], reject_reason, merged_revision, reviewer, created_at)
sync_log(id, action[sync|update|comment|error], zentao_id, payload, created_at)
meta(key, value)          # 最近同步时间等杂项
```

表和列由 `db.init_db()` / `db.migrate()` 自动补齐（每次 `python -m app.cli` 与启动 Web 都会检查，
全部语句是 `IF NOT EXISTS`，不会动已有数据），不用你手动 ALTER；任务那 5 张表是纯新增，`bugs` 一字未改。
读写侧由 `db.tables_for(target)` 选表，业务代码只传 `target="bug"|"task"`。

`status` 枚举：`pending / fixing / need_solution / await_review / rejected / merged / closed`。

## 8. 常见问题

1. **点同步报「禅道未配置」** → `.env` 三项必填没填。
2. **登录取不到 token** → 先在「禅道同步」页点 **一键诊断禅道**，它会分步回显真实响应
   （配置 / 站点可达 / 登录 / 产品列表 / Bug 列表），再按提示处理：
   - 返回 HTML 页面 → `ZENTAO_BASE_URL` 路径不对，或后台没开 RESTful API；
   - 返回 `{"errcode":401,"errmsg":"缺少code参数"}` 这类**不是禅道原生格式**的 JSON
     → `api.php` 被前置网关/统一登录接管了，账号密码换 token 这条路走不通，
     需要管理员对 `api.php` 放行，或改用下面的手工令牌；
   - 提示需要验证码 / 账号被锁 → 用 `ZENTAO_TOKEN` 手工令牌（填了就跳过账号密码登录）。
3. **自签 HTTPS 证书报错** → 保持 `ZENTAO_VERIFY_SSL=false`。
4. **`branch`/`commit` 报「产品 X 未绑定代码库」** → 该产品还没绑定、`.env` 的 `SVN_REPO_URL` 也为空。
   去 `/repos` 页绑定，或 `python -m app.cli bind-repo <产品ID> <仓库根地址>`；
   绑完用 `python -m app.cli branch <禅道ID> --dry-run` 确认输出里 `repo_source` 是 `product`。
5. **`working_copy` 显示「该副本属于 …，与本产品仓库不一致」** → 那个目录是别的产品检出的。
   删掉该目录让它重新 checkout，或在绑定里给它单独指定一个 `working_copy`。
6. **老 bug 没有产品名/产品 ID** → 产品字段是新增的，重新点一次「立即同步」即可回填。
7. **SVN 相关任何不通** → 先跑 `python -m app.cli svn-check --all`（或页面「SVN 记录 → 全部仓库自检」），
   它只跑 `svn --version` / `svn info`，逐仓库告诉你：客户端版本、仓库地址是否可达、
   认证是否通过、`/trunk` 是否存在、`/branches` 是否要自动建、工作副本状态、
   正式 SVN 工作副本（多语言通道用）是否已配且归属正确、
   以及 `SVN_ALLOW_TRUNK_WRITE` 是否仍为 false。
   注意本机 svn 是 **1.6.16-SlikSvn**：若仓库工作副本是 1.7+ 格式（`.svn/wc.db`），
   1.6 客户端读不了，自检会直接 FAIL 提示，需要升级客户端并把 `SVN_BIN` 指向新的 `svn.exe`。
8. **页面数据为空** → 数据库里目前只有 `[DEMO]` 演示数据时会被真实同步混着显示，
   用 `python -m app.cli demo --clear` 清掉。
9. **`checkout`/`draft`/`commit` 报「是任务：首轮只许给方案…」** → 这不是故障，是闸门 G13 生效：
   这条任务还没交过方案、或方案还没被主人确认。按 `error` 里附的命令先 `plan T<id> --question "…"` 登记方案，
   再到「需方案」页确认；确认前它会以 `need_solution` 停在队列外。**不要重试、不要绕道裸 git**。
10. **`plan`/`block`/`commit` 回报找不到条目** → 种类写错了：任务必须带 `T` 前缀（`T5417`），
    裸数字一律按缺陷解析；`5417` 与 `T5417` 在禅道里是两条不同的东西。
11. **任务卡片正文是空的 / 看不到需求** → 任务列表接口只带回标题，正文挂在关联需求的 `storySpec` 上，
    必须抓过详情才有：`python -m app.cli detail T<id>`（或「禅道同步」页批量抓取详情并勾「任务」）。
    同样地，任务的产品号也只在下发详情里解得出，**没抓过详情的任务没有 `product_id`、解析不出仓库**。
12. **同步了缺陷但一条任务都没进来** → ①`.env` 的 `ZENTAO_TASK_STATUS` 是否含该任务的状态
    （默认 `wait,doing`；禅道任务状态与缺陷状态是两套枚举），②任务是否指派给你，
    ③抓详情后解不出所属产品的任务会被丢弃（不入库），`sync` 返回里的 `by_kind.task` 与 `errors` 会说明。
13. **禅道上看不到 AI 的评论，但命令返回 `zentao_comment.ok=true`** → 已修：禅道的写接口会用
    「HTTP 200 + `self.location='/user-deny-….html'`」表示拒绝，旧判据把它当成功。
    现在系统识别这类拒绝页并改用 `/task-comment-<id>-<actionId>.json`；若仍被拒，说明该账号对这条任务
    没有留言权限，评论原文在 `sync_log` 表里留痕，页面流程不受影响。

## 9. 目录结构

```
run_web.py           一键启动入口（建库 + 起服务 + 开浏览器，支持 --sync/--port）
start_web.bat        双击启动（自检 Python、缺依赖时自动 pip install）
start_web_sync.bat   双击启动并先同步禅道
app/config.py        读取 .env，集中配置
app/db.py            SQLite 建表/迁移 + 全部数据访问（缺陷与任务两套表按 target 选表，含 product_repos）
app/zentao_client.py 禅道 REST v1 客户端（token 认证，只读 + 评论；缺陷与任务两套端点）
app/zentao_session.py禅道会话客户端（登录 + /xxx.json 内部接口 + 附件下载 + 任务列表/详情/留言，识别拒绝页）
app/zentao_parse.py  禅道响应解析（描述/图片/备注/附件 共用一层；任务详情与 storySpec、产品号反解）
app/svn_client.py    svn 命令封装（按产品解析仓库、分支、提交、log、trunk 保护）
app/sync.py          同步、批量抓详情、回写编排（按 kind 分流，一种失败不拖另一种）
app/web.py           Flask 页面与接口
app/cli.py           AI 执行器命令行（全部输出单条 JSON）
app/templates/       看板/下达任务/同步/需方案/审查/SVN/产品仓库/配置 页面
app/static/          style.css + app.js（弹窗与后退返回、三类行块折叠、确认框、一键复制、kind 参数）
AGENTS.md            AI 批量执行规则（Trae 读它干活）
AUTO_LOOP.md         自动连跑下达手册（提交闸门 G1–G13 + 任务两轮流程 + 多语言直连通道 + 可复制的主循环/子任务指令）
README.md            本文档（使用与运维）
CAPABILITIES.md      能力清单与接口协议（CLI/Web/DB 三张表）
```
