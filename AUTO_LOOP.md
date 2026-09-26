# 自动连续执行下达手册（一条跑完自动下一条）

**职责边界（本系统的唯一定位）**

| | 谁负责 | 内容 |
| --- | --- | --- |
| 任务管理 | **本系统（AI-BUG）** | 拉禅道、抓截图/备注/附件、排队与优先级、占任务、状态流转、登记改动与修订号、审查流、回写禅道评论 |
| 提交资格与提交动作 | **代码库自己的知识体系**（目标仓库工作区的 `CLAUDE.md` 及其 `registry/playbooks/skills`） | 什么情况下允许提交、怎么走 SSH、要不要征求同意、`svn ci` 怎么执行、真实修订号从哪来 |

本系统**不做提交决策、默认也不执行 svn**。它只把一份「提交闸门」清单交给执行器；
执行器在代码库侧按自己的细则完成提交，回到本系统只做一件事：`commit --no-svn --revision <真实修订号>` 登记。

本文档只管「怎么下达任务」；执行协议本体在 `AGENTS.md`，接口细节在 `CAPABILITIES.md`。

---

## 0. 最短下达语（直接粘）

```
读 AGENTS.md 与 AUTO_LOOP.md，无人值守处理禅道 bug，按 AUTO_LOOP.md 的【提交闸门】判定可否提交：
1) python -m app.cli sync --details -1     （同步 + 全量抓截图/备注/附件）
2) python -m app.cli tasks --limit 5       （按 product_id 分组，组内严格串行、组间可并行）
3) 每条 bug 起一个独立子任务，用 AUTO_LOOP.md §3.2 模板，只替换禅道ID
4) 子任务只做：status(看图看备注) → claim → 定位改码 → 编译/测试 → 逐条核对提交闸门
5) 闸门全过 → 按目标代码库自己的提交细则完成提交，拿到真实修订号后回本系统登记，
   并把分析结论一起写入（--analysis-file a.json，字段见 §2.1）：
   commit --no-svn --revision <真实修订号> --analysis-file a.json；
   闸门任一条不过 → block 回填（也带上分析），继续下一条
6) 一批跑完立刻取下一批，禁止问我是否继续；tasks --limit 1 返回 0 条才停并输出 report
本系统不执行 svn、不判定提交细则；没写分析结论不许 commit；不许提交或合并 trunk；
不许 resolve/close 禅道 bug；不许改与当前 bug 无关的文件
```

## 1. 两种执行方式（默认用第一种）

| 方式 | 提交动作由谁做 | 本系统命令 | 适用 |
| --- | --- | --- | --- |
| **默认：只管任务**（推荐） | 执行器按**代码库知识体系**的提交细则做（含 SSH 门禁、同意流程、真实修订号） | `claim` → 提交 → `commit --no-svn --revision <真实号> --analysis-file a.json` | 有自己规则体系的仓库（如带 `CLAUDE.md` 的工作区） |
| 可选：托管 svn | 本系统代跑 `svn copy/switch/commit` | `branch <ID>` → `commit <ID> --files ...` | 无门禁的独立工作副本（`svn_workspace/p<产品ID>-<hash>`），想彻底无人值守时 |

两种方式的**任务管理部分完全一样**（排队、详情、状态、审查、禅道回填），差别只在 `svn` 由谁执行。
换方式不需要换文档，只换子任务里第 5 步那一条命令。

## 2. 提交闸门（下达给执行器的「什么情况下可以提交」）

闸门全过才允许提交；**任一条不过 → 走 `block` 回填需方案，绝不「先提交再说」**。

| # | 条件 | 本系统提供的判据 |
| --- | --- | --- |
| G1 | 已看过禅道的完整描述、**截图**、备注/操作记录 | `status` 的 `steps` / `attachments[].local_path` / `comments`；缺则先 `detail <ID>` |
| G2 | 根因明确、修改点唯一（或已按主人 `owner_reply` 执行） | `status` 的 `owner_reply` 与历史提交 |
| G3 | 只改与本 bug 直接相关的文件，且都在**该 bug 所属产品的仓库**内 | `status` 的 `product_id` + `repos` 的产品→仓库映射 |
| G4 | 该产品的编译/测试实际通过（连续 2 次不过即视为不过） | `repos` 的 `build_command` / `test_command`（未绑定则按代码库自身构建规则） |
| G5 | 状态矩阵自审完成：主路径 / fallback 与异常路径 / 资源释放收尾 / 日志计数 / 同构镜像分支 | 无（由代码库知识体系的 precheck 规程负责，本系统只要求你显式声明已核对） |
| G6 | 触及页面文案就同步 `i18n.po/.mo`；页面源码不直接写中文、注释全英文；不引入超出语言基线的语法 | 无（同上，属代码库规则） |
| G7 | 提交目标是 `bugfix/zentao-<禅道ID>` 分支，**不是 trunk** | `status` 的 `branch`；本系统侧还有 `SVN_ALLOW_TRUNK_WRITE=false` 兜底 |
| G8 | commit message = `fix #<禅道ID> <一句话根因>`，不含账号密码等敏感串 | `commit --message` 拼接规则 |
| G9 | 未验证的路径必须标「未验证」，不得按「已完成 / 可直接合入」收口 | 登记内容进 `fix_summary` / `verify_steps` 供你审查 |
| G10 | 提交所需的授权已按代码库规程取得（如 SSH 写操作需显式同意） | 无（属代码库规则；本系统只要求拿到真实修订号再登记） |

> G5 / G6 / G10 这三类是**代码库自己的细则**，本系统不复制也不解释它们，只在闸门清单里点名要求 ——
> 细则原文以目标仓库工作区的 `CLAUDE.md` 与其下层 `registry/playbooks` 为唯一权威。

## 2.1 分析结论（bug 完成时必须写进系统的交付物）

子任务结束 = 这条 bug 结束，**分析结论是必交付物**：`REQUIRE_ANALYSIS=true`（默认）时，
没有分析结论的 `commit` 会直接被拒。写法两种：

```
# 写法一：随 commit 一起提交（推荐，一条命令搞定）
python -m app.cli commit <禅道ID> --message "<一句话根因>" --files a.c,b.c --no-svn --revision <真实修订号> --analysis-file a.json

# 写法二：先单独写分析，再 commit
python -m app.cli analyze <禅道ID> --kind commit --analysis-file a.json
python -m app.cli commit <禅道ID> --message "..." --files a.c,b.c --no-svn --revision <真实修订号>
```

`a.json` 的字段（除 gates 外都建议写满，写不出来的就是没分析到位）：

```json
{
  "symptom":     "现象：什么入口、什么版本、用户看到什么",
  "root_cause":  "根因：一句话说清为什么坏",
  "evidence":    "定位依据：真实读过的文件:行 / 函数 / 日志 / 复现输出（禁止写没读过的路径）",
  "call_chain":  "涉及链路：入口 -> 中间层 -> 最终实现",
  "change_desc": "改了什么、为什么这样改",
  "impact":      "影响面 + 同构/镜像路径自审结论（对应 G5）",
  "verify":      "验证方式与实际结果（编译/测试/复现的真实输出，对应 G4、G9）",
  "unverified":  "仍未验证的点；没有就留空，禁止用「无」搪塞",
  "rollback":    "回退方式",
  "conclusion":  "一句话结论",
  "gates":       {"G1": "pass", "G5": "未验证:OEM 同名页未核对", "G7": "pass"}
}
```

命令行也有等价的单字段开关：`--symptom --root-cause --evidence --chain --change --impact
--verify-result --unverified --rollback --conclusion --gates "G1=pass,G5=未验证:xxx"`；
JSON 的 `gates` 也可以走 `--gates`（`k=v` 用逗号/分号分隔）。

落库后你能在三处看到它：审查清单 `/review`（最要紧，通过/打回就看它）、看板卡片展开、
`GET /api/bug/<id>` 与 `python -m app.cli status <禅道ID>`。闸门逐条按颜色显示：
`pass` 绿、`未验证` 黄、其它红。

一条 bug 可以有多份分析（打回重做、卡点后再修都会追加），审查页显示最新一份。

## 3. 标准指令模板（复制即用）

### 3.1 主循环指令（给主对话）

```
角色：你是调度器，不亲自改代码。工作目录 <项目目录>。
循环直到退出条件满足：
  a. python -m app.cli tasks --limit 5；返回 0 条则跳到「收工」
  b. 按 product_id 分组；同一产品严格串行，不同产品可以并行开子任务
  c. 每条 bug 起一个子任务，用 §3.2 模板，只替换 <禅道ID>
  d. 收集子任务返回的 JSON，按 zentao_id 记账；一条失败不影响下一条
  e. 回到 a，不等待我确认
收工：python -m app.cli report，给我三份清单：
  - 已提交待审查（我要去 /review）
  - 需我给方案（我要去 /need 答复）
  - 闸门未过/未提交及原因
期间不要复述规则、不要问我是否继续、不要碰 trunk、不要替代码库判定提交细则。
```

### 3.2 子任务指令（每条 bug 一份，无上下文也能独立执行）

```
你只处理禅道 bug #<禅道ID>，做完立即结束，禁止顺带处理其他 bug。
任务管理命令的工作目录：<项目目录>

1) python -m app.cli status <禅道ID>
   读 product_id / product_name / steps / comments(备注) / attachments[].local_path / 历史提交 / owner_reply
   → 有截图必须用 Read 打开 local_path 真正看图，只看文字就动手是最常见的误判来源
   → 若 attachments 为空且没抓过详情：python -m app.cli detail <禅道ID>
2) python -m app.cli claim <禅道ID>        # 占住任务，置 fixing，避免被别的子任务重复领
3) 在目标代码库里按该库自己的知识体系定位修改点（先读它的 CLAUDE.md / 首跳规程，再动代码）
   只改与这条 bug 直接相关的文件，且路径必须属于这条 bug 的产品仓库
4) 编译 / 测试：用该库规定的构建方式（本系统 `repos` 里的 build_command / test_command 可作参考）
   连续 2 次不过 → 直接走第 6 步 block，写清失败现象，不要硬试
5) 提交判定（AUTO_LOOP.md §2 的 G1–G10）
   - 全过：按**该代码库自己的提交细则**提交到分支 bugfix/zentao-<禅道ID>，取得真实修订号
   - 任一条不过或需要业务决策：不提交，仍要写分析，跳到第 6 步的 block 分支
6) 把分析结论写成 JSON 文件（字段规范见 §2.1，gates 必须逐条给结论），然后回本系统登记
   已提交并拿到真实修订号：
     python -m app.cli commit <禅道ID> --message "<一句话根因>" --files a.c,b.c --summary "<改了什么>" --verify "<人工怎么验>" --no-svn --revision <真实修订号> --analysis-file a.json
   已改好但提交还需你授权/排队等批次：
     python -m app.cli commit <禅道ID> ... --no-svn --revision PENDING --analysis-file a.json --extra "待主人按仓库提交细则提交"
   卡住需要方案（question/options/advice 之外照样给分析）：
     python -m app.cli block <禅道ID> --question "<卡在哪一步>" --options "方案A：…；方案B：…" --advice "<建议及理由>" --analysis-file a.json
7) 回报字段：zentao_id / product / 改了哪些文件 / 闸门逐条结论 / analysis_id / action(commit|pending-commit|block) / revision 或 need_id
禁止：没写分析就 commit（REQUIRE_ANALYSIS 会直接拒绝）；提交或合并 trunk；resolve/close 禅道 bug；
改无关文件；把 .env 内容写进任何输出；问我是否继续
```

### 3.3 收工后你的三步（审 + 授权提交 + 回填）

```
# 1) 看这轮结果与待办
python -m app.cli report

# 2) 对登记成 PENDING 的条目，逐条读它的分析结论 + files + summary + verify：
python -m app.cli status <禅道ID>       # 输出里的 analysis 就是 AI 写的分析（gates_parsed 是闸门逐条）
#    按代码库自己的提交细则决定要不要提交、怎么提交（本系统不替你判）

# 3) 真实提交后回填修订号（分析已在库里，不会再被闸门拦；会顺带再发一条禅道评论，文案用 --extra 控制）
python -m app.cli commit <禅道ID> --message "<一句话根因>" --files a.c,b.c --no-svn --revision <真实修订号>
```

## 4. 为什么它能「自动下一个」而不会走偏

1. **状态在库里，不在对话里**：进度是 `bugs.status`；关窗、断线、换会话后重贴 §3.1 就是断点续跑。
2. **卡点不阻塞**：`block` 把 bug 踢到 `need_solution`，自动掉出队列；你答复后它以最高优先级回来。
3. **提交不了也不阻塞**：闸门未过 → `--revision PENDING` 登记或直接 `block`，循环继续，缺口在 `/review`、`/need` 看得见。
4. **分析结论是硬交付**：`REQUIRE_ANALYSIS=true` 时没有分析的 `commit` 会被系统拒绝，
   执行器只能「先写分析再收工」，你审查时才有东西可读——这条不靠自觉，靠命令返回码。
5. **优先级不用你指定**：`tasks` 固定排序 = 已答复 > 已打回 > 待处理，再 `pri` 升序 / `severity` 降序。
6. **批次可调**：`.env` 的 `AI_BATCH_SIZE`（默认 5）是约定值；临时改批量直接 `tasks --limit N`。
7. **退出条件由数据决定**：`tasks --limit 1` 返回空 = 没有可处理项，此时才允许停下汇报。

## 5. 并行的唯一硬约束

```
同产品：并发度 = 1（同一份工作副本，一条提交完才能下一条动分支）
跨产品：可以并行（各产品独立仓库与工作副本，互不影响）
```

想加速：按产品开多个主对话，都用 §3.1 但加一句「只处理 product_id=<X> 的 bug」，取队列后自行过滤：

```
python -m app.cli tasks --limit 20      # 只挑 product_id 等于本产品的那几条
```

## 6. 可选方式（托管 svn）的额外前置

只有在目标代码库**没有**「本机禁 svn / 远端只读」这类门禁、且允许本系统代跑时才用：

```
python -m app.cli bind-repo <产品ID> "<SVN仓库根地址>" --name "<产品名>" --build "<编译命令>" --test "<测试命令>"
python -m app.cli svn-check --product <产品ID>     # 仓库可达 / trunk 存在 / 工作副本归属
python -m app.cli branch <禅道ID> --dry-run        # 确认 repo_source=product、working_copy 是预期目录
```

工作副本留空即自动分配 `svn_workspace/p<产品ID>-<仓库哈希>`，不占用你日常开发那份代码。
此时子任务把 §3.2 的第 5、6 步换成：`branch <禅道ID>` → 改码 → `commit <禅道ID> --message ... --files ...`（不带 `--no-svn`）。

## 7. 跑起来后你在看什么

```
Web /sync     同步与详情抓取留痕（含禅道评论是否写入成功）
Web /         看板：pending 变少、fixing/await_review/need_solution 变多就是在正常推进
Web /need     要你给方案（答复后自动回到 AI 队列最前）
Web /review   待你审查：AI 分析结论 + 闸门逐条 + files_changed / fix_summary / verify_steps / 修订号
              （PENDING 表示尚未真实提交；缺分析会标红提示）
Web /repos    各产品用哪个仓库、工作副本在哪、build/test 命令
命令 python -m app.cli report            进度：counts + 三份清单 + remaining_work
命令 python -m app.cli tasks --limit 5   下一条会被处理的是谁
```

## 8. 异常与回退

| 现象 | 处理 |
| --- | --- |
| `commit` 报「提交前必须把分析结果写入系统」 | 子任务没交分析结论。补 `analyze <ID> --kind commit --analysis-file a.json`（字段见 §2.1）再 commit；确实写不出来就该转 `block` |
| 某条被 AI 反复处理不满意 | `/review` 填原因「打回」→ `rejected`，自动回队列且优先级提升 |
| 某条想先搁置 | 让它 `need_solution`，或直接在库里改状态，它就不在队列里 |
| PENDING 一直没人提交 | 它已在 `await_review`，`report` 与 `/review` 都能看到；不提交就不会变 `merged` |
| 误改了别的文件 | 只有当轮改动的那份工作副本受影响，按代码库规程 `svn revert`（本系统不自动 revert） |
| 禅道评论写不进去 | 不影响主流程，原文留在 `sync_log`（`/sync` 页可见）；之后用 `comment <ID> --text ...` 单独补发 |
| 中途关窗/IDE 重启 | 重贴 §3.1，状态从库里恢复，不会重复领任务 |
| 全队列跑完 | `report` 的 `remaining_work=0`，只剩你的审查与合入 trunk（手动） |

## 9. 一次完整实操（默认方式）

```
# 你（一次性）
python -m app.cli bind-repo 25 "<DPDK/NGFW 仓库根地址>" --name "DPDKUAC&NGFW" --build "<编译命令>" --test "<测试命令>"
python -m app.cli repos                       # 确认产品 25 已 bound
# 若代码库工作副本另有其人（在你日常那份代码里），把 repo_url 填对即可，本系统不会去动它的 svn

# 你（下达任务）：粘 §0 或 §3.1

# AI（自动）
sync --details -1 → tasks → 每条一个子任务（status→claim→改→编译测试→闸门）
   → 按代码库细则提交并拿真实修订号 → commit --no-svn --revision <号> → 下一条 → report

# 你（收尾）
/need 答复 → /review 逐条审查 → 手动 svn merge 到 trunk → merged 卡片点「标记已结案」
```
