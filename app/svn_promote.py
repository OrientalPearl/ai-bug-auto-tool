"""Owner-side promotion: turn a draft (``git:<hash>``) into a real SVN revision.

AUTO_LOOP.md G11 keeps every remote write away from the executor; the one place it
does happen is this module, and only behind an explicit click in the review dialog.
Neither build server has ``git svn`` (verified: ``git: 'svn' is not a git command``),
so the draft cannot be dcommitted. Instead the promoted content is materialised
file by file: git objects are read inside the mirror, written into a sparse
(``--depth empty``) SVN working copy that holds nothing but the touched files, and
committed with the message the owner typed.

Safety properties this module is responsible for:

* read-only until the plan checks out -- a preflight never writes anywhere;
* every file's base content is compared against trunk before anything is written,
  so a draft can never stomp a change somebody else committed in the meantime;
* ``--non-interactive`` plus ``BatchMode=yes`` everywhere: a missing credential
  fails fast and loudly instead of hanging on a password prompt;
* the only working copy it ever touches is its own ``svn_promote`` directory --
  never the git mirror, never the owner's real checkout.
"""
from __future__ import annotations

import base64
import subprocess
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .config import get_settings

# Total wall clock allowed for one SSH round trip. Every svn/git command inside
# the remote script also carries its own `timeout`, so a dead repository server
# can slow a push down but never hang the web worker forever.
_SSH_TIMEOUT = 900


class PromoteError(RuntimeError):
    """A promotion that is refused before anything is written."""


@dataclass
class RemoteTarget:
    """The SSH endpoint and paths of one mirrored code line."""

    host: str
    user: str
    port: int
    mirror: str                  # the git-svn mirror on the Linux side
    identity_file: str = ""
    rsa_only: bool = False       # old OpenSSH needs the ssh-rsa knobs (2.3 line)
    promote_root: str = ""       # where the sparse SVN checkouts live

    @property
    def wc(self) -> str:
        tail = self.mirror.rstrip("/").rsplit("/", 1)[-1] or "mirror"
        return f"{self.promote_root.rstrip('/')}/{tail}"

    def ssh_command(self, remote: str) -> list[str]:
        cmd = [
            "ssh",
            "-o", "BatchMode=yes",            # never open a password prompt
            "-o", "ConnectTimeout=10",
            "-o", "ServerAliveInterval=30",
            "-o", "ServerAliveCountMax=3",
            "-p", str(self.port),
        ]
        if self.identity_file:
            cmd += ["-i", self.identity_file, "-o", "IdentitiesOnly=yes"]
        if self.rsa_only:
            cmd += ["-o", "HostKeyAlgorithms=+ssh-rsa",
                    "-o", "PubkeyAcceptedKeyTypes=+ssh-rsa"]
        return cmd + [f"{self.user}@{self.host}", remote]


@dataclass
class Promotion:
    """What one promotion attempt produced; ``markers`` keeps every raw line."""

    ok: bool = False
    dry_run: bool = False
    merge_trunk: bool = False
    ref: str = ""
    url: str = ""
    commit: str = ""
    base: str = ""
    drafts: list[str] = field(default_factory=list)
    want: list[str] = field(default_factory=list)
    conflicts: list[str] = field(default_factory=list)
    culprits: list[str] = field(default_factory=list)
    mergeable: list[str] = field(default_factory=list)
    overlaps: list[str] = field(default_factory=list)
    blocked: list[str] = field(default_factory=list)
    auto_merged: str = ""
    merged_files: list[str] = field(default_factory=list)
    hint: str = ""
    pending: list[str] = field(default_factory=list)
    revision: str = ""
    log: list[str] = field(default_factory=list)
    svn_output: list[str] = field(default_factory=list)
    ready: str = ""
    wc: str = ""                 # the sparse promote checkout used on the server
    error: str = ""
    exit_code: int = 0
    markers: list[tuple[str, str]] = field(default_factory=list)
    raw: str = ""

    def summary(self) -> str:
        if self.ok:
            tail = f"，其中 {len(self.merged_files)} 个已按 trunk 对齐" if self.merged_files else ""
            return f"已推入 SVN：r{self.revision}（{len(self.want)} 个文件{tail}）"
        if self.ready:
            extra = "，含自动对齐" if self.auto_merged else ""
            return f"预检通过：{self.ready}（没有写入任何东西{extra}）"
        return self.error or f"推送未完成（exit {self.exit_code}）"

    def detail(self) -> str:
        """Human-readable evidence for the dialog note and the failure record."""
        bits = [self.summary()]
        if self.conflicts:
            bits.append("trunk 已变动的文件：" + "；".join(self.conflicts[:6]))
        # A refusal should name the revision that moved, not just the md5 pair.
        if self.culprits:
            bits.append("trunk 上最近一笔：" + "；".join(self.culprits[:4]))
        if self.auto_merged:
            bits.append("自动对齐：" + self.auto_merged)
        elif self.mergeable:
            bits.append("可无损自动对齐（勾选后重推）：" + "、".join(self.mergeable[:6]))
        if self.overlaps:
            bits.append("改到同一行区间，必须人工对齐：" + "、".join(self.overlaps[:6]))
        if self.ready and self.pending:
            bits.append("待提交：" + "；".join(self.pending[:6]))
        elif self.want and (self.error or self.ready):
            bits.append("草稿文件：" + "、".join(self.want[:6]))
        if self.svn_output:
            bits.append("svn 输出：" + " / ".join(self.svn_output[-3:])[:400])
        return " | ".join(bits)


# ---------------------------------------------------------------------------
# where to run it: the mirror's own CLAUDE.local.yaml is the only path authority
# ---------------------------------------------------------------------------

def _yaml_block(text: str, block: str) -> dict[str, str]:
    """Very small ``key: value`` reader for one top-level block.

    ``CLAUDE.local.yaml`` is flat under each top-level key, which is deliberately
    handled here instead of adding a YAML dependency to a stdlib-only project.
    """
    values: dict[str, str] = {}
    inside = False
    for line in text.splitlines():
        if not line.strip() or line.lstrip().startswith("#"):
            continue
        if not line[0].isspace():
            inside = line.split(":", 1)[0].strip() == block
            continue
        if not inside:
            continue
        key, _, raw = line.strip().partition(":")
        if not raw.strip():
            continue
        values[key.strip()] = raw.split("#", 1)[0].strip().strip("'\"")
    return values


def remote_target(working_copy: Any) -> RemoteTarget:
    """Resolve the SSH side of a mirrored code line from its local directory."""
    root = Path(str(working_copy or "").strip())
    if not str(root):
        raise PromoteError("该产品没有绑定镜像工作副本（repos 页的 working_copy 为空）")
    cfg = root / "CLAUDE.local.yaml"
    if not cfg.is_file():
        raise PromoteError(f"镜像根缺少 CLAUDE.local.yaml：{root}")
    try:
        text = cfg.read_text(encoding="utf-8", errors="replace")
    except OSError as exc:
        raise PromoteError(f"读取 CLAUDE.local.yaml 失败：{exc}") from exc
    ssh = _yaml_block(text, "ssh")
    paths = _yaml_block(text, "path_aliases")
    host, user = ssh.get("host", ""), ssh.get("user", "")
    mirror = ssh.get("remote_workspace", "") or paths.get("LOCAL_WORKSPACE_ROOT", "")
    if not (host and user and mirror):
        raise PromoteError("CLAUDE.local.yaml 的 ssh.host / ssh.user / ssh.remote_workspace 不完整")
    port_raw = ssh.get("port", "22")
    identity = ssh.get("identity_file", "")
    # The 2.3 line's OpenSSH only accepts RSA keys. Say so in its CLAUDE.local.yaml
    # with `ssh.rsa_only: true`; naming an identity file implies it as well.
    rsa_only = ssh.get("rsa_only", "").lower() in ("1", "true", "yes") or bool(identity)
    promote_root = str(get_settings().svn_promote_root or "").strip()
    if not promote_root:
        # <remote home>/<project>/svn_promote -- beside the mirror, never inside it.
        parent = mirror.rstrip("/").rsplit("/", 2)[0]
        promote_root = f"{parent}/svn_promote"
    return RemoteTarget(
        host=host,
        user=user,
        port=int(port_raw) if str(port_raw).isdigit() else 22,
        mirror=mirror.rstrip("/"),
        identity_file=identity,
        rsa_only=rsa_only,
        promote_root=promote_root,
    )


# ---------------------------------------------------------------------------
# the remote half
# ---------------------------------------------------------------------------

_SCRIPT = r"""
# Promote one git draft into SVN. POSIX sh, runs on the build server.
# Every decision is reported as "PV|<tag>|<value>" for app/svn_promote.py.
export LC_ALL=C LANG=C
set -f
MIRROR=$1; REF=$2; WCPATH=$3; MSG_B64=$4; DRY=$5; MERGE=$6
TAB=$(printf '\t')
P() { printf 'PV|%s|%s\n' "$1" "$2"; }
TMPN=$(mktemp) || { P err "mktemp 失败"; exit 1; }
TMPW=$(mktemp) || { P err "mktemp 失败"; exit 1; }
TMPC=$(mktemp) || { P err "mktemp 失败"; exit 1; }
TMPT=$(mktemp) || { P err "mktemp 失败"; exit 1; }
TMPP=$(mktemp) || { P err "mktemp 失败"; exit 1; }
TMPM=$(mktemp) || { P err "mktemp 失败"; exit 1; }
TMPX=$(mktemp) || { P err "mktemp 失败"; exit 1; }
MDIR=$(mktemp -d) || { P err "mktemp -d 失败"; exit 1; }
trap 'rm -rf "$TMPN" "$TMPW" "$TMPC" "$TMPT" "$TMPP" "$TMPM" "$TMPX" "$MDIR" 2>/dev/null' EXIT
abort() {
  P err "$1"
  if [ "$2" = dirty ]; then
    cleanup_wc
    P reverted "已回退并清空 $WCPATH 里本次写入的内容"
  fi
  exit 1
}

# The promote checkout belongs to this script alone. svn 1.6 treats each nested
# depth-empty checkout as its own working copy, so status and cleanup have to walk
# the directories this push actually owns.
wc_status() {
  [ -s "$TMPP" ] || return 0
  while read D; do
    [ -n "$D" ] || continue
    [ -d "$D/.svn" ] || continue
    timeout 90 svn status "$D" 2>/dev/null
  done < "$TMPP"
}

cleanup_wc() {
  [ -s "$TMPP" ] || return 0
  while read D; do
    [ -n "$D" ] || continue
    [ -d "$D/.svn" ] || continue
    timeout 90 svn revert --non-interactive -R "$D" >/dev/null 2>&1
    timeout 90 svn status "$D" 2>/dev/null | grep '^?' | sed 's|^[^/]*||' |
      while read U; do
        [ -n "$U" ] || continue
        rm -rf "$U" 2>/dev/null
      done
  done < "$TMPP"
}

# Grow the file's own directory to depth=files so the file becomes versioned with
# pristine trunk content. `svn update <file>` alone is unreliable on 1.6: it only
# picks the path up when the entry already exists, which left earlier runs with an
# unversioned file and an empty commit list.
fetch_file() {
  _d=$(dirname "$1")
  if [ "$_d" = "." ]; then _t="$WCPATH"; else _t="$WCPATH/$_d"; fi
  timeout 90 svn update --non-interactive --set-depth files "$_t" >/dev/null 2>&1
  timeout 60 svn info --non-interactive "$WCPATH/$1" >/dev/null 2>&1 \
    || { P err "取 trunk 版本失败: $1"; return 1; }
  return 0
}

for t in svn git base64 md5sum timeout awk cp; do
  command -v "$t" >/dev/null 2>&1 || abort "该服务器缺少 $t"
done
cd "$MIRROR" 2>/dev/null || abort "镜像目录不存在: $MIRROR"

URL=$(git config --get svn-remote.svn.url 2>/dev/null)
[ -n "$URL" ] || abort "镜像里没有 svn-remote.svn.url，无法确定正式库地址"
P url "$URL"

git rev-parse --verify refs/remotes/origin/trunk >/dev/null 2>&1 \
  || abort "镜像缺 refs/remotes/origin/trunk（就绪判据没过），先按 §1.5 修镜像"
COMMIT=$(git rev-parse --verify "$REF^{commit}" 2>/dev/null)
[ -n "$COMMIT" ] || abort "草稿提交在镜像里找不到: $REF"
BASE=$(git merge-base refs/remotes/origin/trunk "$COMMIT" 2>/dev/null)
[ -n "$BASE" ] || abort "找不到 $COMMIT 与 origin/trunk 的共同祖先"
P commit "$COMMIT"
P base "$BASE"
git rev-list --abbrev-commit "$BASE..$COMMIT" 2>/dev/null |
  sed '/^$/d' | while read C; do P draft "$C"; done

git diff --name-status "$BASE" "$COMMIT" > "$TMPN" 2>/dev/null
[ -s "$TMPN" ] || abort "草稿相对 origin/trunk 没有任何改动: $COMMIT"
while IFS="$TAB" read ST P1 P2; do
  [ -n "$ST" ] || continue
  case "$ST" in
    A*) printf 'A\t%s\n' "$P1" >> "$TMPW" ;;
    D*) printf 'D\t%s\n' "$P1" >> "$TMPW" ;;
    M*|T*) printf 'M\t%s\n' "$P1" >> "$TMPW" ;;
    R*) printf 'D\t%s\n' "$P1" >> "$TMPW"; printf 'A\t%s\n' "$P2" >> "$TMPW" ;;
    C*) printf 'A\t%s\n' "$P2" >> "$TMPW" ;;
    *) abort "无法处理的 git 状态 $ST（$P1）" ;;
  esac
done < "$TMPN"
[ -s "$TMPW" ] || abort "草稿 diff 解析不出文件清单"
while IFS="$TAB" read ST RP; do P want "$ST|$RP"; done < "$TMPW"

# Absolute commit targets plus the working-copy dirs that own them. svn 1.6 keeps
# every depth-empty checkout as a *nested* working copy, so neither `svn status`
# nor `svn ci` on the root sees what lives under it -- all later steps therefore
# work off these explicit target lists.
: > "$TMPT"
: > "$TMPP"
while IFS="$TAB" read ST RP; do
  _dir=$(dirname "$RP")
  if [ "$_dir" = "." ]; then
    printf '%s\n' "$WCPATH" >> "$TMPP"
  else
    printf '%s\n' "$WCPATH/$_dir" >> "$TMPP"
  fi
  printf '%s\n' "$WCPATH/$RP" >> "$TMPT"
done < "$TMPW"
sort -u "$TMPP" -o "$TMPP"

# Guard 1: the draft's base must still be what trunk holds, or somebody else has
# landed on the same file after the draft was cut. By default that refuses the
# push outright. With MERGE=1 (the owner ticked "自动按 trunk 对齐") a file whose
# trunk-side change touches no line the draft changed is merged three-way with
# `git merge-file` and the merged result -- theirs plus ours, nothing stomped --
# is what gets committed. Everything that cannot be proven non-overlapping (a
# real overlap, an add/delete clash, a file trunk no longer has) blocks the push.
: > "$TMPM"
: > "$TMPX"
while IFS="$TAB" read ST RP; do
  SMD5=$(timeout 90 svn cat --non-interactive "$URL/$RP" 2>/dev/null | md5sum | cut -d' ' -f1)
  GMD5=$(git show "$BASE:$RP" 2>/dev/null | md5sum | cut -d' ' -f1)
  STOMP=
  case "$ST" in
    A) if [ -n "$SMD5" ]; then
         if [ "$SMD5" = "$GMD5" ]; then WHY=same; else WHY=diff; STOMP=1; fi
         printf '%s\n' "A $RP trunk 上已存在同名文件（内容 $WHY）" >> "$TMPC"
       fi ;;
    *) if [ -z "$SMD5" ]; then
         printf '%s\n' "$ST $RP trunk 上已找不到该文件（可能已被别人删除）" >> "$TMPC"
         printf '%s\n' "$RP|trunk 上文件已不存在，不能自动合并" >> "$TMPX"
         STOMP=1
       elif [ "$SMD5" != "$GMD5" ]; then
         printf '%s\n' "$ST $RP trunk 内容已变动 svn=$SMD5 base=$GMD5" >> "$TMPC"
         STOMP=1
       fi ;;
  esac
  [ -n "$STOMP" ] || continue

  # Name the revision that moved under the draft, so a refusal explains itself
  # instead of leaving the owner to run the svn log by hand.
  WHO=$(timeout 60 svn log --non-interactive -l 1 "$URL/$RP" 2>/dev/null |
        sed -n '2p' | sed 's/^[[:space:]]*//; s/[[:space:]]*$//')
  [ -n "$WHO" ] && P culprit "$RP <= $WHO"

  case "$ST" in
    M*|T*) : ;;
    *) printf '%s\n' "$RP|状态 $ST（新增或删除）需要人工判断" >> "$TMPX"
       continue ;;
  esac
  _slug=$(printf '%s' "$RP" | tr '/ ' '__')
  git show "$COMMIT:$RP" > "$MDIR/$_slug.mine" 2>/dev/null || \
    { printf '%s\n' "$RP|取草稿内容失败" >> "$TMPX"; continue; }
  git show "$BASE:$RP" > "$MDIR/$_slug.base" 2>/dev/null || \
    { printf '%s\n' "$RP|取草稿基线失败" >> "$TMPX"; continue; }
  timeout 90 svn cat --non-interactive "$URL/$RP" > "$MDIR/$_slug.trunk" 2>/dev/null || \
    { printf '%s\n' "$RP|取 trunk 内容失败" >> "$TMPX"; continue; }
  cp "$MDIR/$_slug.mine" "$MDIR/$_slug.out" 2>/dev/null || \
    { printf '%s\n' "$RP|准备合并目标失败" >> "$TMPX"; continue; }
  if git merge-file "$MDIR/$_slug.out" "$MDIR/$_slug.base" "$MDIR/$_slug.trunk" 2>/dev/null; then
    P mergeable "$RP"
    printf '%s\t%s\n' "$RP" "$MDIR/$_slug.out" >> "$TMPM"
  else
    rm -f "$MDIR/$_slug.out"
    printf '%s\n' "$RP|trunk 与草稿改到了同一行区间" >> "$TMPX"
    P overlap "$RP trunk 与草稿改到了同一行区间"
  fi
done < "$TMPW"
if [ -s "$TMPC" ]; then
  while read L; do P conflict "$L"; done < "$TMPC"
  NM=$(wc -l < "$TMPM" | tr -d ' ')
  NX=$(wc -l < "$TMPX" | tr -d ' ')
  if [ "$MERGE" = 1 ] && [ "$NX" = 0 ] && [ "$NM" -gt 0 ]; then
    P auto "$NM 个文件已与 trunk 现内容三方合并（他人改动全部保留，且与本草稿改动不重叠）"
  elif [ "$MERGE" = 1 ]; then
    while IFS="$TAB" read L; do P blocked "$L"; done < "$TMPX"
    abort "无法全部无损对齐（重叠或增删 $NX 处），整笔拒绝；trunk 未被改动，草稿未被改动"
  elif [ "$NM" -gt 0 ]; then
    P hint "$NM 个文件可无损自动对齐（勾选「自动按 trunk 对齐」后重推）"
    abort "trunk 相对草稿基线已有他人改动，拒绝覆盖；$NM 个文件其实与本草稿改在不同行区间，可勾选「自动按 trunk 对齐」直接重推"
  else
    abort "trunk 相对草稿基线已有他人改动，拒绝覆盖；先把草稿对齐到当前 trunk 再推"
  fi
fi

# Guard 2: the dirs we are about to use must start clean. That checkout is owned by
# this script alone and is never edited by hand, so anything found in it is the
# debris of an earlier interrupted push: it is reverted and dropped automatically
# rather than stalling the owner. Only a leftover that survives the cleanup stops
# the push.
if [ -n "$(wc_status)" ]; then
  cleanup_wc
  P cleaned "推送副本里有上次中断的残留，已自动回退并清空"
  LEFT=$(wc_status | grep -v '^?' | sed 's|^[^/]*||' | sed '/^$/d')
  [ -z "$LEFT" ] || abort "推送工作副本回退后仍不干净: $(printf '%s' "$LEFT" | tr '\n' ';' | cut -c1-300)"
fi

if [ "$DRY" = 1 ]; then
  P ready "$(wc -l < "$TMPW") 个文件待写入 $WCPATH 并提交进 $URL"
  exit 0
fi

if [ ! -d "$WCPATH/.svn" ]; then
  mkdir -p "$(dirname "$WCPATH")" 2>/dev/null || abort "无法创建 $(dirname "$WCPATH")"
  timeout 300 svn checkout --depth empty --non-interactive "$URL" "$WCPATH" >/dev/null 2>&1 \
    || abort "稀疏检出失败: $URL"
  P wc "$WCPATH"
fi

# Bring every ancestor directory of a touched file in as a depth-empty checkout,
# top down; the file itself is fetched afterwards by fetch_file.
ensure_dir() {
  _rel=$(dirname "$1")
  [ "$_rel" = "." ] && return 0
  _total=$(printf '%s/\n' "$_rel" | tr -cd '/' | wc -c)
  _i=1
  while [ "$_i" -le "$_total" ]; do
    _acc=$(printf '%s\n' "$_rel" | cut -d/ -f1-"$_i")
    if [ ! -d "$WCPATH/$_acc/.svn" ]; then
      mkdir -p "$WCPATH/$_acc" 2>/dev/null
      timeout 90 svn checkout --depth empty --non-interactive "$URL/$_acc" "$WCPATH/$_acc" >/dev/null 2>&1
      [ -d "$WCPATH/$_acc/.svn" ] || { P err "目录检出失败: $_acc"; return 1; }
    fi
    _i=$((_i + 1))
  done
  return 0
}

while IFS="$TAB" read ST RP; do
  ensure_dir "$RP" || abort "准备目录失败: $RP" dirty
  SRC=$(awk -F'\t' -v f="$RP" '$1==f{print $2}' "$TMPM" 2>/dev/null | head -1)
  case "$ST" in
    A) git show "$COMMIT:$RP" > "$WCPATH/$RP" 2>/dev/null \
         || abort "导出草稿内容失败（新增）: $RP" dirty
       timeout 90 svn add --non-interactive "$WCPATH/$RP" >/dev/null 2>&1 \
         || abort "svn add 失败: $RP" dirty ;;
    D) fetch_file "$RP" || abort "取 trunk 版本失败: $RP" dirty
       timeout 90 svn delete --non-interactive "$WCPATH/$RP" >/dev/null 2>&1 \
         || abort "svn delete 失败: $RP" dirty ;;
    *) fetch_file "$RP" || abort "取 trunk 版本失败: $RP" dirty
       if [ -n "$SRC" ]; then
         cp "$SRC" "$WCPATH/$RP" 2>/dev/null || abort "写入对齐后的内容失败: $RP" dirty
         P merged "$RP"
       else
         git show "$COMMIT:$RP" > "$WCPATH/$RP" 2>/dev/null \
           || abort "导出草稿内容失败: $RP" dirty
       fi ;;
  esac
done < "$TMPW"

# Guard 3: what svn is about to send must be exactly the draft's file list.
PENDING=$(wc_status | grep -v '^?')
printf '%s\n' "$PENDING" | sed 's|^[^/]*||' | sed '/^$/d' | sort > "$TMPN"
sort "$TMPT" > "$TMPC"
EXTRA=$(comm -23 "$TMPN" "$TMPC" | sed '/^$/d')
MISSING=$(comm -13 "$TMPN" "$TMPC" | sed '/^$/d')
printf '%s\n' "$PENDING" | sed '/^$/d' | while read L; do P pend "$L"; done
if [ -n "$EXTRA" ] || [ -n "$MISSING" ]; then
  abort "待提交清单与草稿不一致 extra=[$(printf '%s' "$EXTRA" | tr '\n' ';' | cut -c1-300)] missing=[$(printf '%s' "$MISSING" | tr '\n' ';' | cut -c1-300)]" dirty
fi

MSGF=$(mktemp)
printf '%s' "$MSG_B64" | base64 -d > "$MSGF" 2>/dev/null || abort "提交说明解码失败" dirty
[ -s "$MSGF" ] || abort "提交说明为空" dirty

# Say so in the log itself: without this line a reader comparing the commit with
# the draft branch would wonder where the extra trunk context came from.
if [ "$MERGE" = 1 ] && [ -s "$TMPM" ]; then
  ALIGNED=$(awk -F'\t' '{printf "%s ", $1}' "$TMPM" | sed 's/ $//')
  printf '\n（推送前已将以下文件对齐到 trunk 现内容，本次提交只含本草稿的改动：%s）\n' \
    "$ALIGNED" >> "$MSGF"
  P annotated "$ALIGNED"
fi

set --
while read T; do
  [ -n "$T" ] && set -- "$@" "$T"
done < "$TMPT"
[ $# -gt 0 ] || abort "提交目标为空" dirty
P target "$1"
P targets "$#"

# --encoding UTF-8 is not cosmetic: LC_ALL=C above makes svn read the log file as
# plain ASCII and a Chinese message then dies with "Can't convert string from
# native encoding to 'UTF-8'" on the http commit path (verified in the field).
# Declaring the encoding keeps the message bytes intact and the English output
# that the revision parse below relies on.
OUT=$(timeout 300 svn ci --non-interactive --encoding UTF-8 -F "$MSGF" "$@" 2>&1)
RC=$?
rm -f "$MSGF"
printf '%s\n' "$OUT" | sed '/^$/d' | while read L; do P svnout "$L"; done
if [ "$RC" -ne 0 ]; then
  abort "svn ci 失败（exit $RC），本次写入已回退" dirty
fi
REV=$(printf '%s\n' "$OUT" | sed -n 's/.*Committed revision \([0-9][0-9]*\).*/\1/p' | head -1)
[ -n "$REV" ] || abort "svn ci 已执行但读不到修订号，请人工核对 trunk 日志"
P rev "$REV"
cleanup_wc
timeout 90 svn log --non-interactive -r "$REV" -l 1 "$URL" 2>/dev/null |
  sed '/^$/d' | head -6 | while read L; do P log "$L"; done
P done "$REV"
"""


def _decode(raw: bytes) -> str:
    """The 2.3 host speaks GBK; the 3.0 host speaks UTF-8."""
    for encoding in ("utf-8", "gbk"):
        try:
            return raw.decode(encoding)
        except UnicodeDecodeError:
            continue
    return raw.decode("utf-8", errors="replace")


def _run_script(target: RemoteTarget, ref: str, message: str, dry_run: bool,
                merge_trunk: bool = False) -> tuple[int, str]:
    args = [
        target.mirror,
        ref,
        target.wc,
        base64.b64encode(message.encode("utf-8")).decode("ascii"),
        "1" if dry_run else "0",
        "1" if merge_trunk else "0",
    ]
    quoted = " ".join("'" + str(a).replace("'", "'\\''") + "'" for a in args)
    cmd = target.ssh_command(f"sh -s {quoted}")
    proc = subprocess.run(
        cmd,
        input=_SCRIPT.strip().replace("\r\n", "\n").encode("utf-8"),
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        timeout=_SSH_TIMEOUT,
    )
    text = _decode(proc.stdout or b"")
    err = _decode(proc.stderr or b"")
    if err.strip():
        text += "\n" + "\n".join(
            f"PV|ssherr|{line}" for line in err.strip().splitlines()[:8]
        )
    return proc.returncode, text


def _parse(text: str, carrier: Promotion) -> Promotion:
    for line in (text or "").splitlines():
        if not line.startswith("PV|"):
            continue
        parts = line.split("|", 2)
        if len(parts) != 3:
            continue
        tag, value = parts[1], parts[2]
        carrier.markers.append((tag, value))
        if tag == "url":
            carrier.url = value
        elif tag == "commit":
            carrier.commit = value
        elif tag == "base":
            carrier.base = value
        elif tag == "draft":
            carrier.drafts.append(value)
        elif tag == "want":
            carrier.want.append(value)
        elif tag == "conflict":
            carrier.conflicts.append(value)
        elif tag == "culprit":
            carrier.culprits.append(value)
        elif tag == "mergeable":
            carrier.mergeable.append(value)
        elif tag == "overlap":
            carrier.overlaps.append(value)
        elif tag == "blocked":
            carrier.blocked.append(value)
        elif tag == "merged":
            carrier.merged_files.append(value)
        elif tag == "auto":
            carrier.auto_merged = value
        elif tag == "hint":
            carrier.hint = value
        elif tag == "pend":
            carrier.pending.append(value)
        elif tag == "rev":
            carrier.revision = value
        elif tag == "log":
            carrier.log.append(value)
        elif tag == "svnout":
            carrier.svn_output.append(value)
        elif tag == "ready":
            carrier.ready = value
        elif tag == "err":
            carrier.error = carrier.error or value
        elif tag == "done":
            carrier.ok = True
            carrier.revision = carrier.revision or value
    carrier.raw = text
    return carrier


def promote(bug: dict, message: str, *, dry_run: bool = False,
            merge_trunk: bool = False) -> Promotion:
    """Push one draft into SVN; with ``dry_run`` only the preflight runs.

    The reference is the bug's branch when it is registered (so several draft
    commits travel together), otherwise the newest ``git:<hash>``.

    ``merge_trunk`` is the owner's explicit tick: when trunk moved under the
    draft, files whose trunk-side change does not overlap this draft's lines are
    aligned three-way and the merged content is committed. Without it a moved
    trunk always refuses the push.
    """
    text = str(message or "").strip()
    if not text:
        raise PromoteError("提交说明不能为空 -- 它就是你写进 svn log 的那句话")
    if len(text) > 2000:
        raise PromoteError("提交说明过长（>2000 字），精简后再推")

    candidates: list[str] = []
    branch = str(bug.get("branch") or "").strip()
    if branch:
        candidates.append(f"refs/heads/{branch}")
    for rev in reversed(bug.get("revisions") or []):
        value = str(rev.get("revision") or "")
        if value.startswith("git:"):
            candidates.append(value.split(":", 1)[1])
    if not candidates:
        raise PromoteError("这条 bug 没有可推的草稿（既无分支也无 git:<哈希> 登记）")

    # The mirror path comes from the product's own binding, exactly like every
    # other repository operation in this system.
    from . import svn_client

    mirror_dir = svn_client.resolve_target(bug=bug).working_copy
    target = remote_target(mirror_dir)

    carrier = Promotion(dry_run=dry_run, ref=candidates[0], wc=target.wc,
                        merge_trunk=bool(merge_trunk))
    carrier.exit_code, output = _run_script(target, carrier.ref, text, dry_run,
                                            bool(merge_trunk))
    prom = _parse(output, carrier)
    if not prom.ok and not prom.error and not prom.ready:
        prom.error = f"远端执行未完成（exit {prom.exit_code}）"
    if prom.error:
        prom.error = f"{prom.error}（{target.host} · {target.mirror}）"
    return prom


# ---------------------------------------------------------------------------
# rolling a draft back (the owner refused the fix)
# ---------------------------------------------------------------------------

@dataclass
class DraftRollback:
    """Outcome of discarding one bug's draft branch."""

    ok: bool = False
    branch: str = ""
    tip: str = ""
    shadow: str = ""
    absent: bool = False          # nothing to roll back: the branch was already gone
    error: str = ""
    raw: str = ""

    def summary(self) -> str:
        if self.absent:
            return "没有草稿分支需要回滚（可能已被清理）"
        if self.ok:
            return (f"已回滚草稿分支 {self.branch}；提交 {self.tip[:10]} "
                    f"保留在 {self.shadow}，需要时可取回")
        return self.error or "回滚未完成"


_ROLLBACK_SCRIPT = r"""
# Drop one bug's draft branch without moving HEAD and without touching the work
# tree, keeping the commit reachable through a shadow ref so the owner can get it
# back. POSIX sh, runs on the build server.
export LC_ALL=C LANG=C
set -f
MIRROR=$1; BR=$2
P() { printf 'PV|%s|%s\n' "$1" "$2"; }
cd "$MIRROR" 2>/dev/null || { P err "镜像目录不存在: $MIRROR"; exit 1; }
case "$BR" in
  bugfix/*) : ;;
  *) P err "只允许回滚 bugfix/* 草稿分支，收到: $BR"; exit 1 ;;
esac
if ! git rev-parse --verify "refs/heads/$BR" >/dev/null 2>&1; then
  P absent "$BR"
  exit 0
fi
CUR=$(git rev-parse --abbrev-ref HEAD 2>/dev/null)
if [ "$CUR" = "$BR" ]; then
  P err "镜像的 HEAD 正停在 $BR（大概率有会话在它上面改代码），不动别人的工作区：先让那条会话切走再回滚"
  exit 1
fi
TIP=$(git rev-parse "refs/heads/$BR")
P tip "$TIP"
git update-ref "refs/rejected/$BR" "$TIP" || { P err "写影子引用 refs/rejected/$BR 失败"; exit 1; }
P shadow "refs/rejected/$BR"
git branch -D "$BR" >/dev/null 2>&1 || { P err "删除分支失败: $BR"; exit 1; }
git rev-parse --verify "refs/heads/$BR" >/dev/null 2>&1 && { P err "分支仍然存在: $BR"; exit 1; }
P done "$TIP"
"""


def rollback_draft(bug: dict) -> DraftRollback:
    """Delete the bug's draft branch after parking its commit under ``refs/rejected``.

    Deliberately conservative: it never checks a branch out, never touches the
    working tree, and refuses to run while HEAD sits on that branch, so a parallel
    session's uncommitted work cannot be disturbed. The commit stays reachable, so
    a rollback is recoverable while the object survives in the mirror.
    """
    branch = str(bug.get("branch") or "").strip()
    if not branch:
        return DraftRollback(ok=True, absent=True)

    from . import svn_client

    mirror_dir = svn_client.resolve_target(bug=bug).working_copy
    target = remote_target(mirror_dir)
    args = [target.mirror, branch]
    quoted = " ".join("'" + str(a).replace("'", "'\\''") + "'" for a in args)
    cmd = target.ssh_command(f"sh -s {quoted}")
    proc = subprocess.run(
        cmd,
        input=_ROLLBACK_SCRIPT.strip().replace("\r\n", "\n").encode("utf-8"),
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        timeout=_SSH_TIMEOUT,
    )
    text = _decode(proc.stdout or b"")
    err = _decode(proc.stderr or b"")
    out = DraftRollback(branch=branch)
    for line in text.splitlines():
        if not line.startswith("PV|"):
            continue
        parts = line.split("|", 2)
        if len(parts) != 3:
            continue
        tag, value = parts[1], parts[2]
        if tag == "tip":
            out.tip = value
        elif tag == "shadow":
            out.shadow = value
        elif tag == "absent":
            out.absent = True
            out.ok = True
        elif tag == "done":
            out.ok = True
            out.tip = out.tip or value
        elif tag == "err":
            out.error = out.error or value

    # The build servers print a login banner on stderr even for a clean run, so
    # stderr counts as a failure only when the script itself did not report one
    # and exited non-zero.
    if not out.ok and not out.error and err.strip():
        out.error = err.strip().splitlines()[-1]
    if not out.ok and not out.error and proc.returncode != 0:
        out.error = f"回滚未完成（exit {proc.returncode}）"
    out.raw = text if not err.strip() else text + "\n[stderr] " + err.strip()
    if out.error:
        out.error = f"{out.error}（{target.host} · {target.mirror}）"
    return out
