# 自动连续执行下达手册（一条跑完自动下一条）

**职责边界（本系统的唯一定位）**

| | 谁负责 | 内容 |
| --- | --- | --- |
| 任务管理 | **本系统（AI-BUG）** | 拉禅道、抓截图/备注/附件、排队与优先级、占任务、状态流转、登记改动与修订号、审查流、回写禅道评论 |
| 提交资格与提交动作 | **代码库自己的知识体系**（目标仓库工作区的 `CLAUDE.md` 及其 `registry/playbooks/skills`） | 什么情况下允许提交、怎么走 SSH、要不要征求同意、`svn ci` 怎么执行、真实修订号从哪来 |
| 远端写入（进正式库） | **只有主人** | `git svn dcommit` / `git push` / `svn ci` / merge 到 trunk —— 执行器一律不碰，见 §1.5 与闸门 G11；主人侧点审查弹窗的「正式推入 SVN」（§3.3） |

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
   看镜像、开分支、落草稿一律走本系统命令：`mirror` / `checkout` / `draft`（§1.8）——
   它们内部就是 SSH 侧带 `-c core.ignorecase=false` 的 git，Windows 侧那份只读代码不跑 git
   （本库 109 组只差大小写的路径会被 ignorecase 混成一个）
5) 子任务只做：`mirror <ID>` 验镜像就绪 → status(看图看备注) → claim → `checkout <ID>` 开/切分支
   → 先读该库自带的 AI 知识体系
   （镜像根 CLAUDE.md 常驻层 → `.trae/` 指向的知识根 registry|doc|agents|playbooks，见 §1.6）
   再定位 → 改码 → SSH 编译/测试 → 逐条核对提交闸门
6) 闸门全过 → `python -m app.cli draft <ID> --message "fix #<ID> <根因>" --files a,b`
   落草稿提交（只 add 列出的文件，禁止 add -A），把返回的短哈希当修订号登记，
   并一起写入分析结论（--analysis-file a.json，字段见 §2.2）：
   commit --no-svn --revision git:<哈希> --analysis-file a.json；
   闸门任一条不过 → block 回填（也带上分析），继续下一条
7) 一批跑完立刻取下一批，禁止问我是否继续；tasks --limit 1 返回 0 条才停并输出 report
   一批收尾后补一次 `python -m app.cli reconcile`：镜像上有提交、库里没登记的，它会点名
   再补一次 `python -m app.cli tmp-clean --apply`：回收本次留下的交接文件
8) 运行中遇到这四类情况按 §3.4 自己解决，禁止停下来等我：
   模型/接口限流（rate limit / 429 / 繁忙）→ 等 5 分钟重试，最多 3 次，不许中止整轮；
   任何需要授权的动作（密码弹窗、命令审批、删除文件、sudo、交互 yes/no、提问工具）→ 不许触发，直接 block；
   分析长文本写进 JSON 交给 --analysis-file / --block-file，命令入库后会自己回收那个文件，
   所以你全程不需要、也不许自己去删文件（删除是最常把无人值守冻住的那一下点击）；
   模型报截图尺寸不合规（400 invalid_parameter_error / must be larger than 10）→ 那张图本系统已标
   unreadable，跳过它按文字继续，不许因为一张图中止这条；
   提示「本地文件比仓库新 / 要不要比较」→ 自己在 SSH 侧 diff 取证，噪音就照常做，别问我
本系统不执行 svn、不判定提交细则；没写分析结论不许 commit；
禁止 git svn dcommit / git push / svn ci / 提交或合并 trunk（进正式库只由我做）；
不许 resolve/close 禅道 bug；不许改与当前 bug 无关的文件；不许打开或提交 `.secrets.env`

多语言词条是唯一例外（G12，见 §2.1）：改 .po/.mo 前必须先
`python -m app.cli i18n-up <禅道ID> --files <词条文件>`，改完立刻
`python -m app.cli i18n-commit <禅道ID> --files <同一批文件> --message "<说明>"` 单独提交进 SVN；
不许把词条攒进镜像分支、不许和代码混在一次提交里、除这两个命令外不许碰任何 svn 子命令
```

## 1. 三种执行方式（本工程默认第三种）

| 方式 | 提交动作由谁做 | 本系统命令 | 适用 |
| --- | --- | --- | --- |
| 只管任务 | 执行器按**代码库知识体系**的提交细则做（含 SSH 门禁、同意流程、真实修订号） | `claim` → 提交 → `commit --no-svn --revision <真实号> --analysis-file a.json` | 有自己规则体系的仓库（如带 `CLAUDE.md` + `.trae` 知识根的工作区） |
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
3. **镜像未就绪时不许开工**：判据见下面「就绪」，任一条不过 → 不改码、不转去动正式工作副本，
   直接 `block` 写明「镜像未就绪（缺 ref / 缺 commit / 缺 index）」并继续下一条。
4. **所有 git 动作只在 SSH 侧（Linux）做**，命令形态固定为
   `ssh <SSH主机> "cd <镜像目录> && git -c core.ignorecase=false <子命令>"`；
   Windows 侧那份只是同一份目录的映射视图，**只用来读代码，不许在它里面跑 git**（理由见下面「代价」）。
   2.3 那台的 git 是 1.7.1，不认 `-c` 也不认 `-C` —— 改用 §1.6 写的 `GIT_CONFIG=` + `--git-dir=` 写法。

为什么这么分：SVN 没有本地提交，`svn ci` 一跑就进正式库；git 有。用镜像当草稿区，就把「AI 已改完」
和「已进正式库」这两件事拆成了两个独立动作 —— 前者随时可 `git reset` 回退，后者只在人点头后发生。

代价（必须知道，别当透明）：

- 镜像与正式工作副本是**两份独立副本**，改一份不会同步到另一份；最终回灌靠主人 `git svn dcommit`
  或 `git diff` 出 patch 打到正式副本，本系统不代做。
- 两边共用同一个 `.git` 目录（跨 Samba）时**只能有一个操作方**，git 的文件锁在 SMB 上不可靠；
  所以「同一镜像串行」是硬约束，且划分单位是镜像而不是产品 —— 两个产品绑定到同一个镜像时，
  它们必须共用一条串行队列（见 §5）。Windows 侧与编译服务器侧同样不得同时动镜像。
- 镜像目录**两侧是同一条数据**：Windows 那个映射盘路径与 SSH 侧的 Linux 路径指向同一份 checkout、同一个
  `.git`（映射关系与真实值只存在该库工作区根的 `CLAUDE.local.yaml`（`path_aliases` / `ssh.*`）与本系统的
  `product_repos` 里，本文档不复制一份）。**主机、用户、路径都从 `CLAUDE.local.yaml` 读**，别猜也别写死。
- 就绪 = **三条一起满足**，全部在 SSH 侧验（真实踩过：clone 最后一步在 Windows 侧报 `Filename too long`
  中止，ref 与对象都在，但 `.git/index` 没落盘，执行器一上来 `checkout -b` 就失败）：

  ```
  ssh <SSH主机> "cd <镜像目录> && git -c core.ignorecase=false rev-parse --verify refs/remotes/origin/trunk"
  ssh <SSH主机> "cd <镜像目录> && git -c core.ignorecase=false rev-parse --verify HEAD"
  ssh <SSH主机> "cd <镜像目录> && git -c core.ignorecase=false ls-files --error-unmatch AGENTS.md"
  ```

  ③ 报错或没有输出 = 工作树/index 缺失 → 不许改码，按 §8 那行让主人补 index 后再开工。
- **Windows 侧禁止跑 git**（三条实测，不是理论）：
  1. 那份 `.git/config` 是 Windows 侧 git-svn 写的：`ignorecase = true`、`symlinks = false`；
  2. 本库有 **109 组只差大小写的路径**，Windows 侧两个拼名读到的是同一个文件（实测
     `xt_DSCP.c` 与 `xt_dscp.c` 在 Windows 侧 md5 相同、在 Linux 侧一个是 4028B 一个是 2839B）
     → 在 Windows 侧「改 A 文件」会把改动落到 B 文件上，且另一半在 git 眼里永远是 deleted；
  3. 长路径文件名（如 higress 的 envoy fuzz corpus）Windows 侧创建不了。
  Linux 侧必须带 `-c core.ignorecase=false`，因为上面那份 `ignorecase = true` 会被 Linux 侧 git 照抄。
- SSH 侧的 `~/.gitconfig` 身份是占位值（`user.name=123` / `you@example.com`）→ 提交时必须显式带上
  `-c user.name=<...> -c user.email=<...>`（用主人给的值），否则草稿 commit 的作者是垃圾身份。
- 工作树有一批**结构性噪音**（实测 3.0 补完 index 后 `git status` 共 92 条，占 777,157 个条目的万分之一），
  不是这次 bug 造成的：`--files` 里一律不许带、**禁止 `git add -A` / `add .`**，也别去「修复」它们——
  1. ` D` 36 条：35 条是整棵 `.trae/**`（镜像里它是指向主人知识库根的符号链接，`symlinks=false` 让 git
     认不了它；真去 `checkout` 会**写穿符号链接覆盖知识库根**），1 条是 `openvpn-2.4.8/INSTALL`
     （仓库里是文件、盘上是同名目录，任何平台都只能存在一个）；
  2. ` M` 53 条：其中 13 条纯粹是 Windows 侧检出留下的 CRLF（HEAD 里是 LF），另外 40 条连
     `$Id: <哈希> $` 关键字行也不一样（HEAD 里是展开形态、盘上是 `$Id$`）；看真实差异用
     `--ignore-cr-at-eol`，它只能消掉 CRLF 那一半，剩下 40 条本来就跟你无关，别管；
  3. `??` 3 条：`.trae`、`CLAUDE.md`、`CLAUDE.local.yaml` —— 主人的本地知识框架文件，永远不进版本库。
   `git add <单个文件>` 时 CRLF 会被 git 自动归一回 LF（提交内容不受影响）；但那 40 条 `$Id$` 差异 git
   不会归一 —— **如果你正好要改的文件在这 40 条里**，提交就会顺带把 `$Id: <哈希> $` 改成 `$Id$`，
   这时必须在分析结论里写明「该文件本来就有 $Id$ 形态差异，本次一并带上了」，别让它当成本次的改动量。
- 编译与实测仍只能在 SSH 编译服务器上做（镜像目录在 Windows 本地编译不了）。
- 绑定关系仍要落进本系统，G3 才有判据（真实地址与镜像路径存 `product_repos`，不写进文档）：
  `python -m app.cli bind-repo <产品ID> "<SVN仓库地址>" --name "<产品名>" --working-copy <镜像目录>`

## 1.6 代码库自带的 AI 知识体系（执行器先读它，再动手）

这套体系有**三层**，前两层在镜像目录里直接能读（实测），第三层是仓库正式那份：

| 层 | 位置（相对镜像根） | 是什么 | 什么时候读 |
| --- | --- | --- | --- |
| 常驻注入层 | `CLAUDE.md` | 该库工作区的 AI 首跳规则：硬门禁 + 「触发条件 → 下层路径」一行式指针（实测 65 行、与知识库根那份逐字节一致） | 每条 bug 动手前的第一跳；起手顺序固定 `Skill → playbooks/task-precheck-protocol.md → 触发指针` |
| 按需知识层 | `.trae/`（实测是符号链接 → 主人的知识库根 `<LOCAL_KNOWLEDGE_ROOT>`） | `registry/`（症状入口、能力、API、OEM 矩阵等 23 个）`doc/`（含 `DOC_INDEX.md`、`ARCHITECTURE.md`、`CAPABILITY_MAP.md`）`agents/`（25 个目录级 `AGENT.md`）`playbooks/` `skills/` `scanners/` `staging/` `metrics/` | 按 `CLAUDE.md` 的触发指针**定向 Grep 命中段落再局部 Read**，不整篇通读 |
| 仓库正式层 | `AGENTS.md`、`.trae/…`（进 SVN 的那些） | 库作者维护、随版本走的正式规则与目录级 `AGENT.md` | 常驻层没覆盖该域时；两者口径以代码库为准 |

- 路径真实源只有一个：镜像根 `CLAUDE.local.yaml` 的 `path_aliases`（`LOCAL_KNOWLEDGE_ROOT` /
  `LOCAL_RUNTIME_ROOT` / `LOCAL_WORKSPACE_ROOT`）与 `ssh.*`。本系统不复制这些值，也不写进本文档。
- 生成态读数（`staging/`、`metrics/`、`doc/DOC_INDEX.md`）有 `generated_at` + `max_age_days`；
  超过时效一律当「未知」，不许把过期的空清单读成「没有问题」（实测 `DOC_INDEX.md` 30 天时效）。
- 该产品**没有**对应文件时就跳过（实测两条线现在都有 `doc/ARCHITECTURE.md` 与 `doc/CAPABILITY_MAP.md`，
  但覆盖面不同：3.0 知识根 `agents/` 下 25 个目录级 `AGENT.md`、2.3 是 11 个；`registry/` 各 23 个文件），
  别为不存在的入口反复找；目录级 `AGENT.md` 不存在时也直接按下钻规则新建（见下面「知识回写」），不算越界。
- **两条产品线的 SSH 侧不是一套环境**（实测）：3.0 那台是 git 2.33.0，支持 `git -c` / `git -C`；
  2.3 那台是 **git 1.7.1（RHEL6）—— `git -c` 与 `git -C` 都不认**（报 `Unknown option: -c / -C`），
  所以在那台上要写成 `cd <镜像> && git --git-dir=.git <子命令>`，覆盖配置改用环境变量
  `GIT_CONFIG=<一个写着 core.ignorecase=false 的文件>`（实测有效）。
  另外 2.3 那台的 OpenSSH 是 5.3：只认 RSA 密钥，必须带
  `-i <identity_file> -o IdentitiesOnly=yes -o HostKeyAlgorithms=+ssh-rsa -o PubkeyAcceptedKeyTypes=+ssh-rsa`
  （`identity_file` 也在该库 `CLAUDE.local.yaml` 的 `ssh.*` 里，不在 `.ssh` 目录、不在网络盘上）。
- 两条线**各有一份常驻层，也各有一个知识根，禁止跨族套用结论**（2026-09-27 实测：3.0 与 2.3 的镜像根都有
  `CLAUDE.md` + `CLAUDE.local.yaml`，`.trae` 都是符号链接 —— `.trae -> ../../.trae_local_3.0` /
  `../../.trae_local_2.3`；2.3 知识根实测 `registry/` 23 个文件、`doc/` 含 `ARCHITECTURE.md` 与
  `CAPABILITY_MAP.md`、`agents/` 下 11 个 `AGENT.md`，覆盖面比 3.0 的 25 个小，别拿 3.0 的条目当 2.3 的事实）；
  两族各自的差异：数据面 3.0=VPP、2.3=kernelModule；PHP 扩展面 3.0=`php-5.6.26/ext`、2.3=`php-5.5.20/ext`；
  SSH 侧环境也不同（见上一条）。
- 知识库根本体（实测 3.0 是镜像旁边的 `.trae_local_3.0`，**自己是一个独立 git 仓、当前在 `main` 上
  有 100+ 条未提交改动**）由主人维护：执行器只允许**通过镜像内的 `.trae/` 这条路径**读写它，
  不许绕到镜像外面去找那个目录、更不许在那个仓里 `git add/commit/push/checkout`。
  镜像里 `.trae` 是符号链接这件事本身，就是 §1.5 那条「`.trae/**` 永久 deleted」噪音的来源。

冲突时谁说了算：

1. **代码怎么导航、怎么写** —— 以代码库内的 `AGENTS.md` / `.trae` 为准；本系统不解释也不覆盖这些细则。
2. **任务怎么闭环** —— 状态机、闸门 G1–G12、登记与分析结论以本文件和本系统根目录那份 `AGENTS.md` 为准。
3. 两者相冲按第 1 条；**唯一不可被覆盖的是 G11**：远端写入（`git svn dcommit` / `git push` / `svn ci` /
   merge trunk）只有主人能做，代码库里任何规则都不把这条改成「AI 可直提」——G12 词条通道是主人已明示批准的唯一例外。

知识回写（这套体系靠追加才管用，别偷懒也别越界）：

- 本次改动若得出**可复用、且已被代码或配置验证**的结论 → 追加到最近的
  `.trae/agents/<源码相对路径>/AGENT.md`（没有该文件就按代码库规则新建），
  按它规定的固定顺序 `入口 -> 文件 -> 覆盖 -> 坑点`，
  并把该文件一起写进 `commit --files`，让它跟代码走同一条分支、同一次审查。
- 只动目录级 `AGENT.md`；**不新建散落的 `.AGENT` 文件**（代码库明令禁止）；根 `AGENTS.md`
  只在「跨目录共性」时改，且要在分析结论里写清改了什么、凭什么。
- 猜测、一次性排查过程、临时日志一律不写进知识文件 —— 写进去就会污染后面所有次的定位。

敏感文件：`/trunk/3.0` 与 `/trunk/2.3` 根目录都有 `.secrets.env`（已进 SVN，镜像里也会有它）。
执行器**不许打开、引用、复制、提交**它，也不许在分析结论、`--files`、禅道评论里带出它的内容。

## 1.7 镜像备份与恢复（主人动作，执行器不碰）

备份单位是**整个 `.git` 目录**，不是工作树：`refs/`（基线与本地草稿分支）、`objects/`（内容）、
`config`（含 `svn-remote` 映射）和 `.git/svn/…/.rev_map`（SVN 修订号 ↔ git commit 的对照表）全在里面；
工作树随时可以由 `read-tree` + `checkout` 重建，不必进包（3.0 工作树 23G，`.git` 才 6.9G）。

```
# 在 SSH 侧、镜像的上一级目录执行（3.0 用 pigz -p 8，2.3 那台只有 gzip）
tar -cf - <镜像名>/.git | pigz -p 8 > ~/hy-10G-2/mirror-backup/<镜像名>-dotgit-<短哈希>-<日期>.tar.gz
tar -tzf <那个包> | wc -l          # 能列出来就说明 gzip 完整
sha256sum <那个包> > <那个包>.sha256
git --git-dir=<镜像名>/.git fsck   # dangling blob 是镜像的正常现象，不是损坏
```

恢复：解开 `.git` 放回镜像目录 → `git read-tree HEAD` → 按 §8 那行补 `ls-files -d` 列出的缺失文件
（清单排掉 `.trae/`）→ 三条判据验收。**只解 `.git` 时工作树是空的，别直接开始改码。**

包放在各台服务器自己的 `~/hy-10G-2/mirror-backup/`（3.0 的包在 3.0 那台，2.3 的包在 2.3 那台），
不跨机拷贝；换盘或重装前先补一份新的，包名带短哈希，能看出它是哪条基线的备份。

## 1.8 镜像操作收进本系统命令（执行器一律走这四条，别再自己拼 ssh）

以前子任务要 `ssh … "cd 镜像 && git checkout -b …"`、`ssh … "git add && git commit"`、
`ssh … "git rev-parse …"` 各来一遍。每一条都是 IDE 眼里的陌生命令，都要点一次授权；
无人值守时那一下点击等于没人点，整轮就冻在那条 bug 上。现在镜像的整个草稿生命周期都有
本系统命令，执行器只需要 `python -m app.cli …` 一种形态：

| 命令 | 做什么 | 护栏 |
| --- | --- | --- |
| `mirror <ID>` | 只读探测：草稿分支在不在、tip、领先几笔、改了哪些文件、镜像当前停在哪个分支、工作区脏不脏、就绪三判据（`mirror_ready`） | 纯只读，不 fetch 不 checkout |
| `checkout <ID> [--base …]` | 建/切这条 bug 的 `bugfix/zentao-<ID>`，默认基线 `refs/remotes/origin/trunk`，成功即置 `fixing` 并写回 branch | 镜像缺 trunk ref 或索引没落盘 → 拒绝；工作区有未提交改动 → 拒绝（不 stash 不切走，那是别人的会话） |
| `draft <ID> --message "fix #<ID> …" --files a,b` | 只 `git add` 列出的文件并提交，返回短哈希 | 说明必须以 `fix #<禅道ID>` 开头（G8）；`--files` 必填、必须是镜像内相对路径、不许 `..`/绝对路径/`.git`；分支不存在就报错让你先 `checkout`；HEAD 不在这条分支上就拒绝；暂存为空 → 不造空提交 |
| `reconcile [ID] [--apply]` | 对账：镜像上有草稿提交、库里却没登记（执行器跑完没收口 / 崩在收口前） | 默认只报告；`--apply` 才登记 `git:<哈希>` 并转 `await_review`，分析里写明「结论来自 git 提交信息，未验证」 |
| `tmp-clean [--apply]` | 回收跑完留在项目根的交接文件（`a_<ID>.json`、`blk_<ID>.json`、`py_<ID>.py`、`_tmp_*.py`） | 只认上面这几种命名；分析**没**入库的 `a_<ID>.json` 一律留着只报告；默认 dry-run，`--apply` 才删；`.env`、库文件、源码都不在候选里 |

`draft` 之后仍然要 `commit --no-svn --revision git:<哈希> --analysis-file a.json` 收口——
它只替你落 git 提交，不替你写分析、不替你过闸门。三条通道（推 SVN / 拒绝并回滚 / 重开）照旧是主人的。

**还有一条最容易踩的：执行器不许自己去删文件。** 现场核对下来，无人值守弹授权的动作
绝大多数是「清理用完的 `a_<ID>.json`」这类删除，而删除要人点确认，一点确认整轮就冻在原地。
所以规则改成：长文本写进 JSON 交给 `--analysis-file` / `--block-file`，**命令写库成功之后由本系统
把它读过的文件回收掉**（返回里的 `handoff_removed` 就是清单），想留着自查就加 `--keep-handoff`；
一批跑完统一 `tmp-clean --apply` 清场。这样整条链路上没有任何一步需要「删除」这个动作。

## 2. 提交闸门（下达给执行器的「什么情况下可以提交」）

闸门全过才允许提交；**任一条不过 → 走 `block` 回填需方案，绝不「先提交再说」**。

| # | 条件 | 本系统提供的判据 |
| --- | --- | --- |
| G1 | 已看过禅道的完整描述、**截图**、备注/操作记录 | `status` 的 `steps` / `images[].local_path` / `comments`；缺则先 `detail <ID>`；`images_skipped` 里的图模型读不了（1x1 之类），跳过它并在 `unverified` 注明即视为已尽看图义务（§3.4 四） |
| G2 | 根因明确、修改点唯一（或已按主人 `owner_reply` 执行） | `status` 的 `owner_reply` 与历史提交 |
| G3 | 只改与本 bug 直接相关的文件，且都在**该 bug 所属产品的工作副本**内（git 方式下 = `repos` 给出的镜像目录，不是正式工作副本） | `status` 的 `product_id` + `repos` 的产品→仓库映射（含 `working_copy`） |
| G4 | 该产品的编译/测试实际通过（连续 2 次不过即视为不过） | `repos` 的 `build_command` / `test_command`（未绑定则按代码库自身构建规则） |
| G5 | 状态矩阵自审完成：主路径 / fallback 与异常路径 / 资源释放收尾 / 日志计数 / 同构镜像分支 | 无（由代码库知识体系的 precheck 规程负责，本系统只要求你显式声明已核对） |
| G6 | 触及页面文案就同步 `i18n.po/.mo`；页面源码不直接写中文、注释全英文；不引入超出语言基线的语法 | 无（同上，属代码库规则）；词条文件本身走 G12，不进镜像草稿 |
| G7 | 提交目标是 `bugfix/zentao-<禅道ID>` 分支，**不是 trunk**（git 方式下=本地分支，且只是一次 `git commit`） | `status` 的 `branch`；本系统侧还有 `SVN_ALLOW_TRUNK_WRITE=false` 兜底 |
| G8 | commit message = `fix #<禅道ID> <一句话根因>`，不含账号密码等敏感串 | `commit --message` 拼接规则 |
| G9 | 未验证的路径必须标「未验证」，不得按「已完成 / 可直接合入」收口 | 登记内容进 `fix_summary` / `verify_steps` 供你审查 |
| G10 | 提交所需的授权已按代码库规程取得（如 SSH 写操作需显式同意） | 无（属代码库规则；本系统只要求拿到真实修订号再登记） |
| G11 | **远端写入留给主人**：执行器只做本地动作（`git commit` / 改工作副本不 `ci`），绝不 `git svn dcommit`、`git push`、`svn ci`、merge 到 trunk。主人那一侧走审查详情弹窗的「正式推入 SVN」（§3.3，`app/svn_promote.py`），执行器不许调用该接口。**唯一例外是 G12 的多语言通道**，其余任何文件都不适用 | 无（本系统不检测，靠禁止事项 + 登记形态 `git:<哈希>` 让审查页一眼可辨） |
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

`a.json` 由命令读入并写库，**成功后本系统把它回收掉**（返回里的 `handoff_removed`），
执行器不需要、也不许再去删它 —— 删除动作会弹授权、把无人值守冻在原地（§1.8、§3.4 二）；
想留着自查就加 `--keep-handoff`，一批跑完统一 `tmp-clean --apply` 清场。

`a.json` 的字段（除 gates 外都建议写满，写不出来的就是没分析到位）：

```json
{
  "symptom":     "现象：什么入口、什么版本、用户看到什么",
  "root_cause":  "根因：一句话说清为什么坏",
  "evidence":    "定位依据：真实读过的文件:行 / 函数 / 日志 / 复现输出（含 §1.6 里按顺序读过的知识体系入口，例如 .trae/agents/cgi/AGENT.md；禁止写没读过的路径）",
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

一条 bug 可以有多份分析（打回重做、卡点后再修都会追加），审查页显示最新一份**非人工**分析；
`kind=manual` 的人工留痕（例如推入 SVN 失败）另列在「人工留痕」里，不会盖掉执行器的根因分析。

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
  f. 撞上 §3.4 那三类情况自己处理：限流就等 5 分钟重试（最多 3 次）后继续，
     需要授权的动作一律不触发、直接 block 留痕，「本地文件比仓库新」自己取证判定；
     任何一种都不许把循环停下来等我
收工：python -m app.cli report，给我三份清单：
  - 已提交待审查（我要去 /review）
  - 需我给方案（我要去 /need 答复）
  - 闸门未过/未提交及原因
期间不要复述规则、不要问我是否继续、不要碰 trunk、不要推远端（dcommit/push/svn ci）、不要替代码库判定提交细则。
不要在 Windows 侧跑任何 git（那份 .git 与 Linux 侧是同一个，Windows 的 ignorecase 会把改动落到大小写对偶文件上）；
唯一例外是词条：改了 .po/.mo 就按 §2.1 用 i18n-up + i18n-commit 单独提交，其余文件一律不碰 svn。
```

### 3.2 子任务指令（每条 bug 一份，无上下文也能独立执行）

```
你只处理禅道 bug #<禅道ID>，做完立即结束，禁止顺带处理其他 bug。
任务管理命令的工作目录：<项目目录>
落码位置：镜像仓库 <镜像路径>（见 §1.5）；正式 SVN 工作副本只读，禁止在里面改码
        —— 唯一例外：多语言词条文件按 G12 走 §2.1 的通道，改前先 up、改完立刻单独提交
   <SSH主机> / <SSH用户> / Linux 侧镜像路径 = 该库镜像根 `CLAUDE.local.yaml` 里的 `ssh.*` 与
   `path_aliases.LOCAL_WORKSPACE_ROOT`（同一份目录，Windows 侧只读代码，git 一律走 SSH）

0) 先验镜像就绪（§1.8，别再自己拼 ssh）：python -m app.cli mirror <禅道ID>
   看 `mirror_ready`（= trunk ref + HEAD + 索引里有 AGENTS.md 三条判据）与 `worktree_clean`
   → ready=false = clone 没跑完或最后检出没落盘 → 直接跳到第 7 步 block，
   原因写「镜像未就绪（缺 ref / 缺 index）」，不许自己重跑 clone/fetch/read-tree 去补
1) python -m app.cli status <禅道ID>
   读 product_id / product_name / steps / comments(备注) / attachments[].local_path / 历史提交 / owner_reply
   → 有截图必须用 Read 打开 local_path 真正看图，只看文字就动手是最常见的误判来源
   → `images_skipped` 里的图（1x1 之类的占位图）模型读不了，硬喂会换来一个 400 把整条任务打断：
     跳过它，按文字描述与代码取证继续，并在分析的 unverified 里写明「某张截图不可读」
   → 若 attachments 为空且没抓过详情：python -m app.cli detail <禅道ID>
2) python -m app.cli claim <禅道ID>        # 占住任务，置 fixing，避免被别的子任务重复领
3) 先读这条 bug 所在代码库自带的 AI 知识体系（§1.6），再动手定位：
   镜像根 `CLAUDE.md`（常驻层：硬门禁 + 触发指针）→ 命中哪条就读 `<LOCAL_KNOWLEDGE_ROOT>` 下哪个
   `registry/` / `doc/` / `agents/` / `playbooks/`（实测镜像里 `.trae` 已是指向知识根的符号链接，
   从镜像内走这条路即可，别绕到镜像外面）→ 常驻层没覆盖该域时再读仓库正式的 `AGENTS.md`
   → 定下落点后读 `.trae/agents/<源码相对路径>/AGENT.md`，流程按 `.trae/skills/` 走，
   不许绕过知识体系全库散搜；跨文件检索与遍历也按该库硬门禁走 SSH（Windows 映射盘上本机 grep/find 会卡死）
   然后为这条 bug 开/切本地分支（§1.8，它会顺带拒绝脏工作区，不替你 stash）：
   python -m app.cli checkout <禅道ID>
   只改与这条 bug 直接相关的文件，且路径必须属于这条 bug 的产品仓库（闸门 G3）
   改文件前先确认大小写拼名 —— 本库有 109 组只差大小写的同名对偶，Windows 侧两个拼名读到的是
   同一个文件，只有 Linux 侧才是真身份：按该库规程在 SSH 侧跑
   `git -c core.ignorecase=false ls-files <那个路径>`（这一条是定位取证，不属于 §1.8 的草稿动作）
4) 编译 / 测试：用该库规定的构建方式（本系统 `repos` 里的 build_command / test_command 可作参考）
   本工程只能在 SSH 编译服务器上实测；连续 2 次不过 → 直接走第 7 步 block，写清失败现象，不要硬试
5) 多语言词条（改了 .po/.mo 才做，G12；镜像分支里不留词条文件）
   python -m app.cli i18n-up <禅道ID> --files <词条文件>          # 改之前先 up
   → conflict=true → 停手，走第 7 步 block，原因写「多语言冲突待人工处理」，不许自己硬解 .mo
   词条的扫描与回填按该库自己的规程做（根 `AGENTS.md`「全局多语言」一节写明的脚本与
   PO/MO 固定路径，例：`ai_i18n.py --no-ai --scan …` 生成 tri.json → 补三语 → `--tri-only --yes` 回填），
   本系统只负责改前 up 与改完单独提交，不要手抄着改 .po/.mo
   在正式 SVN 工作副本里只改这些词条文件，改完立即单独提交（不许攒、不许跟代码混一次提交）：
   python -m app.cli i18n-commit <禅道ID> --files <同一批文件> --message "<一句话说明>"
   → 输出里的 r<号> 就是词条的真实修订号，稍后写进分析结论的 change_desc / evidence
6) 提交判定（§2 的 G1–G12）
   - 全过：先做知识回写，再落草稿提交（§1.8，短哈希在它的返回里，不用再自己 rev-parse）
     本次若得出可复用、且已被代码或配置验证的结论 → 追加进
     `.trae/agents/<改动目录>/AGENT.md`（§1.6 的知识回写）；它是知识根那个独立仓的内容，
     **只追加文件、不 add 不 commit**，也不要写进本条 bug 的 `--files`，改为在 analysis 的
     evidence 里点名「回写了哪个知识文件、写了什么」，由主人自己收口
     python -m app.cli draft <禅道ID> --message "fix #<禅道ID> <一句话根因>" --files a.c,b.c
     只列你改过的那几个文件；命令本身不做整仓 add —— §1.5 那批结构性噪音
     （整棵 `.trae/**`、`openvpn-2.4.8/INSTALL`、Windows 检出留下的 CRLF `M`）
     一旦被 `add -A` 卷进来就是白送的一堆误报，所以它按清单逐个 add
   - 禁止：git svn dcommit / git push / svn ci（G11，进正式库只由主人做；词条已由 i18n-commit 单独提过）
   - 任一条不过或需要业务决策：不提交，仍要写分析，跳到第 7 步的 block 分支
7) 把分析结论写成 JSON 文件（字段规范见 §2.2，gates 必须逐条给结论，含 G11/G12），然后回本系统登记
   已本地提交（把 git 短哈希当修订号登记，前缀 git:）：
     python -m app.cli commit <禅道ID> --message "<一句话根因>" --files a.c,b.c --summary "<改了什么>" --verify "<人工怎么验>" --no-svn --revision git:<短哈希> --analysis-file a.json
   改好了但不便开分支/哈希还没定（占位待你处理）：
     python -m app.cli commit <禅道ID> ... --no-svn --revision PENDING --analysis-file a.json --extra "待主人按仓库提交细则提交"
   卡住需要方案（question/options/advice 之外照样给分析）：
     python -m app.cli block <禅道ID> --question "<卡在哪一步>" --options "方案A：…；方案B：…" --advice "<建议及理由>" --analysis-file a.json
     # 三段中文太长不好过 shell 时，写成 blk.json（question / options / advice，可再带 analysis_file）
     # 用 --block-file blk.json 一次交进去，命令同样在入库后把它们回收掉
8) 回报字段：zentao_id / product / 分支名 / 改了哪些文件 / git 短哈希 / 词条修订号 r<号>（若有）/
   闸门逐条结论 / analysis_id / action(commit|pending-commit|block) / revision 或 need_id
禁止：没写分析就 commit（REQUIRE_ANALYSIS 会直接拒绝）；git svn dcommit / git push / svn ci / 提交或合并 trunk；
在正式工作副本里改码（词条文件除外，且必须走 i18n-up / i18n-commit 两条命令）；
把词条文件留在镜像分支里不提交、或用 i18n-commit 夹带任何非词条文件；
resolve/close 禅道 bug；改无关文件；把 .env 内容写进任何输出；问我是否继续；
触发任何要人授权的动作（密码弹窗、命令审批、sudo、交互 yes/no、提问工具）—— 需要授权就 block，别等；
看镜像 / 开分支 / 落草稿自己拼 `ssh … git …`（一律用 §1.8 的 mirror / checkout / draft，那才是免审批的一条命令）；
撞上模型/接口限流就按 §3.4 等 5 分钟重试（最多 3 次），不许把这条任务中止或跳过；
把 `images_skipped` 里的图喂给模型，或因为一张读不了的图（400 / must be larger than 10）中止这条任务；
看到「本地文件比仓库新 / 要不要比较」按 §3.4 自己取证判定，不许停下来问我
```

### 3.3 收工后你的三步（审 + 回灌正式库 + 回填）

```
# 1) 看这轮结果与待办
python -m app.cli report

# 2) 逐条读 AI 的分析结论 + files + summary + verify，并审它本地的提交
#    （审改动也在 SSH 侧看：Windows 侧的 git 会把大小写对偶文件混成一个，diff 不可信）
python -m app.cli status <禅道ID>       # 输出里的 analysis 就是 AI 写的分析（gates_parsed 是闸门逐条）
ssh <SSH用户>@<SSH主机> "cd <镜像目录> && git -c core.ignorecase=false show <短哈希>"
ssh <SSH用户>@<SSH主机> "cd <镜像目录> && git -c core.ignorecase=false diff --ignore-cr-at-eol refs/remotes/origin/trunk..<分支名>"

# 3) 你判定可以进正式库：在审查页打开这条的详情弹窗，写提交说明后点「正式推入 SVN」
#    （先点「预检（不写入）」看一眼清单与 trunk 漂移更稳）。这一步执行器绝不代做。
#    它做的事全在 SSH 侧：读镜像里的草稿对象 -> 落到 <项目>/svn_promote/<镜像名> 这个
#    --depth empty 稀疏工作副本 -> 按文件 svn ci 进 trunk（提交说明按 UTF-8 交给 svn）。
#    只有真进了库才登记真实 r 号并置 merged；推送失败只写一条人工留痕（`kind=manual`），
#    状态仍是 await_review、`git:<哈希>` 草稿记录不动 —— 推不入库不等于改得不对，
#    要不要打回重做由你判定。被护栏拦下（trunk 已有他人改动）也走这条留痕路径。
#    被护栏拦下时预检会自己说清三件事：谁改的（该文件 trunk 上最近一笔 r 号与说明）、
#    哪些文件与本草稿改在不同行区间（`可无损自动对齐`）、哪些真的撞上同一行（`必须人工对齐`）。
#    不重叠的那类可以勾「自动按 trunk 对齐」（表单 `align=1`）后重推：脚本用
#    `git merge-file` 三方合并，以 trunk 现内容为底再套本草稿的改动，他人改动一行不少。
#    对齐了哪些文件、跳过了哪些已入库文件，只写进审查留痕与禅道评论，**不写进 svn 的提交说明**
#    （正式 log 里只该有这次改了什么，不该有本系统的过程日志）。重叠/增删仍整笔拒绝，trunk 与草稿都不动。
#    这个勾只有主人能点，执行器不许调用本接口（G11），也不许改脚本放宽护栏。
#    两台编译服务器都没有 git-svn（实测 `git: 'svn' is not a git command`），所以 dcommit 不是选项；
#    镜像 `refs/remotes/origin/trunk` 落后于真实 trunk 是这类拦截的常见根因，能 fetch 时先 fetch 再推更干净。
python -m app.cli status <禅道ID>       # 推完再看一眼：r<号> 与 git:<哈希> 会同时挂在这条名下
```

推完发现修得不完全，就在详情弹窗点「重开补修（入队列）」（`/bug/<id>/reopen`，必填哪里不完全）：
这条回到 `rejected` 重新入队，`queue_kind=reopened`，上一轮的结论/改动文件/r 号作为 `prior_fix`
一起下发；登记的 r 号与草稿分支都不删，**trunk 不回退**（要回退是你自己的 svn 动作）。
下一轮推送会自动以「上一轮推入的那个草稿提交」为增量基准（推送成功时 `svn_revisions.git_commit`
记下了它），只提交新改动；trunk 上已经是草稿内容的文件直接跳过（跳过清单进审查留痕与禅道评论，
不进 svn 提交说明），整批都一致时整笔拒——不会造空修订。拿不到那个提交（历史条目手工合入的）时会退回
`origin/trunk` 口径并靠「已落地」识别跳过，必要时勾「自动按 trunk 对齐」。

```
python -m app.cli tasks --limit 5       # 队列里 queue_kind=reopened 的行会带 prior_fix
```

想手工做也可以，顺序与按钮一致（`--non-interactive` 保证不会弹密码）：

```
ssh <SSH用户>@<SSH主机> "cd <镜像目录> && git diff --name-status <基线>..<提交>"
ssh <SSH用户>@<SSH主机> "cd <稀疏副本> && svn update <文件> && ..."   # 逐个落内容
ssh <SSH用户>@<SSH主机> "cd <稀疏副本> && svn ci --non-interactive -m 'fix #<禅道ID> <一句话根因>' <文件...>"
python -m app.cli commit <禅道ID> --message "<一句话根因>" --files a.c,b.c --no-svn --revision r<真实号>
```

回填是**追加**：`git:<哈希>`（本地草稿）与 `r<号>`（正式库）会同时挂在这条 bug 名下，
`status` 与审查页能看到它从草稿到落库的完整轨迹。
登记成 `PENDING` 的条目同理，只是还没有可信的本地哈希可审，得先看 `files` 与工作副本状态。

### 3.4 运行中不许卡住的四类情况（限流 / 要授权 / 文件比仓库新 / 截图读不了）

这四类以前都会把整轮停下等人，现在一律**由执行器自己解决**，规则如下。

**一、限流与暂时失败：等一会儿重试，不许中止整轮**

- 命中特征：模型或接口返回 `rate limit` / `429` / `overloaded` / `quota` / `503` / `502` /
  「稍后再试」「服务繁忙」，或连接超时。
- 处置：**等 5 分钟**（`RETRY_WAIT`，默认 300 秒）再重试同一步，总共试 `RETRY_MAX` 次（默认 3）。
  本系统的禅道读取已经内置了这个等待重试（`app/net_retry.py`，`Retry-After` 会被尊重，
  等待过程打到 stderr，日志里看得见「是在等」而不是「挂了」）；模型侧的限流只能由你自己在循环里等。
- 重试期间**不许**改这条 bug 的状态、不许 block、不许跳过它去做别的、更不许退出循环。
- 只有 `RETRY_MAX` 次用完还是失败，才 `block` 写明「上游限流未恢复（已等 X 分钟重试 N 次）」，
  然后**立刻继续下一条**；这条留在 `need_solution` 里等主人。
- 写类动作不自动重试（评论推送、`i18n-commit`）：一次没成就算了，重复提交比失败更难收拾。
- 真的被限流打到中断也不用担心卡死：那几条已 `claim` 成 `fixing` 的 bug 超过
  `STALE_CLAIM_MINUTES`（默认 40 分钟）会自己回到队列（`queue_kind=stale`），下一轮重跑就行，
  **不需要你或我去手工改状态**。
- 反过来也要守住这条线：一条 bug 的处理时间**不该接近**这个阈值。眼看要超就先收尾
  （闸门过了就 `commit`、没过就 `block`），别让它被回收后两条子任务同时改同一条。

**二、需要授权的操作：一条都不许出现**

无人值守期间，任何要人点一下才能过的动作都算违规，因为它会把循环冻在那里：

- 禁止清单：密码/口令弹窗（含 SSH 密码登录 —— 只准用免密 key，2.3 那台的 RSA 参数见
  该库 `CLAUDE.local.yaml`）、IDE 的命令审批/沙箱越权提示、`sudo`、要求输入 yes/no 的
  `svn`/`git` 交互（本系统的 svn 一律 `--non-interactive`）、让你「确认是否继续」的提问，
  以及 G11 禁的 `git svn dcommit` / `git push` / `svn ci`。
- **实测最常见的触发点是「删除文件」**：清理用完的 `a_<ID>.json`、临时脚本、`__pycache__`
  都会弹确认。所以这些删除根本不该由你做 —— 见上面 §1.8 的交接文件回收与 `tmp-clean`。
  同理别顺手 `rm`、别 `del`、别用删除工具去"收拾现场"；要留就 `--keep-handoff`，要清就一条命令。
- 也不许调用 `AskUserQuestion` 之类的提问工具：无人值守没有人在屏幕前。
- 某一步确实非授权不可（例如必须主人本机才能做）→ **不要等批准**，直接 `block`
  写清「需要主人做什么授权、为什么、影响面、怎么回滚」，然后继续下一条。
- **少触发审批的第一办法：别自己造命令。** 看镜像、开分支、落草稿、对账这四件事
  都已经有 `python -m app.cli mirror / checkout / draft / reconcile`（§1.8），
  它们和 `status` / `claim` / `commit` 同一种形态，不需要为它们点授权；
  裸 `ssh … git …` 每出现一次就多一次被冻住的机会。
- 结论：**授权需求一律转成 `block` 留痕，不转成等待。**

**三、「本地文件比仓库新 / 要不要比较」：自己判定，不许问**

镜像工作树是 Windows 与 Linux 两侧共用同一份目录，盘上内容与 index 有历史差异是常态
（§1.5 的 92 条噪音就是），所以「本地文件比仓库新，是否比较」这类提示**不该来问你**：

1. 自己先在 SSH 侧取证：`git -c core.ignorecase=false status --porcelain -- <那个文件>`，
   再 `git -c core.ignorecase=false diff --ignore-cr-at-eol -- <那个文件>`；
2. 差异只剩 CRLF / `$Id$` 形态 / 大小写对偶 / `.trae/**` → 属已知噪音，**照常改照常提交**，
   不提问、不 `block`、不许 `checkout` 复原、不许写进 `--files`；
3. 差异是真实内容、且不是你这次写的 → 不许覆盖、不许 stash、不许 revert；
   `block` 写明「<文件> 有一份不是我改的改动：<一行摘要>，请确认保留还是丢弃」，继续下一条；
4. 差异是你自己这次写的但分支不对（别的禅道 ID 混进来）→ 按 §8「分支基线莫名变化」那行处理。

**四、模型读不了那张截图：跳过它，不许中止这条**

禅道里有的是 1x1 的占位图或损坏图，视觉接口对它们的回答不是警告而是硬失败：

```
{"error":{"code":"invalid_parameter_error",
 "message":"The image length and width do not meet the model restrictions.
            [height:1 or width:1 must be larger than 10]"}} (HTTP Status: 400)
```

- 本系统在**下载附件时就量过尺寸**（`app/imgsize.py`）：短边不足 10px 或读不出尺寸的，
  该条会带 `unreadable` 原因，并从 `images` 挪进 `images_skipped`。
  所以 `status` / 审查页给你的 `images` 一律是「能看的图」，`images_skipped` 是「已经替你判定不能看的」。
- 库里已有的历史附件用 `python -m app.cli img-gate` 离线重量一遍（不连禅道，只读文件头）。
- 处置：`images_skipped` 里的图**不要 Read**；按描述文字 + 代码取证继续，
  在分析的 `unverified` 里写「某张截图 1x1 不可读，未看图」。G1 的「必须看图」
  对这种图自动降级，**不算你没看图**。
- 万一还是撞上了这个 400（新图、或别的图触发的）：把图从这一步拿掉重试一次文字路径，
  仍然不许中止整条任务，更不许中止整轮；实在缺图不可判断 → `block` 写明缺哪张图。

## 4. 为什么它能「自动下一个」而不会走偏

1. **状态在库里，不在对话里**：进度是 `bugs.status`；关窗、断线、换会话后重贴 §3.1 就是断点续跑。
2. **卡点不阻塞**：`block` 把 bug 踢到 `need_solution`，自动掉出队列；你答复后它以最高优先级回来。
3. **提交不了也不阻塞**：闸门未过 → `--revision PENDING` 登记或直接 `block`，循环继续，缺口在 `/review`、`/need` 看得见。
4. **分析结论是硬交付**：`REQUIRE_ANALYSIS=true` 时没有分析的 `commit` 会被系统拒绝，
   执行器只能「先写分析再收工」，你审查时才有东西可读——这条不靠自觉，靠命令返回码。
5. **优先级不用你指定**：`tasks` 固定排序 = 已答复 > 已打回 > 待处理，再 `pri` 升序 / `severity` 降序。
6. **批次可调**：`.env` 的 `AI_BATCH_SIZE`（默认 5）是约定值；临时改批量直接 `tasks --limit N`。
7. **退出条件由数据决定**：`tasks --limit 1` 返回空 = 没有可处理项，此时才允许停下汇报。
8. **跑挂了不会把任务卡死**：被限流打断、进程被杀、会话断掉时，那条 bug 会停在 `fixing`；
   超过 `STALE_CLAIM_MINUTES`（默认 40 分钟）它自动回到队列并标 `queue_kind=stale`，
   下一轮重跑就行，不用你手工改状态。加上 §3.4 的等待重试，一轮里没有任何东西需要人来救。

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

现在这条已经收成一句更简单的规定：**Windows 侧完全不跑 git**（§1.5），所以「两侧同时动镜像」
只剩「主人在 Windows 侧手抖敲了 git」这一种来源；执行器侧永远只有 SSH 一个写入方。

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
命令 python -m app.cli reconcile         对账：镜像上有草稿提交、库里没登记的（执行器没收口）——
                                         看一眼，确认无误再 `reconcile <ID> --apply` 认领成待审查
命令 python -m app.cli img-gate --check  有哪些截图模型读不了（1x1 / 损坏），量的是本地文件不连禅道
命令 python -m app.cli tmp-clean         列出本次留下的交接文件（a_<ID>.json 等）；--apply 才回收，
                                         分析没入库的那份一律留着只报告
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
| 另一条产品线（2.3 那份镜像）缺 index | 同上，但它挂在**另一台服务器**的共享目录上，那台目前不认这里的 key（`Permission denied (publickey)`）→ 主人要么在那台上用密码登录执行同一套 `read-tree` + 补文件，要么把执行侧的公钥装上去；在此之前该产品的 bug 全部 `block`「镜像未就绪（缺 index）」，不许转去动正式副本 |
| clone 末尾报 `Filename too long` + `read-tree -m -u -v HEAD HEAD: command returned error: 128` | **不用重拉**：对象、基线 commit、`.git/svn/…/.rev_map` 都已写好，只是 `.git/index` 没落盘。**必须在 SSH（Linux）侧修** —— 本库有 109 组只差大小写的路径，Windows 侧的 `ignorecase` 修不出完整工作树。主人执行：`ssh <SSH用户>@<SSH主机> "cd <镜像目录> && git read-tree HEAD"` 建 index，再 `git -c core.ignorecase=false ls-files -z -d` 列出盘上缺的文件、`grep -zv '^\.trae/'` 滤掉符号链接那棵，`git -c core.ignorecase=false checkout --pathspec-from-file=<清单> --pathspec-file-nul` 补齐（实测 3.0 缺 129 条、补回 128 条，剩下 `.trae/doc/vpp-23.02/VPP_CORE_SHOW_LOG_AND_ERROR.md` 属符号链接噪音）。清单务必排掉 `.trae/` —— 那会写穿符号链接覆盖主人知识库。执行器遇到这种情况一律 `block` 写「镜像缺 index」，别自己动 |
| 本地 git 分支攒了一堆 commit 没进正式库 | 正常状态（G11 设计如此）。由主人在 SSH 侧 `git -c core.ignorecase=false diff refs/remotes/origin/trunk..<分支>` 审，通过后自行 `git svn dcommit` 或出 patch 打到正式副本，再回填真实修订号 |
| SSH 侧 `git status` 一跑冒出几十条 `M`，diff 是整文件重写 | Windows 侧检出留下的 CRLF（HEAD 里是 LF），本库实测 53 条（php / openvpn / products 那几棵第三方树居多）。不是改动，别去「修复」也别 `add -A`；看真实差异用 `git -c core.ignorecase=false diff --ignore-cr-at-eol`，`git add` 单文件时 git 会自动归一回 LF，提交内容不受影响。剩约 40 条是 `$Id$` 关键字形态差异，`--ignore-cr-at-eol` 消不掉，改到这类文件时在分析里点明 |
| `git status` 里整棵 `.trae/**` 是 ` D`、`openvpn-2.4.8/INSTALL` 是 ` D` | 结构性噪音（`.trae` 是指向主人知识库根的符号链接 + 仓库里文件与盘上目录同名），**不是这次 bug 造成的**：不许 `checkout` 它们（会写穿符号链接覆盖知识库）、不许写进 `--files`、不许为此 `block` |
| 在 Windows 侧改了 `xt_dscp.c`，结果 `xt_DSCP.c` 变了 / `git status` 说另一半被删 | Windows 的 `ignorecase` 把大小写对偶混成一个（实测两个拼名 md5 相同）。立刻 `git -c core.ignorecase=false checkout -- <被误改的那个>` 回退，改到 SSH 侧重做；这类事故只有 Linux 侧能看出来 |
| 工作树/分支基线莫名变化，或 `git status` 里冒出**别的产品/别的禅道 ID** 的文件 | 两个产品共用了同一份镜像却被并行处理（违反 §5）：立刻停掉其中一条队列，在 SSH 侧 `git -c core.ignorecase=false status` 核对，被串味的分支 `git reset --hard <基线>` 重做；要让它们真并行只能给其中一个另开一份镜像 |
| 某条被 AI 反复处理不满意 | 三种处置别混：「打回」→ `rejected`，**改动与草稿分支都不回滚**，AI 在同一分支上追加提交，最终一次推入 SVN；「拒绝并回滚」→ 删掉该草稿分支（提交先存进 `refs/rejected/<分支>` 可取回），这条直接置 `closed`，AI 不再重做；「重开补修」（限 `merged`/`closed`）→ 已入过库但仍不完全，回到 `rejected` 重新入队，`queue_kind=reopened` |
| 已合入的条目发现修得不完全 | 弹窗点「重开补修（入队列）」。登记的 r 号与草稿分支都保留，**trunk 不回退**（回退 trunk 是主人自己的 svn 动作）；上一轮的结论、改动文件、r 号作为 `prior_fix` 随任务下发。AI 在原分支续做；推送时以「上一轮推入的那个草稿提交」为增量基准，只把新改动入库，trunk 上已有的文件自动跳过，全一致就拒（不许空修订） |
| 推入 SVN 失败（编码、护栏拦下、远端不可达） | **不改状态**：只追加一条 `kind=manual` 人工留痕 + 禅道留言，`git:<哈希>` 草稿记录不动；修好原因后重推即可，多次草稿提交会合并成一次 svn 提交 |
| 推送被护栏拦下：`trunk 相对草稿基线已有他人改动` | 根因几乎都是镜像 `origin/trunk` 落后。留痕里会带「trunk 上最近一笔 r 号」和可自动对齐的文件清单：改动不重叠就勾「自动按 trunk 对齐」重推（三方合并，他人改动保留）；撞在同一行区间或涉及增删文件时不许勾，先 fetch 镜像或把这条打回让 AI 在新基线上重做。执行器不许自己调这个接口，也不许放宽护栏 |
| 模型或接口报限流（`rate limit` / `429` / `overloaded` / `quota` / 「稍后再试」/ 502、503） | **不许中止整轮**：等 5 分钟重试同一步，总共 3 次（`.env` 的 `RETRY_WAIT` / `RETRY_MAX`；本系统读禅道那侧已内置，会打印 `[retry] …等 N 秒后重试`）。3 次用完才 `block` 写「上游限流未恢复（已等 X 分钟重试 N 次）」，然后继续下一条。写类动作（评论、`i18n-commit`）不重试，失败就如实登记 |
| 模型报截图尺寸不合规（`400 invalid_parameter_error` / `must be larger than 10` / `height:1 or width:1`） | 那是禅道发的 1x1 占位图，不是你的错也不该中止：本系统已在下载时把它标 `unreadable` 并挪进 `images_skipped`（§3.4 四），执行器跳过它按文字继续即可；库里已有的历史附件跑一次 `python -m app.cli img-gate` 重量一遍 |
| 任务其实跑完了、镜像上有 `bugfix/zentao-<ID>` 提交，库里却还停在 `fixing`（子任务崩在收口前 / 忘了 `commit`） | `python -m app.cli reconcile` 点名所有这种条目；核对无误后 `reconcile <ID> --apply` 认领：登记 `git:<哈希>`（带真实哈希）、写回分支与文件清单、转 `await_review`，并在分析里标明「结论来自 git 提交信息，未验证」。不 apply 就只报告，什么都不改 |
| 某一步需要授权（SSH 要密码、IDE 命令审批、沙箱越权、`sudo`、yes/no 交互、要人点「继续」） | **禁止出现，也禁止等**：本系统 svn 已 `--non-interactive`、SSH 只走免密 key（参数见该库 `CLAUDE.local.yaml`）；看镜像 / 开分支 / 落草稿 / 对账一律用 §1.8 的 `mirror` / `checkout` / `draft` / `reconcile`，别自己拼裸 `ssh … git …`。真绕不开就 `block` 写清「要主人做什么授权 / 为什么 / 影响面 / 怎么回滚」，立刻做下一条；不许调 `AskUserQuestion` 之类的提问工具 |
| 弹「删除文件」的授权确认（清理 `a_<ID>.json` / 临时脚本 / `__pycache__`） | 实测最常见的卡死点，而且全是**没必要的删除**：分析长文本交给 `--analysis-file`、需方案文本交给 `--block-file`，命令入库成功后自己回收（返回里的 `handoff_removed`），想留着看就加 `--keep-handoff`；一批跑完 `python -m app.cli tmp-clean --apply` 统一清场。执行器侧一律不许出现删除动作（§1.8、§3.4 二） |
| 提示「本地文件比仓库新，是否比较」 | 执行器自己按 §3.4 第三步取证（SSH 侧 `status` + `diff --ignore-cr-at-eol`）：只剩 CRLF / `$Id$` / 大小写对偶 / `.trae/**` 就当已知噪音照常做；是别人写的真实改动就**不覆盖不 stash 不 revert**，`block` 写「<文件> 有一份不是我改的改动：<摘要>」；都不许停下来问人 |
| 某条想先搁置 | 让它 `need_solution`，或直接在库里改状态，它就不在队列里 |
| PENDING 一直没人提交 | 它已在 `await_review`，`report` 与 `/review` 都能看到；不提交就不会变 `merged` |
| 误改了别的文件 | 镜像里 `git reset --hard <分支基线>` 或 `git checkout -- <文件>` 即可回退（不影响正式库）；正式工作副本里则由主人按代码库规程 `svn revert`（本系统不自动 revert） |
| 禅道评论写不进去 | 不影响主流程，原文留在 `sync_log`（`/sync` 页可见）；之后用 `comment <ID> --text ...` 单独补发 |
| 中途关窗/IDE 重启 | 重贴 §3.1，状态从库里恢复，不会重复领任务 |
| 全队列跑完 | `report` 的 `remaining_work=0`，只剩你的审查与合入 trunk（手动） |

## 9. 一次完整实操（§1.5 本地 git 草稿方式）

```
# 你（一次性，且必须在有 git-svn 的那一侧跑 —— 编译服务器上通常没有 git-svn；
#     而且只能在 Linux 侧 clone：Windows 侧的 ignorecase + MAX_PATH 会让检出天生不全）
git svn clone --trunk=. --no-metadata <SVN仓库地址> <镜像目录>
git -C <镜像目录> rev-parse --verify refs/remotes/origin/trunk     # 有输出才算就绪
python -m app.cli bind-repo <产品ID> "<SVN仓库地址>" --name "<产品名>" --working-copy <镜像目录> --svn-working-copy <正式SVN工作副本> --build "<编译命令>" --test "<测试命令>"
python -m app.cli repos                       # 确认该产品已 bound、working_copy 指向镜像、svn_working_copy 已配

# 你（下达任务）：打开 /dispatch 复制 §0 或 §3.1（也可照抄下面这段）

# AI（自动）
sync --details -1 → tasks → 每条一个子任务
   （mirror 验镜像就绪 → status → claim → checkout 开/切 bugfix/zentao-<ID> → 改 → SSH 编译测试
     → 改了词条就 i18n-up → 改词条 → i18n-commit（单独进 SVN，拿 r<号>）
     → 闸门 G1–G12 → draft（只 add 列出的文件，只本地提交，返回短哈希）
     → commit --no-svn --revision git:<哈希> --analysis-file a.json）
   → 下一条 → report → reconcile（点名跑完没收口的条目）
   注：镜像的读与草稿提交都在 mirror / checkout / draft 三条命令里（§1.8），
   执行器不再自己拼 ssh + git；只有编译测试与定位取证仍按该库规程走 SSH

# 你（收尾）
/need 答复 → /review 逐条看分析与闸门 → 同样在 SSH 侧 git -c core.ignorecase=false log/diff 审改动
   → 通过后自己执行 git svn dcommit（或出 patch 打到正式副本再 svn ci）→ 拿到真实 r 号后回填：
python -m app.cli commit <禅道ID> --message "<一句话根因>" --files a.c,b.c --no-svn --revision r<真实号>
   → 是否 merge 到 trunk 由你决定 → merged 卡片点「标记已结案」
```

回填那一步为什么可以重复 `commit`：`svn_revisions` 对同一个 bug 是**追加**而不是覆盖，
`git:<哈希>`（本地草稿）与 `r<号>`（正式库）会并存在这条 bug 名下，审查记录是完整的。
