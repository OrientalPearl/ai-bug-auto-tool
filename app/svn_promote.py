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
import re
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


def lock_limit_minutes() -> int:
    """How long a 0-byte index.lock must sit untouched before a writer sweeps it.

    Kept well above the time git needs to rewrite this mirror's ~96 MB index: a
    live writer is continuously streaming into the lock, so it never idles this
    long. ``LOCK_STALE_MINUTES=0`` disables the automatic sweep entirely -- a
    leftover then only gets reported and must be removed by hand.
    """
    try:
        return max(0, int(get_settings().lock_stale_minutes))
    except (TypeError, ValueError, AttributeError):
        return 5


def read_lock_marker(obj, tag: str, value: str) -> bool:
    """Copy a ``lock`` / ``swept`` marker onto any of the three result dataclasses.

    Kept in one place because mirror / checkout / draft all carry the same fields:
    the numbers are the point, so an operator reading a block can tell a leftover
    lock from a live one without logging into the build server.
    """
    if tag == "lock":
        bits = (value.split("|") + ["0", "0", "0", "0"])[:4]
        try:
            obj.lock_state = bits[0] or "absent"
            obj.lock_size = int(bits[1])
            obj.lock_age = int(bits[2])
            obj.lock_procs = int(bits[3])
        except ValueError:
            return False
        return True
    if tag == "swept":
        obj.lock_swept = value.strip()
        return True
    return False


def read_dirt_marker(obj, tag: str, value: str) -> bool:
    """Copy a ``dirt`` / ``dirt_paths`` / ``dirt_why`` / ``gitv`` marker.

    The fourth field of ``dirt`` is the one that saves runs: on the 2.3 mirror the
    git is 1.7.1, so an option probe can fail, and "probe failed" must never be
    rendered as "tree clean". Carrying the state plus git's own stderr words means
    a blocked bug reads as "could not measure" instead of the false "dirty_real=0
    but the checkout still failed" that blocked thirty tickets.
    """
    if tag == "dirt":
        bits = (value.split("|") + ["0", "0", "0", ""])[:4]
        try:
            obj.dirty_real = int(bits[0] or 0)
            obj.dirty_noise = int(bits[1] or 0)
            obj.dirty_untracked = int(bits[2] or 0)
        except ValueError:
            return False
        obj.dirty_state = bits[3] or "failed"
        return True
    if tag == "dirt_paths":
        obj.dirty_paths = value.strip()
        return True
    if tag == "dirt_why":
        obj.dirt_why = value.strip()
        return True
    if tag == "gitv":
        obj.git_version = value.strip()
        return True
    return False


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
    landed: list[str] = field(default_factory=list)
    landed_summary: str = ""
    todo: list[str] = field(default_factory=list)
    base_source: str = ""
    base_ref: str = ""
    base_fall: str = ""
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

    def files_to_commit(self) -> list[str]:
        """The relative paths this push actually writes (draft list minus landed)."""
        source = self.todo or self.want
        return [item.split("|", 1)[1] for item in source if "|" in item]

    def summary(self) -> str:
        count = len(self.files_to_commit())
        if self.ok:
            tail = f"，其中 {len(self.merged_files)} 个已按 trunk 对齐" if self.merged_files else ""
            if self.landed:
                tail += f"，{len(self.landed)} 个上一轮已入库的不再重复提交"
            return f"已推入 SVN：r{self.revision}（{count} 个文件{tail}）"
        if self.ready:
            extra = "，含自动对齐" if self.auto_merged else ""
            if self.landed:
                extra += f"，跳过 {len(self.landed)} 个已入库文件"
            return f"预检通过：{self.ready}（没有写入任何东西{extra}）"
        return self.error or f"推送未完成（exit {self.exit_code}）"

    @property
    def round_note(self) -> str:
        """How this push is based: fresh from trunk, or on top of a landed round."""
        if self.base_source == "promoted":
            return f"本轮是补修：增量基准取上一轮推入的草稿提交 {self.base_ref[:10]}"
        if self.base_fall:
            return f"本轮增量基准不可用，已退回 origin/trunk：{self.base_fall}"
        return ""

    def detail(self) -> str:
        """Human-readable evidence for the dialog note and the failure record."""
        bits = [self.summary()]
        if self.round_note:
            bits.append(self.round_note)
        if self.landed_summary:
            bits.append("已入库跳过：" + self.landed_summary)
        elif self.landed:
            bits.append("trunk 上已是草稿内容的文件：" + "、".join(self.landed[:6]))
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
MIRROR=$1; REF=$2; WCPATH=$3; MSG_B64=$4; DRY=$5; MERGE=$6; BASE_REF=$7
TAB=$(printf '\t')
P() { printf 'PV|%s|%s\n' "$1" "$2"; }
TMPN=$(mktemp) || { P err "mktemp 失败"; exit 1; }
TMPW=$(mktemp) || { P err "mktemp 失败"; exit 1; }
TMPC=$(mktemp) || { P err "mktemp 失败"; exit 1; }
TMPT=$(mktemp) || { P err "mktemp 失败"; exit 1; }
TMPP=$(mktemp) || { P err "mktemp 失败"; exit 1; }
TMPM=$(mktemp) || { P err "mktemp 失败"; exit 1; }
TMPX=$(mktemp) || { P err "mktemp 失败"; exit 1; }
TMPL=$(mktemp) || { P err "mktemp 失败"; exit 1; }
TMPA=$(mktemp) || { P err "mktemp 失败"; exit 1; }
TMPQ=$(mktemp) || { P err "mktemp 失败"; exit 1; }
MDIR=$(mktemp -d) || { P err "mktemp -d 失败"; exit 1; }
trap 'rm -rf "$TMPN" "$TMPW" "$TMPC" "$TMPT" "$TMPP" "$TMPM" "$TMPX" "$TMPL" "$TMPA" "$TMPQ" "$MDIR" 2>/dev/null' EXIT
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

COMMIT=$(git rev-parse --verify "$REF^{commit}" 2>/dev/null)
[ -n "$COMMIT" ] || abort "草稿提交在镜像里找不到: $REF"
if [ -n "$BASE_REF" ] && git rev-parse --verify "$BASE_REF^{commit}" >/dev/null 2>&1 \
   && git merge-base --is-ancestor "$BASE_REF" "$COMMIT" 2>/dev/null; then
  # Round two of a reopened bug: the previous round is already in trunk, so the
  # increment has to be measured from the commit that was pushed, not from the
  # (usually stale) mirror trunk. Otherwise the first round's own change reads
  # as "somebody else edited this file" and the bug can never be finished.
  BASE=$(git rev-parse "$BASE_REF^{commit}")
  P base_source promoted
else
  [ -n "$BASE_REF" ] && P basefall "$BASE_REF 不是 $COMMIT 的祖先（分支被改写或重开过），改用 origin/trunk 作基准"
  git rev-parse --verify refs/remotes/origin/trunk >/dev/null 2>&1 \
    || abort "镜像缺 refs/remotes/origin/trunk（就绪判据没过），先按 §1.5 修镜像"
  BASE=$(git merge-base refs/remotes/origin/trunk "$COMMIT" 2>/dev/null)
  [ -n "$BASE" ] || abort "找不到 $COMMIT 与 origin/trunk 的共同祖先"
  P base_source trunk
fi
P commit "$COMMIT"
P base "$BASE"
git rev-list --abbrev-commit "$BASE..$COMMIT" 2>/dev/null |
  sed '/^$/d' | while read C; do P draft "$C"; done

git diff --name-status "$BASE" "$COMMIT" > "$TMPN" 2>/dev/null
[ -s "$TMPN" ] || abort "草稿相对基准 $(printf '%.10s' "$BASE") 没有任何改动"
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

# Guard 1: the draft's base must still be what trunk holds, or somebody else has
# landed on the same file after the draft was cut. By default that refuses the
# push outright. With MERGE=1 (the owner ticked "自动按 trunk 对齐") a file whose
# trunk-side change touches no line the draft changed is merged three-way with
# `git merge-file` and the merged result -- theirs plus ours, nothing stomped --
# is what gets committed. Everything that cannot be proven non-overlapping (a
# real overlap, an add/delete clash, a file trunk no longer has) blocks the push.
: > "$TMPM"
: > "$TMPX"
: > "$TMPA"
while IFS="$TAB" read ST RP; do
  [ -n "$RP" ] || continue
  _pre=$(printf '%s' "$RP" | tr '/ ' '__')
  # Existence has to come from the command's exit status: `svn cat` on a missing
  # path yields empty output, and md5sum of empty output is a perfectly good hash
  # (d41d8cd...), which used to make "trunk does not have this file" look like a
  # content difference and refuse clean additions.
  if timeout 90 svn cat --non-interactive "$URL/$RP" > "$MDIR/$_pre.trunk" 2>/dev/null; then
    SMD5=$(md5sum < "$MDIR/$_pre.trunk" | cut -d' ' -f1); HAVE_T=1
  else
    SMD5=""; HAVE_T=0
  fi
  if git show "$BASE:$RP" > "$MDIR/$_pre.base" 2>/dev/null; then
    GMD5=$(md5sum < "$MDIR/$_pre.base" | cut -d' ' -f1); HAVE_B=1
  else
    GMD5=""; HAVE_B=0
  fi
  if git show "$COMMIT:$RP" > "$MDIR/$_pre.mine" 2>/dev/null; then
    DMD5=$(md5sum < "$MDIR/$_pre.mine" | cut -d' ' -f1); HAVE_D=1
  else
    DMD5=""; HAVE_D=0
  fi

  if [ "$HAVE_T" = 1 ] && [ "$HAVE_D" = 1 ] && [ "$SMD5" = "$DMD5" ]; then
    # trunk already holds exactly what this draft would write: an earlier round of
    # the same bug that landed (typically pushed by hand before the bug was
    # reopened). Not a conflict, and committing it again would be an empty revision.
    P already "$ST $RP"
    printf '%s\n' "$RP" >> "$TMPA"
    continue
  fi
  STOMP=
   case "$ST" in
     A) if [ "$HAVE_T" = 1 ]; then
          printf '%s\n' "A $RP trunk 上已存在同名文件（内容 $([ "$SMD5" = "$GMD5" ] && echo same || echo diff)）" >> "$TMPC"
          STOMP=1
        fi ;;
    D*) if [ "$HAVE_T" = 0 ]; then
          # The file this draft deletes is already gone from trunk: the intended
          # end state holds, so there is nothing left to commit for it.
          P already "D $RP"
          printf '%s\n' "$RP" >> "$TMPA"
          continue
        fi
        if [ "$HAVE_B" = 0 ]; then
          printf '%s\n' "D $RP 草稿基线里没有该文件，无法比对" >> "$TMPC"
          printf '%s\n' "$RP|草稿基线里也没有这个文件，无法判断谁删的" >> "$TMPX"
          STOMP=1
        elif [ "$SMD5" != "$GMD5" ]; then
          printf '%s\n' "D $RP trunk 内容已变动 svn=$SMD5 base=$GMD5（删除要先对齐）" >> "$TMPC"
          STOMP=1
        fi ;;
    *) if [ "$HAVE_T" = 0 ]; then
         printf '%s\n' "$ST $RP trunk 上已找不到该文件（可能已被别人删除）" >> "$TMPC"
         printf '%s\n' "$RP|trunk 上文件已不存在，不能自动合并" >> "$TMPX"
         STOMP=1
       elif [ "$HAVE_B" = 0 ]; then
         printf '%s\n' "$ST $RP 草稿基线里没有该文件，无法比对" >> "$TMPC"
         printf '%s\n' "$RP|草稿基线里也没有这个文件，无法判断谁改的" >> "$TMPX"
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
  cp "$MDIR/$_pre.mine" "$MDIR/$_pre.out" 2>/dev/null || \
    { printf '%s\n' "$RP|准备合并目标失败" >> "$TMPX"; continue; }
  if [ "$HAVE_B" = 0 ] || [ "$HAVE_D" = 0 ]; then
    printf '%s\n' "$RP|草稿侧内容取不到，无法三方合并" >> "$TMPX"
    continue
  fi
  if git merge-file "$MDIR/$_pre.out" "$MDIR/$_pre.base" "$MDIR/$_pre.trunk" 2>/dev/null; then
    P mergeable "$RP"
    printf '%s\t%s\n' "$RP" "$MDIR/$_pre.out" >> "$TMPM"
  else
    rm -f "$MDIR/$_pre.out"
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

# What is actually left to commit: the draft's file list minus the files trunk
# already holds. Absolute commit targets plus the working-copy dirs that own them
# are built from that, because svn 1.6 keeps every depth-empty checkout as a
# *nested* working copy -- neither `svn status` nor `svn ci` on the root sees what
# lives under it, so all later steps work off these explicit target lists.
: > "$TMPQ"
while IFS="$TAB" read ST RP; do
  [ -n "$RP" ] || continue
  grep -qxF "$RP" "$TMPA" && continue
  printf '%s\t%s\n' "$ST" "$RP" >> "$TMPQ"
done < "$TMPW"
NA=$(wc -l < "$TMPA" | tr -d ' ')
NQ=$(wc -l < "$TMPQ" | tr -d ' ')
[ "$NA" -gt 0 ] && P summary_landed "$NA 个文件 trunk 上已是草稿内容（上一轮已入库），本次跳过"
[ "$NQ" -gt 0 ] || abort "草稿内容与 trunk 已经一致，没有需要提交的东西（本条大概已经推入过了）"

: > "$TMPT"
: > "$TMPP"
while IFS="$TAB" read ST RP; do
  [ -n "$RP" ] || continue
  _dir=$(dirname "$RP")
  if [ "$_dir" = "." ]; then
    printf '%s\n' "$WCPATH" >> "$TMPP"
  else
    printf '%s\n' "$WCPATH/$_dir" >> "$TMPP"
  fi
  printf '%s\n' "$WCPATH/$RP" >> "$TMPT"
done < "$TMPQ"
sort -u "$TMPP" -o "$TMPP"

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
  P ready "$NQ 个文件待写入 $WCPATH 并提交进 $URL"
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
done < "$TMPQ"

# Guard 3: svn must be about to send exactly the planned list. A planned file that
# shows no local change is skipped only when the content sitting in the working
# copy is byte for byte what trunk holds -- after an aligned merge that happens
# whenever the other person's change already covers our hunk. Any other mismatch
# means plan and repository disagree, and it stops the push before a byte lands.
PENDING=$(wc_status | grep -v '^?')
printf '%s\n' "$PENDING" | sed 's|^[^/]*||' | sed '/^$/d' | sort > "$TMPN"
sort "$TMPT" > "$TMPC"
EXTRA=$(comm -23 "$TMPN" "$TMPC" | sed '/^$/d')
if [ -n "$EXTRA" ]; then
  abort "待提交清单里有草稿之外的文件 extra=[$(printf '%s' "$EXTRA" | tr '\n' ';' | cut -c1-300)]" dirty
fi
printf '%s\n' "$PENDING" | sed '/^$/d' | while read L; do P pend "$L"; done
: > "$TMPL"
while IFS="$TAB" read ST RP; do
  [ -n "$RP" ] || continue
  ABS="$WCPATH/$RP"
  if grep -qxF "$ABS" "$TMPN"; then
    printf '%s\n' "$ABS" >> "$TMPL"
    continue
  fi
  SM=$(timeout 90 svn cat --non-interactive "$URL/$RP" 2>/dev/null | md5sum | cut -d' ' -f1)
  LM=$(md5sum < "$ABS" 2>/dev/null | cut -d' ' -f1)
  case "$ST" in
    D*) if [ -z "$SM" ]; then
          P landed "D $RP（trunk 上已经没有这个文件）"
          printf '%s\n' "$RP" >> "$TMPA"
          continue
        fi ;;
    *) if [ -n "$SM" ] && [ "$SM" = "$LM" ]; then
         P landed "$ST $RP（trunk 上已是这个内容，本次不重复提交）"
         printf '%s\n' "$RP" >> "$TMPA"
         continue
       fi ;;
  esac
  abort "待提交清单与草稿不一致：$RP 既不在待提交里，内容也和 trunk 对不上 svn=$SM local=$LM" dirty
done < "$TMPQ"
sort -u "$TMPL" -o "$TMPL"
[ -s "$TMPL" ] || abort "草稿内容与 trunk 已经一致，没有需要提交的东西（本条大概已经推入过了）"
# Final numbers: landed may have grown inside guard 3, and the owner must see the
# list svn is really about to commit, not the list the draft touched.
NA=$(wc -l < "$TMPA" | tr -d ' ')
while IFS="$TAB" read ST RP; do
  [ -n "$RP" ] || continue
  grep -qxF "$WCPATH/$RP" "$TMPL" && P todo "$ST|$RP"
done < "$TMPQ"

MSGF=$(mktemp)
printf '%s' "$MSG_B64" | base64 -d > "$MSGF" 2>/dev/null || abort "提交说明解码失败" dirty
[ -s "$MSGF" ] || abort "提交说明为空" dirty

# The alignment and already-landed facts belong to the review trail, not to the
# log of a trunk commit: svn is about to record what changed in the source, and a
# reader of `svn log` has no idea what a draft branch or an alignment pass is.
# Both are still reported through the markers below so the review page and the
# Zentao comment can carry them.
if [ "$MERGE" = 1 ] && [ -s "$TMPM" ]; then
  P annotated "$(awk -F'\t' '{printf "%s ", $1}' "$TMPM" | sed 's/ $//')"
fi
if [ "$NA" -gt 0 ]; then
  P annotated_landed "$(tr '\n' ' ' < "$TMPA" | sed 's/ $//' | cut -c1-300)"
fi

set --
while read T; do
  [ -n "$T" ] && set -- "$@" "$T"
done < "$TMPL"
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
                merge_trunk: bool = False, base_ref: str = "") -> tuple[int, str]:
    args = [
        target.mirror,
        ref,
        target.wc,
        base64.b64encode(message.encode("utf-8")).decode("ascii"),
        "1" if dry_run else "0",
        "1" if merge_trunk else "0",
        base_ref or "",
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
        elif tag == "already":
            carrier.landed.append(value)
        elif tag == "summary_landed":
            carrier.landed_summary = value
        elif tag == "todo":
            carrier.todo.append(value)
        elif tag == "basefall":
            carrier.base_fall = value
        elif tag == "base_source":
            carrier.base_source = value
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


def require_plan(bug: dict, action: str) -> None:
    """Refuse to write code on a task whose plan the owner has not approved (G13).

    Enforced here as well as in the CLI: this module is the last place that knows
    it is about to change a mirror, so a caller that skipped the CLI still cannot
    land a draft on an unapproved task.
    """
    from . import db

    if str(bug.get("target") or "bug") != "task":
        return
    reason = db.plan_gate_reason(bug)
    if reason:
        raise PromoteError(f"{action} 被拒绝（G13）：{reason}")


def draft_branch_of(bug: dict) -> str:
    """The mirror branch this item's work belongs on.

    A bug is ``bugfix/zentao-<ID>`` (unchanged, so every existing branch keeps
    working) and a task is ``bugfix/zentao-T<ID>``: Zentao numbers the two kinds in
    separate sequences, and both ``41468`` may exist.
    """
    from . import db

    stored = str(bug.get("branch") or "").strip()
    if stored:
        return stored
    return db.draft_branch(bug.get("zentao_id") or bug.get("id"),
                           bug.get("target") or "bug")


def promoted_commit(bug: dict) -> str:
    """The draft commit this bug last really landed in SVN, if the system pushed it.

    Round two of a reopened bug must measure its increment from that commit; the
    mirror's ``origin/trunk`` is normally older than the push itself.
    """
    for rev in bug.get("revisions") or []:
        if str(rev.get("revision") or "").isdigit() and str(rev.get("git_commit") or "").strip():
            return str(rev["git_commit"]).strip()
    return ""


def promote(bug: dict, message: str, *, dry_run: bool = False, merge_trunk: bool = False,
            base_ref: str | None = None) -> Promotion:
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
    if base_ref is None:
        base_ref = promoted_commit(bug)
    carrier.base_ref = str(base_ref or "")
    carrier.exit_code, output = _run_script(target, carrier.ref, text, dry_run,
                                            bool(merge_trunk), carrier.base_ref)
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


@dataclass
class DraftCommit:
    """Result of staging and committing a draft on the mirror."""

    ok: bool = False
    branch: str = ""
    before: str = ""
    tip: str = ""
    subject: str = ""
    staged: list[str] = field(default_factory=list)
    nothing: bool = False
    lock_state: str = ""
    lock_size: int = -1
    lock_age: int = -1
    lock_procs: int = 0
    lock_swept: str = ""
    error: str = ""
    raw: str = ""

    @property
    def short_tip(self) -> str:
        return self.tip[:10]

    def summary(self) -> str:
        if self.error:
            return self.error
        if self.nothing:
            return "暂存区是空的：列出来的文件没有实际改动，什么都没提交"
        swept = f"（顺手回收了上一轮留下的 {self.lock_swept}）" if self.lock_swept else ""
        return f"已在 {self.branch} 提交 {self.short_tip}（{len(self.staged)} 个文件）{swept}"


# ---------------------------------------------------------------------------
# stale .git/index.lock
# ---------------------------------------------------------------------------
# git creates .git/index.lock, streams the new index into it, then renames it
# over .git/index. A run killed in the middle leaves the lock behind and nothing
# ever clears it, so every later bug on that mirror dies on the same wall.
#
# These runs do get killed on their own (the IDE is not stable, a model error
# aborts the task), so waiting for a human to delete one lock per bug is not a
# design -- it is an outage. A writer therefore sweeps a lock only when all
# three conditions hold at once:
#   1. it is exactly 0 bytes  -- a write in flight streams bytes into it, so an
#      empty one was created and abandoned before any content went in;
#   2. no git process at all on this host -- nothing can still own it;
#   3. it has not been touched for LOCK_LIMIT minutes -- a real writer on a
#      96 MB index is continuously busy, it never idles that long.
# Anything else (non-zero size, a live process, a fresh lock) is reported and
# left alone: the residual risk is a writer on another machine going through the
# same share, which no check on this host can see. LOCK_LIMIT=0 (.env
# LOCK_STALE_MINUTES=0) switches the sweep off and leaves only the report.
_LOCK_GATE = r"""
# Counts live git writers on this host. `pgrep -x` never matches its own shell
# pipeline; the `ps -e -o comm=` fallback is safe the same way, because the
# process doing the matching is called `grep`, not `git`. A full-command-line
# grep would count itself and would need a self-exclusion filter that breaks
# down on a host with hundreds of git children -- do not switch to one.
git_procs() {
  if command -v pgrep >/dev/null 2>&1; then
    pgrep -x git 2>/dev/null | grep -c . || true
  else
    ps -e -o comm= 2>/dev/null | grep -c '^git' || true
  fi
}

# Fills LOCK_STATE (absent|stale|hold|busy|odd) / LOCK_SIZE / LOCK_AGE / LOCK_PROCS.
# Order matters: process count comes first. `git status` -- the obvious way to ask
# who holds the lock -- creates index.lock itself to refresh stat info, so probing
# with git would manufacture the very lock we are looking for.
assess_lock() {
  LOCK_STATE=absent; LOCK_SIZE=-1; LOCK_AGE=-1; LOCK_PROCS=0
  LOCK_PATH="$GD/index.lock"
  if [ ! -f "$LOCK_PATH" ]; then
    [ -e "$LOCK_PATH" ] && LOCK_STATE=odd
    return 0
  fi
  LOCK_SIZE=$(wc -c < "$LOCK_PATH" 2>/dev/null | tr -d ' ')
  LOCK_AGE=$(awk -v n="$(date +%s)" -v m="$(stat -c %Y "$LOCK_PATH" 2>/dev/null || echo 0)" \
              'BEGIN{printf "%d", n-m}')
  LOCK_PROCS=$(git_procs)
  if [ "${LOCK_PROCS:-0}" -gt 0 ]; then
    LOCK_STATE=busy
  elif [ "$LOCK_SIZE" = 0 ] && [ "${LOCK_LIMIT:-5}" -gt 0 ] \
       && [ "${LOCK_AGE:-0}" -ge $((LOCK_LIMIT * 60)) ]; then
    LOCK_STATE=stale
  else
    LOCK_STATE=hold
  fi
  return 0
}

report_lock() {
  P lock "$LOCK_STATE|$LOCK_SIZE|$LOCK_AGE|$LOCK_PROCS"
  return 0
}

# One wording for every writer that had to leave a lock alone, so a blocker reads
# the same in checkout and in draft and says what the owner can do about it.
lock_refuse() {
  case "$LOCK_STATE" in
    busy) P err "本机还有 $LOCK_PROCS 个 git 进程活着，index.lock（$LOCK_SIZE 字节）不按残留处理：可能有会话正在写这个镜像，等它结束再试" ;;
    hold) P err "index.lock 还在（$LOCK_SIZE 字节、$LOCK_AGE 秒前），但不满足自动回收条件（要 0 字节且静默 ${LOCK_LIMIT} 分钟）；确认没有别的机器在写这个镜像后 rm -f $LOCK_PATH 即可清掉" ;;
    odd)  P err "index.lock 不是普通文件，形态异常，不自动删: $LOCK_PATH" ;;
    *)    P err "index.lock 未通过陈旧判定，不自动删: $LOCK_PATH（$LOCK_STATE）" ;;
  esac
  return 0
}

git_dir_of() {
  GD=$(git rev-parse --git-dir 2>/dev/null)
  [ -n "$GD" ] || { P err "不是 git 仓库或 git 不可用: $PWD"; return 1; }
  case "$GD" in
    /*) : ;;
    *) GD="$PWD/$GD" ;;
  esac
  return 0
}

# Writer scripts only: removes a provably ownerless leftover and re-reads the state.
sweep_lock() {
  assess_lock
  if [ "$LOCK_STATE" = stale ]; then
    if rm -f "$LOCK_PATH" 2>/dev/null; then
      P swept "index.lock(0 字节、$LOCK_AGE 秒未变动、本机无 git 进程)"
    else
      P err "陈旧 index.lock 删不掉: $LOCK_PATH"
      return 1
    fi
    assess_lock
  fi
  report_lock
  [ "$LOCK_STATE" = stale ] || [ "$LOCK_STATE" = absent ] || return 1
  return 0
}
"""

_DRAFT_COMMIT_SCRIPT = _LOCK_GATE + r"""
# Stage exactly the listed files and commit them on the bug's own draft branch.
# Local git only: no push, no dcommit, no svn. POSIX sh, runs on the build server.
export LC_ALL=C LANG=C
set -f
MIRROR=$1; BR=$2; MSG_B64=$3; FILES_B64=$4; LOCK_LIMIT=${5:-5}
P() { printf 'PV|%s|%s\n' "$1" "$2"; }
cd "$MIRROR" 2>/dev/null || { P err "镜像目录不存在: $MIRROR"; exit 1; }
case "$BR" in
  bugfix/*) : ;;
  *) P err "草稿只允许提交到 bugfix/* 分支，收到: $BR"; exit 1 ;;
esac
if ! git rev-parse --verify "refs/heads/$BR" >/dev/null 2>&1; then
  P err "镜像上没有 $BR：先 python -m app.cli branch <禅道ID> 建分支再改码"
  exit 1
fi
CUR=$(git rev-parse --abbrev-ref HEAD 2>/dev/null)
if [ "$CUR" != "$BR" ]; then
  P err "镜像当前停在 $CUR 而不是 $BR，不替你切分支（切走会动到别人的工作区）"
  exit 1
fi
MSGF=$(mktemp); printf '%s' "$MSG_B64" | base64 -d > "$MSGF" 2>/dev/null || { P err "提交说明解码失败"; exit 1; }
[ -s "$MSGF" ] || { P err "提交说明为空"; exit 1; }
LISTF=$(mktemp); printf '%s' "$FILES_B64" | base64 -d > "$LISTF" 2>/dev/null || { P err "文件清单解码失败"; exit 1; }
[ -s "$LISTF" ] || { P err "文件清单为空：draft 必须显式列出改了哪些文件"; exit 1; }
# `|| [ -n "$F" ]` is not decoration: read returns non-zero on a final line with no
# trailing newline and skips the loop body, so a path list joined by "\n" would lose
# its LAST file silently -- the commit would go through with one file fewer than the
# executor asked for. The caller also appends a newline now; keep both halves fixed.
while read F || [ -n "$F" ]; do
  [ -n "$F" ] || continue
  case "$F" in
    /*)          P err "文件必须是镜像内的相对路径: $F"; exit 1 ;;
    ../*|*/..|*/../*) P err "文件路径不允许上级跳转: $F"; exit 1 ;;
    .git|/*\.git*|.git/*) P err "文件路径不允许指向 .git: $F"; exit 1 ;;
  esac
done < "$LISTF"
P before "$(git rev-parse "refs/heads/$BR")"
set --
while read F || [ -n "$F" ]; do [ -n "$F" ] && set -- "$@" "$F"; done < "$LISTF"
[ $# -gt 0 ] || { P err "文件清单为空"; exit 1; }
git_dir_of || exit 1
sweep_lock || { lock_refuse; exit 1; }
git add -A -- "$@" 2>/dev/null || git add -- "$@" || { P err "git add 失败：$*"; exit 1; }
if git diff --cached --quiet; then
  P nothing "no staged change"
  exit 0
fi
git diff --cached --name-only | while read F; do [ -n "$F" ] && P staged "$F"; done
git commit -q -F "$MSGF" || { P err "git commit 失败"; exit 1; }
P tip "$(git rev-parse "refs/heads/$BR")"
git log -1 --format="PV|subject|%s"
P done "$(git rev-parse --short=10 "refs/heads/$BR")"
"""


def draft_commit(bug: dict, message: str, files: list[str]) -> DraftCommit:
    """Commit the executor's work on the mirror through one allow-listed call.

    The reason this exists is the unattended loop: every raw ``ssh ... git ...``
    the executor types is a command the IDE wants to approve, and one approval
    prompt freezes the whole run. Routing the draft commit through the CLI keeps
    the executor inside ``python -m app.cli``.

    Refuses to run unless the mirror already sits on this bug's branch, because
    switching branches there would move a working tree somebody else may be using.
    """
    branch = draft_branch_of(bug)
    require_plan(bug, "落草稿提交")
    text = str(message or "").strip()
    if not text:
        raise PromoteError("提交说明不能为空（G8：fix #<禅道ID> <一句话根因>）")
    paths = [str(f).strip().replace("\\", "/") for f in (files or [])]
    paths = [f for f in paths if f]
    if not paths:
        raise PromoteError("必须显式给出 --files 文件清单，不许用通配或整仓 add")

    from . import svn_client

    mirror_dir = svn_client.resolve_target(bug=bug).working_copy
    target = remote_target(mirror_dir)
    _require_wiring()
    args = [
        target.mirror, branch,
        base64.b64encode(text.encode("utf-8")).decode("ascii"),
        # Trailing newline included: the remote loop reads lines, and a list whose
        # last path has no newline would drop that path from the commit.
        base64.b64encode(("\n".join(paths) + "\n").encode("utf-8")).decode("ascii"),
        str(lock_limit_minutes()),
    ]
    quoted = " ".join("'" + str(a).replace("'", "'\\''") + "'" for a in args)
    proc = subprocess.run(
        target.ssh_command(f"sh -s {quoted}"),
        input=_DRAFT_COMMIT_SCRIPT.strip().replace("\r\n", "\n").encode("utf-8"),
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        timeout=_SSH_TIMEOUT,
    )
    out_text = _decode(proc.stdout or b"")
    err = _decode(proc.stderr or b"")
    out = DraftCommit(branch=branch)
    for line in out_text.splitlines():
        if not line.startswith("PV|"):
            continue
        parts = line.split("|", 2)
        if len(parts) != 3:
            continue
        tag, value = parts[1], parts[2]
        if tag == "before":
            out.before = value
        elif tag == "staged":
            out.staged.append(value)
        elif tag == "tip":
            out.tip = value
        elif tag == "subject":
            out.subject = value
        elif tag == "nothing":
            out.nothing = True
            out.ok = True
        elif tag == "done":
            out.ok = True
        elif read_lock_marker(out, tag, value):
            pass
        elif tag == "err":
            out.error = value
    if not out.ok and not out.error and proc.returncode != 0 and err.strip():
        out.error = err.strip().splitlines()[-1]
    out.raw = out_text if not err.strip() else out_text + "\n[stderr] " + err.strip()
    if out.error:
        out.error = f"{out.error}（{target.host} · {target.mirror}）"
    return out


# ---------------------------------------------------------------------------
# read-only mirror probe
# ---------------------------------------------------------------------------

@dataclass
class DraftState:
    """What a bug's draft branch really looks like inside the mirror."""

    branch: str = ""
    present: bool = False
    tip: str = ""
    subject: str = ""
    author: str = ""
    when: str = ""
    base: str = ""
    base_source: str = ""
    ahead: int = 0
    commits: list[str] = field(default_factory=list)
    files: list[str] = field(default_factory=list)
    current: str = ""
    trunk_head: str = ""
    index_ok: bool = False
    clean: bool = True
    dirty_real: int = 0
    dirty_noise: int = 0
    dirty_untracked: int = 0
    dirty_paths: str = ""
    dirty_state: str = ""      # ok | failed -- "failed" means we could not read dirt
    dirt_why: str = ""         # git's own words when the read failed
    git_version: str = ""      # the toolchain on that host (2.3 is git 1.7.1)
    lock_state: str = ""
    lock_size: int = -1
    lock_age: int = -1
    lock_procs: int = 0
    error: str = ""
    raw: str = ""

    @property
    def short_tip(self) -> str:
        return self.tip[:10]

    @property
    def ready(self) -> bool:
        """Mirror readiness: trunk ref resolved, HEAD checked out, index populated."""
        return bool(self.trunk_head) and bool(self.current) and self.index_ok

    def summary(self) -> str:
        if self.error:
            return self.error
        if not self.present:
            return f"镜像上没有 {self.branch}（这条还没开工，或者分支已被清掉）"
        return (f"{self.branch} @ {self.short_tip}，比 {self.base_source} 领先 "
                f"{self.ahead} 笔，改了 {len(self.files)} 个文件")


# ---------------------------------------------------------------------------
# worktree dirt, minus this mirror's permanent structural noise
# ---------------------------------------------------------------------------
# AUTO_LOOP.md §1.5/§8 records that these git-svn mirrors carry a fixed baseline
# of dirt that is nobody's edit: ``.trae/`` is a symlink into the owner's
# knowledge root (so every file under it reads as deleted, and its own files read
# as untracked), one vendored path exists on disk as a directory, and the CRLF
# plus ``$Id$`` keyword drift in the php / openvpn / products trees shows up as
# modified content. Measured 2026-09: 92 lines on 3.0.
#
# Only a *real* local edit is ever at stake in a branch switch, so only that may
# stop one. Counting the rest and reporting it is what keeps the guard useful --
# a guard that refuses everything protects nothing and breaks the whole §1.8 chain.
#
# The two mirrors do not run the same toolchain, and this is where that bites:
# 3.0 has git 2.33, 2.3 still has **git 1.7.1**, which understands neither
# `-c <key>` nor `--ignore-cr-at-eol`. A failed probe prints nothing, so a gate
# that reads "empty output" as "clean" tells every bug on 2.3 that the tree is
# fine while git itself refuses every checkout -- 30 tickets ended up blocked that
# way with "判据全绿但建分支失败". So: ask the git on this host what it can do,
# degrade instead of failing, and treat a failed probe as UNKNOWN, which refuses.
_DIRTY_GATE = r"""
is_mirror_noise() {
  case "$1" in
    .trae|.trae/*) return 0 ;;
    openvpn-2.4.8/INSTALL) return 0 ;;
  esac
  return 1
}

# Fills GITE (a git invocation this host accepts), GITCR (how to compare ignoring
# line-ending noise), GITV. Nothing here may assume a modern git.
probe_git() {
  GITV=$(git --version 2>/dev/null | sed 's/[^0-9]*\([0-9][0-9.]*\).*/\1/')
  GITE="git"
  if git -c core.ignorecase=false rev-parse --git-dir >/dev/null 2>&1; then
    GITE="git -c core.ignorecase=false"     # Linux is case-sensitive anyway; this only
  fi                                        # guards the rehearsal run under Windows sh
  GITCR="--ignore-cr-at-eol"
  # rc 0 = no diff, rc 1 = differences, rc > 1 = the option itself is unknown.
  git diff --ignore-cr-at-eol --quiet HEAD >/dev/null 2>&1
  [ $? -le 1 ] || GITCR=""                   # git 1.7.1: strip CR in the comparison instead
  return 0
}

# Somebody's edit is what is left after dropping CR at line ends and the $Id$
# keyword lines. git diff exits 0 (identical) / 1 (differs) / >1 (error), and an
# error is never read as "clean" -- it is reported as an edit so the switch fails.
path_is_edited() {
  _p=$1
  _tf="/tmp/dirtgate.$$.diff"
  _ef="/tmp/dirtgate.$$.err"
  if [ -n "$GITCR" ]; then
    $GITE diff $GITCR -U0 -- "$_p" >"$_tf" 2>"$_ef"
  else
    $GITE diff -U0 -- "$_p" >"$_tf" 2>"$_ef"
  fi
  _rc=$?
  if [ $_rc -gt 1 ]; then
    rm -f "$_tf" "$_ef"
    return 0
  fi
  _res=$(sed -e 's/\r$//' "$_tf" 2>/dev/null | grep '^[+-]' | grep -v '^+++' \
         | grep -v '^---' | grep -v '\$Id' | sed -n '1,20p')
  rm -f "$_tf" "$_ef"
  [ -n "$_res" ]
}

# Fills DIRT_STATE (ok|failed) / DIRT_WHY / REAL / NOISE / UNTRACKED / REAL_LIST.
# The loop reads from a here-document on purpose: a pipe would run it in a
# subshell and the counters would be lost.
assess_dirt() {
  REAL=0
  NOISE=0
  UNTRACKED=0
  REAL_LIST=""
  DIRT_STATE=failed
  DIRT_WHY=""
  probe_git
  P gitv "$GITV"
  PERR=$(mktemp 2>/dev/null || echo /tmp/dirtstatus.$$.err)
  DIRTY=$($GITE status --porcelain 2>"$PERR")
  SRC=$?
  if [ $SRC -ne 0 ]; then
    DIRT_WHY=$(tr '\n' ' ' < "$PERR" 2>/dev/null | cut -c1-200)
    rm -f "$PERR"
    return 0
  fi
  rm -f "$PERR"
  DIRT_STATE=ok
  while IFS= read -r line; do
    [ -n "$line" ] || continue
    xy=$(printf '%s' "$line" | cut -c1-2)
    path=$(printf '%s' "$line" | cut -c4-)
    case "$path" in *' -> '*) path=${path##* -> } ;; esac
    case "$xy" in '??') UNTRACKED=$((UNTRACKED + 1)); continue ;; esac
    if is_mirror_noise "$path"; then
      NOISE=$((NOISE + 1))
      continue
    fi
    idx=$(printf '%s' "$xy" | cut -c1)
    wt=$(printf '%s' "$xy" | cut -c2)
    real=0
    case "$idx" in 'M'|'A'|'R'|'C'|'U') real=1 ;; esac
    if [ "$real" = 0 ]; then
      case "$wt" in
        'M') path_is_edited "$path" && real=1 ;;
        'D'|'U'|'T') real=1 ;;
        *) [ "$wt" != ' ' ] && real=1 ;;
      esac
    fi
    if [ "$real" = 1 ]; then
      REAL=$((REAL + 1))
      [ $REAL -le 5 ] && REAL_LIST="$REAL_LIST $path"
    else
      # Nothing at stake: either a listed noise path or a diff that evaporates once
      # CR at line ends and the $Id$ keywords are ignored. Counted so REAL + NOISE +
      # UNTRACKED always equals the porcelain line count the owner can reproduce.
      NOISE=$((NOISE + 1))
    fi
  done <<EOF
$DIRTY
EOF
  return 0
}

report_dirt() {
  P dirt "$REAL|$NOISE|$UNTRACKED|$DIRT_STATE"
  [ -n "$REAL_LIST" ] && P dirt_paths "$(printf '%s' "$REAL_LIST" | cut -c1-300)"
  [ -n "$DIRT_WHY" ] && P dirt_why "$DIRT_WHY"
  return 0
}
"""

_DRAFT_SCRIPT = _DIRTY_GATE + _LOCK_GATE + r"""
# Report one draft branch read-only: tip, commits, touched files, and how far it
# sits ahead of trunk. POSIX sh, runs on the build server. Nothing here writes:
# no fetch, no checkout, no ref updates, and a leftover index.lock is only
# reported -- clearing it is a writer's decision, made in checkout / draft.
export LC_ALL=C LANG=C
set -f
MIRROR=$1; BR=$2; BASE_REF=$3; LOCK_LIMIT=${4:-5}
P() { printf 'PV|%s|%s\n' "$1" "$2"; }
cd "$MIRROR" 2>/dev/null || { P err "镜像目录不存在: $MIRROR"; exit 0; }
case "$BR" in
  bugfix/*) : ;;
  *) P err "只探测 bugfix/* 草稿分支，收到: $BR"; exit 0 ;;
esac
P current "$(git rev-parse --abbrev-ref HEAD 2>/dev/null)"
P trunk_head "$(git rev-parse --short=10 refs/remotes/origin/trunk 2>/dev/null)"
if git ls-files --error-unmatch AGENTS.md >/dev/null 2>&1; then P index ok; else P index missing; fi
assess_dirt
if [ "$DIRT_STATE" != ok ]; then P clean unknown
elif [ "$REAL" = 0 ]; then P clean ok
else P clean dirty
fi
report_dirt
git_dir_of || exit 0
assess_lock
report_lock
if ! git rev-parse --verify "refs/heads/$BR" >/dev/null 2>&1; then
  P absent "$BR"
  exit 0
fi
P present "$BR"
P tip "$(git rev-parse "refs/heads/$BR")"
git log -1 --format="PV|subject|%s" "$BR"
git log -1 --format="PV|author|%an" "$BR"
git log -1 --format="PV|when|%ci" "$BR"
BASE=""
if [ -n "$BASE_REF" ] && git rev-parse --verify "$BASE_REF^{commit}" >/dev/null 2>&1; then
  BASE=$(git merge-base "$BASE_REF" "$BR" 2>/dev/null)
  P base_source "$BASE_REF"
else
  BASE=$(git merge-base refs/remotes/origin/trunk "$BR" 2>/dev/null)
  P base_source "origin/trunk"
fi
if [ -z "$BASE" ]; then
  P err "草稿分支与 trunk 没有公共祖先，无法算增量"
  exit 0
fi
P base "$(git rev-parse --short=10 "$BASE")"
P ahead "$(git rev-list --count "$BASE..$BR")"
for H in $(git rev-list --max-count=20 "$BASE..$BR"); do
  git log -1 --format="PV|commit|%h %ci %s" --abbrev=10 "$H"
  git show --name-only --format= "$H" | while read F; do
    [ -n "$F" ] && P file "M $F"
  done
done
"""


def draft_state(bug: dict, base_ref: str = "") -> DraftState:
    """Inspect one bug's draft branch on the mirror without changing anything.

    This exists so the executor never has to type a raw ``ssh ... git ...``
    command: every mirror question it needs answered goes through a
    ``python -m app.cli`` call, which keeps the unattended run inside the set of
    commands that need no approval.
    """
    branch = draft_branch_of(bug)
    from . import svn_client

    mirror_dir = svn_client.resolve_target(bug=bug).working_copy
    target = remote_target(mirror_dir)
    _require_wiring()
    args = [target.mirror, branch, base_ref or promoted_commit(bug), str(lock_limit_minutes())]
    quoted = " ".join("'" + str(a).replace("'", "'\\''") + "'" for a in args)
    proc = subprocess.run(
        target.ssh_command(f"sh -s {quoted}"),
        input=_DRAFT_SCRIPT.strip().replace("\r\n", "\n").encode("utf-8"),
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        timeout=_SSH_TIMEOUT,
    )
    text = _decode(proc.stdout or b"")
    err = _decode(proc.stderr or b"")
    out = DraftState(branch=branch)
    for line in text.splitlines():
        if not line.startswith("PV|"):
            continue
        parts = line.split("|", 2)
        if len(parts) != 3:
            continue
        tag, value = parts[1], parts[2]
        if tag == "present":
            out.present = True
        elif tag == "tip":
            out.tip = value
        elif tag == "subject":
            out.subject = value
        elif tag == "author":
            out.author = value
        elif tag == "when":
            out.when = value
        elif tag == "base":
            out.base = value
        elif tag == "base_source":
            out.base_source = value
        elif tag == "ahead":
            out.ahead = int(value or 0)
        elif tag == "commit":
            out.commits.append(value)
        elif tag == "file":
            if value not in out.files:
                out.files.append(value)
        elif tag == "current":
            out.current = value
        elif tag == "trunk_head":
            out.trunk_head = value
        elif tag == "index":
            out.index_ok = value == "ok"
        elif tag == "clean":
            out.clean = value == "ok"
        elif read_dirt_marker(out, tag, value):
            pass
        elif read_lock_marker(out, tag, value):
            pass
        elif tag == "err":
            out.error = value

    # A login banner on stderr is normal here; only a script that said nothing and
    # failed counts as an error.
    if not out.present and not out.error and proc.returncode != 0 and err.strip():
        out.error = err.strip().splitlines()[-1]
    out.raw = text if not err.strip() else text + "\n[stderr] " + err.strip()
    if out.error:
        out.error = f"{out.error}（{target.host} · {target.mirror}）"
    return out


@dataclass
class Checkout:
    """Result of putting the mirror onto one bug's draft branch."""

    ok: bool = False
    branch: str = ""
    action: str = ""          # created | switched | already
    base: str = ""
    tip: str = ""
    dirty_real: int = 0
    dirty_noise: int = 0
    dirty_untracked: int = 0
    dirty_paths: str = ""
    dirty_state: str = ""
    dirt_why: str = ""
    git_version: str = ""
    lock_state: str = ""
    lock_size: int = -1
    lock_age: int = -1
    lock_procs: int = 0
    lock_swept: str = ""
    error: str = ""
    raw: str = ""

    def summary(self) -> str:
        if self.error:
            return self.error
        words = {"created": "已从", "switched": "已切到", "already": "本来就停在"}
        swept = ""
        if self.lock_swept:
            swept += f"（回收了上一轮留下的 {self.lock_swept}）"
        if self.dirty_noise or self.dirty_untracked:
            swept += (f"（已忽略本镜像的结构性噪音：{self.dirty_noise} 条 .trae/INSTALL/CRLF/"
                      f"$Id$ 类 + {self.dirty_untracked} 条未跟踪）")
        if self.action == "created":
            return f"已创建并切到 {self.branch}（基线 {self.base}），当前 {self.tip}{swept}"
        return f"{words.get(self.action, '已切到')} {self.branch}，当前 {self.tip}{swept}"


_CHECKOUT_SCRIPT = _DIRTY_GATE + _LOCK_GATE + r"""
# Put the mirror onto one bug's draft branch, creating it from trunk when needed.
# Refuses to move a working tree that holds somebody's real edit -- that edit may
# belong to another session. The mirror's permanent noise is counted, not refused
# (see _DIRTY_GATE), otherwise this guard would block every bug forever.
# POSIX sh, runs on the build server. Local git only -- no fetch, no push, no svn.
export LC_ALL=C LANG=C
set -f
MIRROR=$1; BR=$2; BASE=$3; LOCK_LIMIT=${4:-5}
P() { printf 'PV|%s|%s\n' "$1" "$2"; }
cd "$MIRROR" 2>/dev/null || { P err "镜像目录不存在: $MIRROR"; exit 1; }
case "$BR" in
  bugfix/*) : ;;
  *) P err "只允许切到 bugfix/* 草稿分支，收到: $BR"; exit 1 ;;
esac
if ! git rev-parse --verify refs/remotes/origin/trunk >/dev/null 2>&1; then
  P err "镜像缺 refs/remotes/origin/trunk（未就绪），不在这上面开工"; exit 1
fi
if ! git ls-files --error-unmatch AGENTS.md >/dev/null 2>&1; then
  P err "镜像索引没落盘（ls-files 找不到 AGENTS.md，未就绪），不在这上面开工"; exit 1
fi
git_dir_of || exit 1
assess_lock
report_lock
CUR=$(git rev-parse --abbrev-ref HEAD 2>/dev/null)
if [ "$CUR" = "$BR" ]; then
  P action already
  P tip "$(git rev-parse --short=10 HEAD)"
  P done ok
  exit 0
fi
# Clear a provably dead leftover before measuring dirt: while the lock sits there
# git cannot refresh the index, so a status taken first reads misleadingly.
if [ "$LOCK_STATE" != absent ]; then
  sweep_lock || { lock_refuse; exit 1; }
fi
assess_dirt
report_dirt
if [ "$DIRT_STATE" != ok ]; then
  # An unreadable worktree is never read as a clean one. On 2.3 this is what turns
  # "判据全绿但建分支失败" into a message that names the real cause.
  P err "读不到工作区状态（git ${GITV:-?}）：${DIRT_WHY:-git status 失败}；判据未知时不建分支，免得把别人的改动带进 $BR"
  exit 1
fi
if [ "$REAL" -gt 0 ]; then
  P err "镜像工作区在 $CUR 上有 $REAL 处真实未提交改动（$REAL_LIST），不替你 stash / checkout：先让那条会话收口"
  exit 1
fi
# git refuses a switch for its own reasons, and only git words them ("error: You
# have local changes to 'x'" on 1.7.1). Swallowing stderr is what left 30 tickets
# blocked with "建分支失败" and nothing to act on, so the failure text is the error.
run_git_checkout() {
  _label=$1
  shift
  _out=$(git checkout "$@" 2>&1)
  if [ $? -ne 0 ]; then
    P err "$_label: $(printf '%s' "$_out" | tr '\n' ' ' | cut -c1-300)"
    return 1
  fi
  return 0
}
if git rev-parse --verify "refs/heads/$BR" >/dev/null 2>&1; then
  run_git_checkout "切到已有分支 $BR 失败" -q "$BR" || exit 1
  P action switched
else
  FROM="$BASE"
  [ -n "$FROM" ] || FROM=refs/remotes/origin/trunk
  if ! git rev-parse --verify "$FROM^{commit}" >/dev/null 2>&1; then
    P err "基线不可用: $FROM"; exit 1
  fi
  run_git_checkout "从 $FROM 建分支 $BR 失败" -q -b "$BR" "$FROM" || exit 1
  P action created
  P base "$FROM"
fi
P tip "$(git rev-parse --short=10 HEAD)"
P done ok
"""

# The remote scripts are assembled by concatenating gate snippets, and
# concatenation is where they break: _CHECKOUT_SCRIPT once called git_dir_of /
# sweep_lock with no _LOCK_GATE in front of it, every checkout died with
# "command not found", and that reads like a broken mirror -- so each bug got
# blocked instead of anyone noticing the typo. Checking the strings costs nothing
# and does not need to reach the build server, so it runs before any of them ships.
_GATE_HELPERS = ("is_mirror_noise", "probe_git", "path_is_edited", "assess_dirt",
                 "report_dirt", "git_dir_of", "assess_lock", "report_lock",
                 "sweep_lock", "lock_refuse", "run_git_checkout")


def script_wiring_issues() -> list[str]:
    """Return helpers a generated script calls but does not define (empty = sane)."""
    problems: list[str] = []
    for name, body in (("_DRAFT_SCRIPT", _DRAFT_SCRIPT),
                       ("_DRAFT_COMMIT_SCRIPT", _DRAFT_COMMIT_SCRIPT),
                       ("_CHECKOUT_SCRIPT", _CHECKOUT_SCRIPT)):
        code = "\n".join(ln for ln in body.splitlines() if not ln.lstrip().startswith("#"))
        defined = set(re.findall(r"^([a-z_][a-z0-9_]*)\(\)", code, re.M))
        for helper in _GATE_HELPERS:
            if re.search(r"(?<![\w])" + helper + r"\s", code) and helper not in defined:
                problems.append(f"{name} 调用 {helper}()，但脚本体内没有定义（缺 _DIRTY_GATE / _LOCK_GATE？）")
    return problems


def _require_wiring() -> None:
    """Refuse to ship a script that would only fail after it reached the server."""
    issues = script_wiring_issues()
    if issues:
        raise PromoteError("远端脚本不自洽，已拒绝下发：" + "；".join(issues))


def checkout_draft(bug: dict, base: str = "") -> Checkout:
    """Create or switch the mirror's working tree onto this bug's draft branch.

    Part of the same goal as ``draft_commit``: the executor should be able to do
    its whole draft lifecycle through ``python -m app.cli`` instead of typing raw
    ``ssh ... git checkout``, which is the kind of command that stops an
    unattended run dead on an approval prompt.
    """
    branch = draft_branch_of(bug)
    require_plan(bug, "开/切草稿分支")
    from . import svn_client

    mirror_dir = svn_client.resolve_target(bug=bug).working_copy
    target = remote_target(mirror_dir)
    _require_wiring()
    args = [target.mirror, branch, base or "", str(lock_limit_minutes())]
    quoted = " ".join("'" + str(a).replace("'", "'\\''") + "'" for a in args)
    proc = subprocess.run(
        target.ssh_command(f"sh -s {quoted}"),
        input=_CHECKOUT_SCRIPT.strip().replace("\r\n", "\n").encode("utf-8"),
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        timeout=_SSH_TIMEOUT,
    )
    text = _decode(proc.stdout or b"")
    err = _decode(proc.stderr or b"")
    out = Checkout(branch=branch)
    for line in text.splitlines():
        if not line.startswith("PV|"):
            continue
        parts = line.split("|", 2)
        if len(parts) != 3:
            continue
        tag, value = parts[1], parts[2]
        if tag == "action":
            out.action = value
        elif tag == "base":
            out.base = value
        elif tag == "tip":
            out.tip = value
        elif read_dirt_marker(out, tag, value):
            pass
        elif read_lock_marker(out, tag, value):
            pass
        elif tag == "done":
            out.ok = True
        elif tag == "err":
            out.error = value
    if not out.ok and not out.error and proc.returncode != 0 and err.strip():
        out.error = err.strip().splitlines()[-1]
    out.raw = text if not err.strip() else text + "\n[stderr] " + err.strip()
    if out.error:
        out.error = f"{out.error}（{target.host} · {target.mirror}）"
    return out
