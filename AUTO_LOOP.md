# 自动连续执行下达手册（一条跑完自动下一条）

**职责边界（本系统的唯一定位）**

| | 谁负责 | 内容 |
| --- | --- | --- |
| 任务管理 | **本系统（AI-BUG）** | 拉禅道、抓截图/备注/附件、排队与优先级、占任务、状态流转、登记改动与修订号、审查流、回写禅道评论 |
| 提交资格与提交动作 | **代码库自己的知识体系**（目标仓库工作区的 `CLAUDE.md` 及其 `registry/playbooks/skills`） | 什么情况下允许提交、怎么走 SSH、要不要征求同意、`svn ci` 怎么执行、真实修订号从哪来 |
| 远端写入（进正式库） | **只有主人** | `git svn dcommit` / `git push` / `svn ci` / merge 到 trunk —— 执行器一律不碰，见 §1.5 与闸门 G11 |

本系统**不做提交决策、默认也不执行 svn**。它只把一份「提交闸门」清单交给执行器；
执行器在代码库侧按自己的细则完成**本地**提交（`git commit`），回到本系统只做一件事：
`commit --no-svn --revision git:<哈希>` 登记。改动要进正式库那一步只由主人做（见 §1.5、G11）。

本文档只管「怎么下达任务」；执行协议本体在 `AGENTS.md`，接口细节在 `CAPABILITIES.md`。

---

## 0. 最短下达语（直接粘）

不必手抄：打开 Web 的「下达任务」页 `/dispatch`，本节与 §3.1 / §3.2 / §9 的提示词都是从这份文件
现场读出来的（规则改了页面跟着改），每段一个复制按钮；页面还会**按落码目录（或你勾选的产品）生成
已经填好产品 ID、镜像目录与串行要求的下达语**——一个目录一条，粘给一个主对话。顶部另有开跑前检查清单。

```
读 AGENTS.md 与 AUTO_LOOP.md，无人值守处理禅道 bug，按 AUTO_LOOP.md 的【提交闸门 G1–G12】判定可否提交：
1) python -m app.cli sync --details -1     （同步 + 全量抓截图/备注/附件）
2) python -m app.cli tasks --limit 5       （按镜像 working_copy 分组：共用同一镜像的产品并入同一条串行队列，
   只有镜像互不相同的组之间才并行）
3) 每条 bug 起一个独立子任务，用 AUTO_LOOP.md §3.2 模板，只替换禅道ID
4) 落码位置是 git-svn 镜像（AUTO_LOOP.md §1.5）；镜像未就绪就不许改码，也不许动正式 SVN 工作副本
5) 子任务只做：验镜像就绪 → status(看图看备注) → claim → 镜像里开 bugfix/zentao-<ID> 定位改码
   → SSH 编译/测试 → 逐条核对提交闸门
6) 闸门全过 → 只在镜像里 git commit（本地），把短哈希当修订号登记，并一起写入分析结论
   （--analysis-file a.json，字段见 §2.2）：
   commit --no-svn --revision git:<哈希> --analysis-file a.json；
   闸门任一条不过 → block 回填（也带上分析），继续下一条
7) 一批跑完立刻取下一批，禁止问我是否继续；tasks --limit 1 返回 0 条才停并输出 report
本系统不执行 svn、不判定提交细则；没写分析结论不许 commit；
禁止 git svn dcommit / git push / svn ci / 提交或合并 trunk（进正式库只由我做）；
不许 resolve/close 禅道 bug；不许改与当前 bug 无关的文件

多语言词条是唯一例外（G12，见 §2.1）：改 .po/.mo 前必须先
`python -m app.cli i18n-up <禅道ID> --files <词条文件>`，改完立刻
`python -m app.cli i18n-commit <禅道ID> --files <同一批文件> --message "<说明>"` 单独提交进 SVN；
不许把词条攒进镜像分支、不许和代码混在一次提交里、除这两个命令外不许碰任何 svn 子命令
```

## 1. 三种执行方式（本工程默认第三种）

| 方式 | 提交动作由谁做 | 本系统命令 | 适用 |
| --- | --- | --- | --- |
| 只管任务 | 执行器按**代码库知识体系**的提交细则做（含 SSH 门禁、同意流程、真实修订号） | `claim` → 提交 → `commit --no-svn --revision <真实号> --analysis-file a.json` | 有自己规则体系的仓库（如带 `CLAUDE.md` 的工作区） |
| 托管 svn | 本系统代跑 `svn copy/switch/commit` | `branch <ID>` → `commit <ID> --files ...` | 无门禁的独立工作副本（`svn_workspace/p<产品ID>-<hash>`），想彻底无人值守时 |
| **本地 git 草稿**（本工程采用） | 执行器只在 git-svn 镜像里 `git commit`，**不推任何远端**；进正式库由主人事后做 | `claim` → 镜像里 `git commit` → `commit <ID> --no-svn --revision git:<哈希> --analysis-file a.json` | 仓库带 git-svn 镜像、要求「改动先落地可见、正式库在你点头之前不动」（见 §1.5） |

三种方式的**任务管理部分完全一样**（排队、详情、状态、审查、禅道回填），差别只在「改动落在哪、由谁落」。
换方式不需要换文档，只换子任务里第 5 步那一条命令。

## 1.5 本地 git 草稿仓（当前代码库的落码位置）

**这是执行器唯一允许改代码的地方，不是正式工作副本。**

| 项 | 值 |
| --- | --- |
| 镜像仓库 | `<镜像目录>`（真实路径写在该产品的 `product_repos.working_copy`，`/repos` 页可见；**不写进本文档**） |
| 上游 | 该产品的 `repo_url`（一个 git-svn 镜像；路径级授权时 stdlayout 不可用，clone 要带 `--trunk=.`） |
| 正式 SVN 工作副本 | 你日常开发那一份（路径 = `product_repos.svn_working_copy`）—— **执行器只读对待，禁止在其中改码**；只有多语言词条按 §2.1 的 G12 通道例外 |
| 本地分支名 | `bugfix/zentao-<禅道ID>`（与 `BRANCH_PREFIX` 一致） |

三条硬规矩：

1. **只 commit，不推远端**：允许 `git checkout -b` / `git add` / `git commit` / `git diff` / `git log`；
   禁止 `git svn dcommit`、`git push`、`svn ci`、`svn copy`。镜像的 `fetch`（拉 SVN→git）由主人跑，执行器不代跑。
   唯一例外是 §2.1 的多语言通道：词条文件由 `i18n-up` / `i18n-commit` 两条命令代跑 `svn update/ci`。
2. **登记时用 git 哈希，不带 `r` 前缀**：`--revision git:<7位短哈希>`，与本系统的 SVN 修订号用两种形态区分，
   审查页一眼能看出这条是「本地草稿」还是「已进正式库」。
3. **镜像未就绪时不许开工**：`git -C <镜像> rev-parse --verify refs/remotes/origin/trunk` 失败、
   或 `refs/remotes/origin/trunk` 为空（clone/fetch 还在跑）→ 不改码、不转去动正式工作副本，
   直接 `block` 写明「镜像未就绪」并继续下一条。

为什么这么分：SVN 没有本地提交，`svn ci` 一跑就进正式库；git 有。用镜像当草稿区，就把「AI 已改完」
和「已进正式库」这两件事拆成了两个独立动作 —— 前者随时可 `git reset` 回退，后者只在人点头后发生。

代价（必须知道，别当透明）：

- 镜像与正式工作副本是**两份独立副本**，改一份不会同步到另一份；最终回灌靠主人 `git svn dcommit`
  或 `git diff` 出 patch 打到正式副本，本系统不代做。
- 两边共用同一个 `.git` 目录（跨 Samba）时**只能有一个操作方**，git 的文件锁在 SMB 上不可靠；
  所以「同一镜像串行」是硬约束，且划分单位是镜像而不是产品 —— 两个产品绑定到同一个镜像时，
  它们必须共用一条串行队列（见 §5）。Windows 侧与编译服务器侧同样不得同时动镜像。
- 编译与实测仍只能在 SSH 编译服务器上做（闸门 G4 不变），镜像目录在 Windows 本地编译不了。
- 绑定关系仍要落进本系统，G3 才有判据（真实地址与镜像路径存 `product_repos`，不写进文档）：
  `python -m app.cli bind-repo <产品ID> "<SVN仓库地址>" --name "<产品名>" --working-copy <镜像目录>`

## 2. 提交闸门（下达给执行器的「什么情况下可以提交」）

闸门全过才允许提交；**任一条不过 → 走 `block` 回填需方案，绝不「先提交再说」**。

| # | 条件 | 本系统提供的判据 |
| --- | --- | --- |
| G1 | 已看过禅道的完整描述、**截图**、备注/操作记录 | `status` 的 `steps` / `attachments[].local_path` / `comments`；缺则先 `detail <ID>` |
| G2 | 根因明确、修改点唯一（或已按主人 `owner_reply` 执行） | `status` 的 `owner_reply` 与历史提交 |
| G3 | 只改与本 bug 直接相关的文件，且都在**该 bug 所属产品的工作副本**内（git 方式下 = `repos` 给出的镜像目录，不是正式工作副本） | `status` 的 `product_id` + `repos` 的产品→仓库映射（含 `working_copy`） |
| G4 | 该产品的编译/测试实际通过（连续 2 次不过即视为不过） | `repos` 的 `build_command` / `test_command`（未绑定则按代码库自身构建规则） |
| G5 | 状态矩阵自审完成：主路径 / fallback 与异常路径 / 资源释放收尾 / 日志计数 / 同构镜像分支 | 无（由代码库知识体系的 precheck 规程负责，本系统只要求你显式声明已核对） |
| G6 | 触及页面文案就同步 `i18n.po/.mo`；页面源码不直接写中文、注释全英文；不引入超出语言基线的语法 | 无（同上，属代码库规则）；词条文件本身走 G12，不进镜像草稿 |
| G7 | 提交目标是 `bugfix/zentao-<禅道ID>` 分支，**不是 trunk**（git 方式下=本地分支，且只是一次 `git commit`） | `status` 的 `branch`；本系统侧还有 `SVN_ALLOW_TRUNK_WRITE=false` 兜底 |
| G8 | commit message = `fix #<禅道ID> <一句话根因>`，不含账号密码等敏感串 | `commit --message` 拼接规则 |
| G9 | 未验证的路径必须标「未验证」，不得按「已完成 / 可直接合入」收口 | 登记内容进 `fix_summary` / `verify_steps` 供你审查 |
| G10 | 提交所需的授权已按代码库规程取得（如 SSH 写操作需显式同意） | 无（属代码库规则；本系统只要求拿到真实修订号再登记） |
| G11 | **远端写入留给主人**：执行器只做本地动作（`git commit` / 改工作副本不 `ci`），绝不 `git svn dcommit`、`git push`、`svn ci`、merge 到 trunk。**唯一例外是 G12 的多语言通道**，其余任何文件都不适用 | 无（本系统不检测，靠禁止事项 + 登记形态 `git:<哈希>` 让审查页一眼可辨） |
| G12 | **多语言词条走独立快车道**：词条文件不与代码一起提交，改前先 up、改完立即单独 `svn ci` 进正式库（详见 §2.1） | `i18n-up` / `i18n-commit` 两条命令；`repos` 的 `svn_working_copy`（正式 SVN 工作副本）没配就无法执行 |

> G5 / G6 / G10 这三类是**代码库自己的细则**，本系统不复制也不解释它们，只在闸门清单里点名要求 ——
> 细则原文以目标仓库工作区的 `CLAUDE.md` 与其下层 `registry/playbooks` 为唯一权威。
> G12 是 G11 的**唯一例外**，且例外只覆盖白名单（`I18N_FILE_PATTERNS`）命中的文件。

## 2.1 多语言专用通道（G12：为什么它不参与草稿/审查）

**为什么单开一条道**：`.po/.mo` 这类词条文件是全库追加型文本，谁都在改。
把它们压在镜像的本地分支里等审查、等回灌，攒上几小时基本必然和别人的词条冲突 ——
冲突还得在 git 草稿里解，解完还得重登记，等于把最没风险的文件变成最贵的文件。
反过来，**新增词条对已发布版本没有影响**（不改逻辑、不改行为，最多是某句文案还没翻），
所以它的风险≈0，而成本（冲突）随时间线性上涨。结论：**单独提交 + 立刻提交**。

一条 bug 的词条按这个顺序走，全程在**正式 SVN 工作副本**（`repos` 的 `svn_working_copy`，
不是 git 镜像 —— 镜像里没有 `.svn`，提交不了）：

```
1) python -m app.cli i18n-up <禅道ID> --files <该 bug 要改的词条文件，逗号分隔>
   → 系统代跑 `svn update`：改动前先把词条拉到最新，并把冲突挡在这里
   → 返回 conflict=true 就停手：`block` 写「多语言冲突待人工处理」，不许硬解 .mo
2) 在正式工作副本里只改这些词条文件（源码改动仍留在镜像分支上，两件事互不夹带）
3) python -m app.cli i18n-commit <禅道ID> --files <同一批文件> --message "<一句话说明>"
   → 逐条校验：必须全部命中 I18N_FILE_PATTERNS，必须都在这个副本内，必须真的有本地改动
   → 再 up 一次（缩到最小冲突窗口）→ `svn ci` 只提这几个文件 → 真实 r 号自动登记 + 回写禅道评论
   → 想先看校验结果不真提交：加 --dry-run
```

四条硬约束（命令层已经拦，别指望绕过）：

1. **一次只提词条**：`i18n-commit` 的 `--files` 里混进任何一个非词条文件（`.c`/`.py`/`.js`…），
   整条命令直接失败 —— 这条通道不可能把代码带进正式库。
2. **不许攒**：词条改完必须在这条 bug 收尾前 `i18n-commit`。攒到本地分支里 = 违反 G12，
   与「先提交再说」同等对待：`block` 说明，不要偷偷留在工作副本里。
3. **不改版本相关的东西**：只允许新增/修正词条本身；动 `Project-Id-Revisions`、
   语言包结构重组、删词条，都属于「对版本有影响」，回去走正常闸门 + 主人回灌。
4. **不占版本判定**：`i18n-commit` 登记的 `r<号>` 只是词条落库留痕，**不代表这条 bug 已完成** ——
   bug 状态不变，代码改动照旧走镜像 `git commit` + `commit --no-svn --revision git:<哈希>`，
   审查页看到的是两笔记录（词条已进正式库 + 代码还是草稿）。

正式 SVN 工作副本是**你和执行器共用的第三份目录**（镜像之外），所以：
词条提交期间别在自己那份副本里改同一个文件；同一产品的两条 bug 也别并行提词条（见 §5）。

```
python -m app.cli bind-repo <产品ID> "<SVN仓库地址>" --working-copy <镜像目录> --svn-working-copy <正式SVN工作副本>
python -m app.cli repos          # 看 svn_working_copy 是否已配好
```

## 2.2 分析结论（bug 完成时必须写进系统的交付物）

子任务结束 = 这条 bug 结束，**分析结论是必交付物**：`REQUIRE_ANALYSIS=true`（默认）时，
没有分析结论的 `commit` 会直接被拒。写法两种：

```
# 写法一：随 commit 一起提交（推荐，一条命令搞定）
python -m app.cli commit <禅道ID> --message "<一句话根因>" --files a.c,b.c --no-svn --revision git:<短哈希> --analysis-file a.json

# 写法二：先单独写分析，再 commit
python -m app.cli analyze <禅道ID> --kind commit --analysis-file a.json
python -m app.cli commit <禅道ID> --message "..." --files a.c,b.c --no-svn --revision git:<短哈希>
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
  "gates":       {"G1": "pass", "G5": "未验证:OEM 同名页未核对", "G7": "pass", "G11": "pass:只本地 commit，未 dcommit", "G12": "pass:词条已 i18n-commit r12345"}
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
  b. 按镜像分组（python -m app.cli repos 看每个产品的 working_copy）：共用同一 working_copy 的产品
     合并成一条串行队列，严格做完一条才动下一条；只有 working_copy 互不相同的组之间才并行开子任务
  c. 每条 bug 起一个子任务，用 §3.2 模板，只替换 <禅道ID>
  d. 收集子任务返回的 JSON，按 zentao_id 记账；一条失败不影响下一条
  e. 回到 a，不等待我确认
收工：python -m app.cli report，给我三份清单：
  - 已提交待审查（我要去 /review）
  - 需我给方案（我要去 /need 答复）
  - 闸门未过/未提交及原因
期间不要复述规则、不要问我是否继续、不要碰 trunk、不要推远端（dcommit/push/svn ci）、不要替代码库判定提交细则。
唯一例外是词条：改了 .po/.mo 就按 §2.1 用 i18n-up + i18n-commit 单独提交，其余文件一律不碰 svn。
```

### 3.2 子任务指令（每条 bug 一份，无上下文也能独立执行）

```
你只处理禅道 bug #<禅道ID>，做完立即结束，禁止顺带处理其他 bug。
任务管理命令的工作目录：<项目目录>
落码位置：镜像仓库 <镜像路径>（见 §1.5）；正式 SVN 工作副本只读，禁止在里面改码
        —— 唯一例外：多语言词条文件按 G12 走 §2.1 的通道，改前先 up、改完立刻单独提交

0) 先验镜像就绪（不就绪就别开工）：
   git -C <镜像路径> rev-parse --verify refs/remotes/origin/trunk
   失败或该 ref 为空 = clone/fetch 还没跑完 → 直接跳到第 7 步 block，原因写「镜像未就绪」
1) python -m app.cli status <禅道ID>
   读 product_id / product_name / steps / comments(备注) / attachments[].local_path / 历史提交 / owner_reply
   → 有截图必须用 Read 打开 local_path 真正看图，只看文字就动手是最常见的误判来源
   → 若 attachments 为空且没抓过详情：python -m app.cli detail <禅道ID>
2) python -m app.cli claim <禅道ID>        # 占住任务，置 fixing，避免被别的子任务重复领
3) 在镜像里为这条 bug 开本地分支，然后按该代码库自己的知识体系定位修改点（先读它的 CLAUDE.md / 首跳规程）
   git -C <镜像路径> checkout -b bugfix/zentao-<禅道ID> refs/remotes/origin/trunk
   只改与这条 bug 直接相关的文件，且路径必须属于这条 bug 的产品仓库（闸门 G3）
4) 编译 / 测试：用该库规定的构建方式（本系统 `repos` 里的 build_command / test_command 可作参考）
   本工程只能在 SSH 编译服务器上实测；连续 2 次不过 → 直接走第 7 步 block，写清失败现象，不要硬试
5) 多语言词条（改了 .po/.mo 才做，G12；镜像分支里不留词条文件）
   python -m app.cli i18n-up <禅道ID> --files <词条文件>          # 改之前先 up
   → conflict=true → 停手，走第 7 步 block，原因写「多语言冲突待人工处理」，不许自己硬解 .mo
   在正式 SVN 工作副本里只改这些词条文件，改完立即单独提交（不许攒、不许跟代码混一次提交）：
   python -m app.cli i18n-commit <禅道ID> --files <同一批文件> --message "<一句话说明>"
   → 输出里的 r<号> 就是词条的真实修订号，稍后写进分析结论的 change_desc / evidence
6) 提交判定（§2 的 G1–G12）
   - 全过：只在镜像里做一次本地提交，并记下短哈希
     git -C <镜像路径> add <改的文件> ; git -C <镜像路径> commit -m "fix #<禅道ID> <一句话根因>"
     git -C <镜像路径> rev-parse --short HEAD
   - 禁止：git svn dcommit / git push / svn ci（G11，进正式库只由主人做；词条已由 i18n-commit 单独提过）
   - 任一条不过或需要业务决策：不提交，仍要写分析，跳到第 7 步的 block 分支
7) 把分析结论写成 JSON 文件（字段规范见 §2.2，gates 必须逐条给结论，含 G11/G12），然后回本系统登记
   已本地提交（把 git 短哈希当修订号登记，前缀 git:）：
     python -m app.cli commit <禅道ID> --message "<一句话根因>" --files a.c,b.c --summary "<改了什么>" --verify "<人工怎么验>" --no-svn --revision git:<短哈希> --analysis-file a.json
   改好了但不便开分支/哈希还没定（占位待你处理）：
     python -m app.cli commit <禅道ID> ... --no-svn --revision PENDING --analysis-file a.json --extra "待主人按仓库提交细则提交"
   卡住需要方案（question/options/advice 之外照样给分析）：
     python -m app.cli block <禅道ID> --question "<卡在哪一步>" --options "方案A：…；方案B：…" --advice "<建议及理由>" --analysis-file a.json
8) 回报字段：zentao_id / product / 分支名 / 改了哪些文件 / git 短哈希 / 词条修订号 r<号>（若有）/
   闸门逐条结论 / analysis_id / action(commit|pending-commit|block) / revision 或 need_id
禁止：没写分析就 commit（REQUIRE_ANALYSIS 会直接拒绝）；git svn dcommit / git push / svn ci / 提交或合并 trunk；
在正式工作副本里改码（词条文件除外，且必须走 i18n-up / i18n-commit 两条命令）；
把词条文件留在镜像分支里不提交、或用 i18n-commit 夹带任何非词条文件；
resolve/close 禅道 bug；改无关文件；把 .env 内容写进任何输出；问我是否继续
```

### 3.3 收工后你的三步（审 + 回灌正式库 + 回填）

```
# 1) 看这轮结果与待办
python -m app.cli report

# 2) 逐条读 AI 的分析结论 + files + summary + verify，并审它本地的提交：
python -m app.cli status <禅道ID>       # 输出里的 analysis 就是 AI 写的分析（gates_parsed 是闸门逐条）
git -C <镜像目录> show <短哈希>          # 真实改动长什么样，这里看
git -C <镜像目录> diff refs/remotes/origin/trunk..<分支名>   # 整条分支相对 trunk 的全部改动

# 3) 你判定可以进正式库，就自己做回灌（这一步执行器绝不代做），拿到真实 r 号后回填：
git svn dcommit          # 在镜像目录里执行；或出 patch 打到正式工作副本再 svn ci
python -m app.cli commit <禅道ID> --message "<一句话根因>" --files a.c,b.c --no-svn --revision r<真实号>
```

回填是**追加**：`git:<哈希>`（本地草稿）与 `r<号>`（正式库）会同时挂在这条 bug 名下，
`status` 与审查页能看到它从草稿到落库的完整轨迹。
登记成 `PENDING` 的条目同理，只是还没有可信的本地哈希可审，得先看 `files` 与工作副本状态。

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
同一镜像 / 工作副本：并发度 = 1（不管它挂了几个产品，一条提交完才能下一条动分支）
不同镜像 / 工作副本：可以并行（每个镜像各自一条串行队列）
```

串行队列的划分单位是 **`working_copy`（镜像目录），不是产品 ID**。两个产品只要共用同一份镜像，
就必须并入同一条串行队列：一份工作树同一时刻只能 checkout 一个分支，前一条 bug 的
`git checkout -b` 会把另一条正在改的工作树一起切走，两个产品的改动会互相污染、
`git add` 也会串味。判定方法：`python -m app.cli repos`，看 `working_copy` 是否相同。

用 §1.5 的 git 镜像时这条约束还要往外扩一层：镜像的 `.git` 跨 Samba 共享时 git 的文件锁不可靠，
**Windows 侧与编译服务器侧绝不能同时操作同一个镜像**。所以：动镜像前先确认没有另一侧在跑
`git` / `git svn fetch`。

多语言通道再加一层：`svn_working_copy`（正式 SVN 工作副本）是**第三份共享目录**，
串行单位从「镜像」扩到「镜像 + 正式副本」—— 同一产品的两条 bug 不许并行提词条
（两个 `i18n-commit` 会同时 up/ci 同一批 `.po`），执行器改词条时你也别在同一副本里改同一个文件。
命令层已把 up→ci 收在一条 `i18n-commit` 里（窗口越小越不容易撞），但同目录仍只允许一个写入方。

想加速，先按镜像归组，再决定开几个主对话：

```
python -m app.cli repos          # 逐产品看实际生效的 working_copy
```

- `working_copy` **相同**的那组产品 → 只能交给**一个**主对话串行做，下达语里写
  「只处理 product_id ∈ {<该组全部产品ID>} 的 bug」，取队列后自行过滤：

  ```
  python -m app.cli tasks --limit 20      # 只挑 product_id 属于该组的那几条
  ```

- `working_copy` **不同**的产品 → 每个镜像各开一个主对话，都用 §3.1，但都要写死自己那组产品 ID，
  不允许跨组接手（否则又会撞到同一镜像上的另一条队列）。

要让共用的两个产品真正并行，只有一个办法：给其中一个单独 clone 一份镜像，再
`bind-repo <产品ID> <仓库根地址> --working-copy <新镜像目录>` 把它挪过去。在此之前，
「跨产品可并行」只对**镜像不同**的产品成立。

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
              （修订号形态：git:<哈希>=已本地提交未进正式库；r<号>=已进正式库；PENDING=还没提交；
                branch=i18n-direct 的那条是词条已单独提交 SVN（G12），不代表代码已回灌；
                缺分析会标红提示）
Web /repos    各产品用哪个仓库、镜像在哪、正式 SVN 工作副本（多语言通道用）在哪、build/test 命令
命令 python -m app.cli report            进度：counts + 三份清单 + remaining_work
命令 python -m app.cli tasks --limit 5   下一条会被处理的是谁
```

## 8. 异常与回退

| 现象 | 处理 |
| --- | --- |
| `commit` 报「提交前必须把分析结果写入系统」 | 子任务没交分析结论。补 `analyze <ID> --kind commit --analysis-file a.json`（字段见 §2.2）再 commit；确实写不出来就该转 `block` |
| `i18n-commit` 报「不在多语言白名单内」 | `--files` 里混进了代码文件：把词条与代码分开，代码回镜像走闸门流程，别指望这条通道 |
| `i18n-commit` 报「没有本地改动」 | 词条改在了镜像目录而不是正式 SVN 工作副本。回 §2.1：改前先 `i18n-up`，改在 `svn_working_copy` 那份副本里 |
| `i18n-up` / `i18n-commit` 报冲突 | 不许硬解（尤其 `.mo` 是二进制）：`block` 写「多语言冲突待人工处理」，把冲突文件名写进 `--question`，继续下一条 |
| 报「未配置正式 SVN 工作副本」 | 该产品的 `svn_working_copy` 是空的：`bind-repo <产品ID> "<SVN仓库地址>" --svn-working-copy <正式SVN工作副本>` 补上；没补之前词条按老规矩留在镜像里，`block` 说明 |
| 镜像 `refs/remotes/origin/trunk` 不存在或为空（clone/fetch 未完成） | 执行器**不许改码**，也不许转去动正式工作副本；`block` 写明「镜像未就绪」，等主人把 `git svn clone/fetch` 跑完 |
| 本地 git 分支攒了一堆 commit 没进正式库 | 正常状态（G11 设计如此）。由主人 `git -C <镜像> diff origin/trunk..<分支>` 审，通过后自行 `git svn dcommit` 或出 patch 打到正式副本，再回填真实修订号 |
| 工作树/分支基线莫名变化，或 `git status` 里冒出**别的产品/别的禅道 ID** 的文件 | 两个产品共用了同一份镜像却被并行处理（违反 §5）：立刻停掉其中一条队列，`git -C <镜像> status` 核对，被串味的分支 `git reset --hard <基线>` 重做；要让它们真并行只能给其中一个另开一份镜像 |
| 某条被 AI 反复处理不满意 | `/review` 填原因「打回」→ `rejected`，自动回队列且优先级提升 |
| 某条想先搁置 | 让它 `need_solution`，或直接在库里改状态，它就不在队列里 |
| PENDING 一直没人提交 | 它已在 `await_review`，`report` 与 `/review` 都能看到；不提交就不会变 `merged` |
| 误改了别的文件 | 镜像里 `git reset --hard <分支基线>` 或 `git checkout -- <文件>` 即可回退（不影响正式库）；正式工作副本里则由主人按代码库规程 `svn revert`（本系统不自动 revert） |
| 禅道评论写不进去 | 不影响主流程，原文留在 `sync_log`（`/sync` 页可见）；之后用 `comment <ID> --text ...` 单独补发 |
| 中途关窗/IDE 重启 | 重贴 §3.1，状态从库里恢复，不会重复领任务 |
| 全队列跑完 | `report` 的 `remaining_work=0`，只剩你的审查与合入 trunk（手动） |

## 9. 一次完整实操（§1.5 本地 git 草稿方式）

```
# 你（一次性，且必须在有 git-svn 的那一侧跑 —— 编译服务器上通常没有 git-svn）
git svn clone --trunk=. --no-metadata <SVN仓库地址> <镜像目录>
git -C <镜像目录> rev-parse --verify refs/remotes/origin/trunk     # 有输出才算就绪
python -m app.cli bind-repo <产品ID> "<SVN仓库地址>" --name "<产品名>" --working-copy <镜像目录> --svn-working-copy <正式SVN工作副本> --build "<编译命令>" --test "<测试命令>"
python -m app.cli repos                       # 确认该产品已 bound、working_copy 指向镜像、svn_working_copy 已配

# 你（下达任务）：打开 /dispatch 复制 §0 或 §3.1（也可照抄下面这段）

# AI（自动）
sync --details -1 → tasks → 每条一个子任务
   （验镜像就绪 → status → claim → 在镜像开 bugfix/zentao-<ID> → 改 → SSH 编译测试
     → 改了词条就 i18n-up → 改词条 → i18n-commit（单独进 SVN，拿 r<号>）
     → 闸门 G1–G12 → git commit（只本地）→ commit --no-svn --revision git:<哈希> --analysis-file a.json）
   → 下一条 → report

# 你（收尾）
/need 答复 → /review 逐条看分析与闸门 → git -C <镜像目录> log/diff 审改动
   → 通过后自己执行 git svn dcommit（或出 patch 打到正式副本再 svn ci）→ 拿到真实 r 号后回填：
python -m app.cli commit <禅道ID> --message "<一句话根因>" --files a.c,b.c --no-svn --revision r<真实号>
   → 是否 merge 到 trunk 由你决定 → merged 卡片点「标记已结案」
```

回填那一步为什么可以重复 `commit`：`svn_revisions` 对同一个 bug 是**追加**而不是覆盖，
`git:<哈希>`（本地草稿）与 `r<号>`（正式库）会并存在这条 bug 名下，审查记录是完整的。
