"""The `svn_direct` code channel (AUTO_LOOP.md §1.9).

This is the second landing spot for a fix. Where the default flow writes code
into a git-svn mirror and keeps the draft as a local commit, this mode edits a
plain SVN working copy in place. SVN has no local commit, so:

* the "draft" of an item is its set of uncommitted changes in that working copy;
* the working copy itself is the exclusive resource -- one live draft per
  directory, enforced by `db.svn_drafts` and its partial unique index;
* committing to the repository is never an executor action. The owner commits;
  `push()` exists only for the case where the owner explicitly asks the AI to
  run it, and it refuses without that acknowledgement.

Every `svn` invocation runs on the build server over SSH -- never on the Windows
mapping of the same directory. Three reasons, all measured before this module
was written: the working copy is 1.6 format with `text-base` pristine copies, so
reading them across SMB is slow; `svn` on the server is 1.6.23 while the local
client is 1.6.16; and the code line has 109 pairs of paths that differ only in
case, which a case-insensitive client reads as one file. The path conversion
Windows -> Linux comes from that code line's `CLAUDE.local.yaml` and nowhere
else; when it cannot be derived, this module refuses instead of guessing.
"""

from __future__ import annotations

import re
import subprocess
import xml.etree.ElementTree as ET
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from . import db
from .config import get_settings
from .db import item_slug
from .svn_client import RepoTarget
from .svn_promote import RemoteTarget, _yaml_block

# svn statuses that mean "a tracked file was changed by somebody". Anything
# outside the current item's declared set blocks the claim: SVN has no branch to
# separate two items' edits, so the working copy may hold exactly one author's
# real changes at a time.
REAL_ITEMS = {"modified", "added", "deleted", "replaced", "conflicted"}
# Everything below is either unversioned, an external, or a file SVN cannot see
# on disk. None of it is an uncommitted edit by a person, so it never blocks.
SOFT_ITEMS = {"unversioned", "ignored", "external", "missing", "incomplete", "obstructed"}


class DirectError(RuntimeError):
    """Anything that must stop a direct-mode command with a readable reason."""


@dataclass
class DirectWc:
    """One real SVN working copy plus how to reach it."""

    product_id: Any
    product_name: str
    local_path: str          # as stored in product_repos.svn_working_copy
    remote_path: str         # the same directory as seen by the build server
    url: str = ""            # svn URL the copy points at
    revision: str = ""       # revision the copy is updated to
    svn_version: str = ""
    remote: RemoteTarget | None = None
    source: str = "product"

    def label(self) -> str:
        return f"{self.product_name or self.product_id} @ {self.local_path}"


@dataclass
class Dirt:
    """The measured state of a working copy, split the way the gates need it."""

    entries: dict[str, dict[str, str]] = field(default_factory=dict)
    real: list[str] = field(default_factory=list)
    soft: list[str] = field(default_factory=list)
    conflicts: list[str] = field(default_factory=list)
    stale: list[str] = field(default_factory=list)   # repos has newer content
    state: str = "ok"                                 # ok | failed
    why: str = ""

    def counts(self) -> dict[str, int]:
        return {"real": len(self.real), "soft": len(self.soft),
                "conflicts": len(self.conflicts), "stale": len(self.stale)}


# ---------------------------------------------------------------------------
# remote execution
# ---------------------------------------------------------------------------

def _q(text: Any) -> str:
    """POSIX single-quote escaping: nothing inside may reach the remote shell."""
    return "'" + str(text).replace("'", "'\"'\"'") + "'"


def _ssh(rt: RemoteTarget, command: str, timeout: int = 600) -> tuple[int, str, str]:
    proc = subprocess.run(rt.ssh_command(command), capture_output=True, timeout=timeout + 30)
    out = _decode(proc.stdout)
    err = _decode(proc.stderr)
    return proc.returncode, out, err


def _decode(raw: bytes) -> str:
    if not raw:
        return ""
    for encoding in ("utf-8", "gbk"):
        try:
            return raw.decode(encoding)
        except UnicodeDecodeError:
            continue
    return raw.decode("utf-8", errors="replace")


def svn_run(wc: DirectWc, args: list[str], *, timeout: int = 300,
             check: bool = True) -> tuple[int, str, str]:
    """Run one `svn` command inside the working copy, on the build server."""
    if wc.remote is None:
        raise DirectError("该工作副本没有可用的 SSH 端点")
    inner = " ".join(["svn", "--non-interactive"] + [_q(a) for a in args])
    remote = f"cd {_q(wc.remote_path)} && timeout {int(timeout)} {inner}"
    code, out, err = _ssh(wc.remote, remote, timeout=timeout)
    if code != 0 and check:
        raise DirectError(f"svn {' '.join(args[:2])} 失败 (exit {code}): "
                          f"{(err or out).strip()[:600]}")
    return code, out, err


def sh_run(wc: DirectWc, script: str, args: list[str], *, timeout: int = 120) -> tuple[int, str, str]:
    """Run a small shell snippet inside the working copy with positional args."""
    if wc.remote is None:
        raise DirectError("该工作副本没有可用的 SSH 端点")
    inner = f"sh -c {_q(script)} sh " + " ".join(_q(a) for a in args)
    remote = f"cd {_q(wc.remote_path)} && timeout {int(timeout)} {inner}"
    return _ssh(wc.remote, remote, timeout=timeout)


# ---------------------------------------------------------------------------
# path / endpoint resolution
# ---------------------------------------------------------------------------

def _read_yaml(path: Path) -> str:
    try:
        return path.read_text(encoding="utf-8", errors="replace")
    except OSError as exc:
        raise DirectError(f"读取 {path} 失败：{exc}") from exc


def _prefix_pair(local_root: str, remote_root: str) -> tuple[str, str]:
    """Longest shared trailing path -> the prefix pair that maps one onto the other.

    `CLAUDE.local.yaml` states one anchor pair (workspace root on Windows and on
    Linux). The SVN copy usually sits *beside* the mirror, not inside it, so the
    usable mapping is whatever the two anchors still share after the differing
    tail is dropped: `U:\\hy-10G-2\\trunk\\git.3.0` and
    `/home/<user>/hy-10G-2/trunk/git.3.0` share three elements and therefore map
    `U:\\hy-10G-2` to `/home/<user>/hy-10G-2`.
    """
    loc = [p for p in str(local_root).replace("\\", "/").rstrip("/").split("/") if p and p != "."]
    rem = [p for p in str(remote_root).rstrip("/").split("/") if p and p != "."]
    # A drive letter ("U:") is a root, not a directory name: never match on it.
    common = 0
    while (common < len(loc) - 1 and common < len(rem)
           and loc[-1 - common].lower() == rem[-1 - common].lower()):
        common += 1
    if common == 0:
        return "", ""
    local_prefix = "/".join(loc[:len(loc) - common])
    remote_prefix = "/" + "/".join(rem[:len(rem) - common])
    if not local_prefix:
        return "", ""
    return local_prefix.rstrip("/"), remote_prefix.rstrip("/") or "/"


def _find_yaml(starts: list[Path], *, hint: str = "") -> tuple[Path, dict[str, str], dict[str, str]]:
    """Find the CLAUDE.local.yaml that declares the SSH endpoint for this copy.

    The copy itself usually has none -- it is a sibling of the mirror, not
    inside it -- so the search goes: the copy upward, then the mirror upward.
    Whatever is found is the single authority for host, user and the path
    mapping; nothing here has a built-in default.
    """
    for start in starts:
        node = start
        for _ in range(8):
            if not str(node):
                break
            cfg = node / "CLAUDE.local.yaml"
            if cfg.is_file():
                text = _read_yaml(cfg)
                ssh = _yaml_block(text, "ssh")
                paths = _yaml_block(text, "path_aliases")
                if ssh.get("host") and ssh.get("user"):
                    return cfg, ssh, paths
            parent = node.parent
            if parent == node:
                break
            node = parent
    raise DirectError(
        "找不到可用的 CLAUDE.local.yaml（需要 ssh.host / ssh.user）：查找起点 "
        + " -> ".join(str(s) for s in starts)
        + "。SSH 侧执行 svn 命令要求该代码库自己声明主机与用户，本系统不猜。" + hint)


# ---------------------------------------------------------------------------
# Which copy an item may land in (AUTO_LOOP.md §1.9)
# ---------------------------------------------------------------------------

LANDING_LABELS = {
    "draft": "本条在册草稿（副本已领用，中途不换地方）",
    "item": "条目点名（landing_wc）",
    "mode": "条目声明走副本（landing_mode），路径自动判定",
    "binding": "产品绑定（product_repos.svn_working_copy）",
    "auto": "自动判定：镜像同级、仓库根 URL 与该产品一致的那份检出",
}


def direct_preview(item: dict | None, binding: dict | None = None) -> tuple[str, str]:
    """``(copy, why)`` a direct round *would* use for this item, deciding nothing.

    The card needs to show the target before the owner clicks, and the click itself
    must not require typing a path -- so the same discovery the commands will use
    runs here, read-only, and says out loud which copy was chosen and why.
    """
    from . import landing_scan

    if binding is None:
        pid = (item or {}).get("product_id")
        binding = (db.get_product_repo(pid) if pid else None) or {}
    wc = str((item or {}).get("landing_wc") or "").strip()
    if wc:
        return wc, "这一条已经点名了副本"
    wc = str(binding.get("svn_working_copy") or "").strip()
    if wc:
        return wc, "产品绑定里配置的副本"
    found = landing_scan.discover_binding(binding)
    if not found:
        return "", ""
    return found[0]["path"], landing_scan.pick_reason(found[0], found)


def default_direct_wc() -> str:
    """The owner-side default copy from .env -- a pre-fill, and only if it *is* one.

    ``SVN_WORKING_COPY`` already defaults to ``<project>/svn_workspace``, which is
    this system's own promote scratch directory, not a checkout of anything; a
    copied-down ``SVN_DIRECT_WC=`` therefore points at a directory SVN knows nothing
    about. Reading the local manifest decides that instead of trusting the setting:
    anything that is not an SVN 1.6/1.7 working copy is reported as "no default".
    """
    from . import landing_scan

    raw = str(get_settings().svn_direct_wc or "").strip()
    if not raw:
        return ""
    if (Path(raw) / ".svn").is_dir() or landing_scan.read_manifest(raw):
        return raw
    return ""


def _draft_copy(item: dict | None) -> str:
    """The copy this item already holds, if any -- a round never changes location."""
    if not item or item.get("id") is None:
        return ""
    draft = db.get_live_draft_for_item(item["id"], item.get("target") or "bug")
    return str((draft or {}).get("wc_path") or "").strip()


def _auto_copy(binding: dict) -> tuple[str, str]:
    """Discover the landing copy beside the mirror; ('', 'auto') when undecidable."""
    from . import landing_scan

    found = landing_scan.discover_binding(binding)
    return (found[0]["path"], "auto") if found else ("", "auto")


def landing_for(item: dict | None, binding: dict | None = None) -> tuple[str, str, str]:
    """``(code_mode, wc_local, source)`` -- where this item's next edit may go.

    The decision and the path are resolved on two separate chains:

      decision: 在册草稿 > 条目 landing_mode > 产品绑定 code_mode（或条目点了名）
      path:     草稿的 wc > 条目 landing_wc > 绑定的 svn_working_copy > 自动判定

    Splitting them is what lets a direct round name nothing: the owner decides
    "this one runs in place", and the copy is discovered. Nothing about discovery
    can make an item direct on its own -- a directory merely sitting beside a
    mirror must not switch a whole product's flow.
    """
    if binding is None:
        pid = (item or {}).get("product_id")
        binding = (db.get_product_repo(pid) if pid else None) or {}
    held = _draft_copy(item)
    if held:
        return "svn_direct", held, "draft"
    item_wc = str((item or {}).get("landing_wc") or "").strip()
    declared = str((item or {}).get("landing_mode") or "").strip()
    bound = str(binding.get("code_mode") or "git_mirror")
    if declared == "git_mirror":
        # An explicit "this one stays on the mirror" outranks a bound-direct product.
        return "git_mirror", "", "mode"
    # A stored path is never, by itself, the decision -- otherwise a leftover
    # landing_wc would keep an item in the copy after its mode was cleared.
    if declared == "svn_direct" or bound == "svn_direct":
        source = "mode" if declared == "svn_direct" else "binding"
        wc = item_wc or str(binding.get("svn_working_copy") or "").strip()
        if item_wc:
            source = "item"
        elif not wc:
            wc, source = _auto_copy(binding)
        return "svn_direct", wc, source
    return "git_mirror", "", "binding"


def landing_reason(item: dict | None, binding: dict | None = None) -> str:
    """One line explaining an *automatic* choice (the other sources need no essay)."""
    mode, wc, source = landing_for(item, binding)
    if source != "auto" or not wc:
        return ""
    from . import landing_scan

    if binding is None:
        pid = (item or {}).get("product_id")
        binding = (db.get_product_repo(pid) if pid else None) or {}
    found = landing_scan.discover_binding(binding)
    chosen = next((c for c in found if c["path"] == wc), None)
    return landing_scan.pick_reason(chosen, found) if chosen else ""


def resolve(target: RepoTarget, *, binding: dict | None = None,
            item: dict | None = None) -> DirectWc:
    """Turn a product binding into a verified working copy on the build server.

    Verified means: the directory is configured, it really is an SVN working
    copy, and what it points at belongs to the repository bound to that product.
    A copy of a different repository is refused -- an executor editing the wrong
    line's code is worse than a failed command.

    ``item`` may carry a per-item landing override, which outranks the binding;
    everything else about resolution is unchanged, so an override is still held to
    the same URL-ownership proof as a bound copy.
    """
    if binding is None:
        binding = (db.get_product_repo(target.product_id) if target.product_id else None) or {}
    _mode, wc_local, source = landing_for(item, binding)
    if not wc_local:
        raise DirectError(
            f"没有可落码的 SVN 工作副本（本轮模式来源：{LANDING_LABELS.get(source, source)}）。"
            f"三选一：给这一条点名副本（页面「SVN 副本下达语」按钮，或 "
            f"python -m app.cli landing <条目号> --wc <路径>）；或把产品绑成 direct：python -m app.cli bind-repo "
            f"{target.product_id or '<产品ID>'} {target.repo_url or '<SVN仓库地址>'} "
            f"--code-mode svn_direct --svn-working-copy <SVN工作副本>；"
            f"或在 .env 配 SVN_DIRECT_WC=<路径> 后由按钮写进本条")
    local = Path(wc_local)
    starts = [local, Path(str(target.working_copy))] if target.working_copy else [local]
    cfg_path, ssh, paths = _find_yaml(
        starts,
        hint="；若该副本不在镜像同级目录下，请同时在该绑定里填 svn_remote_copy（Linux 路径）")
    remote = str(binding.get("svn_remote_copy") or "").strip()
    if not remote:
        local_root = paths.get("LOCAL_WORKSPACE_ROOT", "")
        remote_root = ssh.get("remote_workspace", "") or paths.get("LOCAL_WORKSPACE_ROOT", "")
        lp, rp = _prefix_pair(local_root, remote_root)
        if not lp or not rp:
            raise DirectError(
                f"无法从 {cfg_path} 推出 Windows→Linux 路径映射"
                f"（path_aliases.LOCAL_WORKSPACE_ROOT={local_root or '(空)'}, "
                f"ssh.remote_workspace={remote_root or '(空)'}）。"
                f"请显式填写：python -m app.cli bind-repo {target.product_id} "
                f"{target.repo_url} --svn-remote-copy <该副本的 Linux 路径>")
        norm = str(local).replace("\\", "/")
        low_lp, low_norm = lp.lower(), norm.lower()
        if not low_norm.startswith(low_lp + "/") and low_norm != low_lp:
            raise DirectError(
                f"工作副本 {norm} 不在映射根 {lp} 之下，换不出 Linux 路径；"
                f"请显式填写 --svn-remote-copy")
        remote = rp + norm[len(lp):]
    port_raw = ssh.get("port", "22")
    identity = ssh.get("identity_file", "")
    rsa_only = ssh.get("rsa_only", "").lower() in ("1", "true", "yes") or bool(identity)
    rt = RemoteTarget(
        host=ssh["host"], user=ssh["user"],
        port=int(port_raw) if str(port_raw).isdigit() else 22,
        mirror=remote.rstrip("/"), identity_file=identity, rsa_only=rsa_only,
        promote_root="",
    )
    wc = DirectWc(product_id=target.product_id, product_name=target.product_name,
                  local_path=wc_local, remote_path=remote.rstrip("/"), remote=rt)
    _verify(wc, target)
    return wc


def _verify(wc: DirectWc, target: RepoTarget) -> None:
    """`svn info --depth empty` on the copy: must be a working copy of *this* repository.

    The `--depth empty` is not a micro-optimisation: on svn 1.6 a bare `svn info .`
    recurses over the whole tree, and on this line that means minutes (measured: a
    330 s timeout) for a value that lives in the first entry alone.
    """
    _code, out, err = svn_run(wc, ["info", "--xml", "--depth", "empty", "."],
                              check=False, timeout=90)
    root = _xml(out) if out else None
    if root is None:
        raise DirectError(f"{wc.local_path} 不是可用的 SVN 工作副本："
                          f"{(err or out).strip()[:300] or 'svn info 无输出或超时'}")
    entry = root.find("./entry")
    if entry is None:
        raise DirectError(f"{wc.local_path} 下 svn info 没有返回条目（多半不是工作副本）")
    wc.url = (entry.findtext("url") or "").strip()
    # 1.6 keeps the copy's own revision on the <entry> element, not on <commit>.
    wc.revision = str(entry.get("revision") or "").strip()
    wc.svn_version = _svn_version(wc)
    expect = str(target.repo_url or "").rstrip("/")
    if expect and not (wc.url.rstrip("/") == expect or wc.url.startswith(expect + "/")):
        raise DirectError(
            f"该工作副本指向 {wc.url}，与产品 {wc.product_id} 绑定的仓库 {expect} 不一致。"
            "改码落点必须属于这个产品：请换一份副本或改绑定，不要手工绕过。")
    if not wc.revision:
        raise DirectError(f"{wc.local_path} 的 svn info 没有给出修订号，无法登记基线")


def _xml(text: str) -> ET.Element | None:
    try:
        return ET.fromstring(text)
    except ET.ParseError:
        return None


def _svn_version(wc: DirectWc) -> str:
    code, out, _err = svn_run(wc, ["--version", "--quiet"], check=False, timeout=60)
    return out.strip() if code == 0 else ""


# ---------------------------------------------------------------------------
# measurement
# ---------------------------------------------------------------------------

def status(wc: DirectWc, *, paths: list[str] | None = None,
           against_repo: bool = False, timeout: int = 300) -> Dirt:
    """Read `svn status --xml` (optionally `-u`, which also asks the repository).

    ``paths`` are working-copy-relative; empty means the whole copy. With
    ``against_repo`` the returned ``stale`` list holds every file the repository
    has newer content for -- that is the check that protects somebody else's
    commit from being overwritten.
    """
    args = ["status", "--xml"]
    if against_repo:
        args.append("-u")
    args += list(paths) if paths else ["."]
    dirt = Dirt()
    code, out, err = svn_run(wc, args, check=False, timeout=timeout)
    root = _xml(out) if out else None
    if code != 0 or root is None:
        dirt.state = "failed"
        dirt.why = (err or out).strip()[:400] or f"svn status 退出码 {code}"
        return dirt
    for entry in root.iter("entry"):
        rel = _clean(entry.get("path") or "")
        if not rel:
            continue
        wcst = entry.find("wc-status")
        item = (wcst.get("item") or "unknown") if wcst is not None else "unknown"
        props = (wcst.get("props") or "none") if wcst is not None else "none"
        repos = entry.find("repos-status")
        flag = _letter(item, props)
        info = {"item": item, "props": props, "flag": flag,
                "rev": (wcst.get("revision") or "") if wcst is not None else "",
                "repos": (repos.get("item") or "") if repos is not None else ""}
        dirt.entries[rel] = info
        if item == "conflicted":
            dirt.conflicts.append(rel)
        if item in REAL_ITEMS:
            dirt.real.append(rel)
        elif item in SOFT_ITEMS:
            dirt.soft.append(rel)
        if info["repos"]:
            dirt.stale.append(rel)
    dirt.real.sort(); dirt.soft.sort(); dirt.conflicts.sort(); dirt.stale.sort()
    return dirt


def _letter(item: str, props: str) -> str:
    """The `svn status` column-1 letter for an XML item name (display only)."""
    return {"modified": "M", "added": "A", "deleted": "D", "replaced": "R",
            "conflicted": "C", "unversioned": "?", "ignored": "I", "external": "X",
            "missing": "!", "incomplete": "!", "obstructed": "~"}.get(item, " ")


def _clean(path: str) -> str:
    return str(path).replace("\\", "/").strip().lstrip("./").strip("/")


def parse_rev(text: str) -> str:
    for pattern in (r"Committed revision (\d+)", r"修订版本 (\d+)",
                    r"revision (\d+)", r"^r(\d+)"):
        match = re.search(pattern, text, flags=re.M | re.I)
        if match:
            return match.group(1)
    return ""


def updated_rev(text: str) -> str:
    match = re.search(r"(?:Updated to revision|At revision)\s+(\d+)", text or "")
    return match.group(1) if match else ""


def diff_stat(wc: DirectWc, rel: str) -> dict[str, Any]:
    """Added/removed line counts for one file; the diff itself is never stored."""
    code, out, _err = svn_run(wc, ["diff", "--depth", "empty", rel], check=False, timeout=180)
    if code != 0:
        return {"file": rel, "added": 0, "removed": 0, "error": out.strip()[:200]}
    added = len([1 for line in out.splitlines()
                 if line.startswith("+") and not line.startswith("+++")])
    removed = len([1 for line in out.splitlines()
                   if line.startswith("-") and not line.startswith("---")])
    return {"file": rel, "added": added, "removed": removed}


def case_ambiguity(wc: DirectWc, paths: list[str]) -> list[dict[str, Any]]:
    """Flag any declared path whose directory holds >1 entry with the same name
    up to case. Those 109 pairs are why git mode forbids Windows-side work: the
    executor would edit one file while believing it edited the other."""
    found: list[dict[str, Any]] = []
    script = "ls -1 -- $1 2>/dev/null"
    for rel in paths:
        rel = _clean(rel)
        parent, _, name = rel.rpartition("/")
        code, out, _err = sh_run(wc, script, [parent or "."], timeout=60)
        if code != 0:
            continue
        names = [line for line in out.splitlines() if line.strip()]
        hits = [n for n in names if n.lower() == name.lower()]
        if len(hits) > 1:
            found.append({"file": rel, "candidates": sorted(hits),
                          "note": "同目录下存在只差大小写的多个条目，Windows 侧拼名会命中其中一个"})
    return found


def has_conflict_text(text: str) -> bool:
    """`svn update` prints a leading `C` for every file it could not merge."""
    return bool(re.search(r"^\s*C\s+\S+", text or "", flags=re.M)) or \
        "conflicts in" in (text or "").lower()


# ---------------------------------------------------------------------------
# the four lifecycle actions
# ---------------------------------------------------------------------------

def open_draft(wc: DirectWc, item: dict, files: list[str], *, note: str = "") -> dict[str, Any]:
    """Claim the working copy for this item and snapshot what is already dirty.

    Ownership is proved against that snapshot, not against a "clean" tree: a
    developer copy of this line always carries local modifications (measured on
    `trunk/3.0-86`: 187 tracked files already modified, 18409 unversioned
    entries). Refusing on any of that would make the mode unusable and teach
    nothing; refusing on anything that appears *after* the claim is the check
    that actually means "this change is not yours".
    """
    declared = [_clean(f) for f in files]
    mine = db.get_live_draft_for_item(item["id"], item.get("target") or "bug")
    already = set((mine or {}).get("files") or [])
    dirt = status(wc)
    if dirt.state == "failed":
        raise DirectError(f"读不出工作副本状态（未知不等于干净）：{dirt.why}")
    if dirt.conflicts:
        raise DirectError("工作副本已有冲突文件，先处理冲突再开工："
                          + ", ".join(dirt.conflicts[:6]))
    if declared:
        updated = update(wc, declared)
        if updated["conflict"]:
            raise DirectError("svn update 在这些文件上产生冲突，先人工解决再开工："
                              + updated["output"][-400:])
    else:
        updated = {"output": "", "rev": wc.revision, "conflict": False}
    # The baseline is "everything that was dirty before this item started", which
    # is every current modification except the ones already declared in an earlier
    # round of the same item. The newly declared files stay inside it on purpose:
    # if one of them was already dirty, `svn ci` would carry that other author's
    # work along, and `record()` reports exactly that as contamination.
    baseline = [p for p in dirt.real if p not in already]
    draft = db.claim_svn_draft(item, wc_path=wc.remote_path, wc_url=wc.url,
                               remote_wc=wc.remote_path, base_rev=wc.revision,
                               baseline=baseline, note=note)
    after = status(wc, paths=declared) if declared else dirt
    return {"draft_id": draft["id"], "base_rev": wc.revision, "wc_url": wc.url,
            "updated": updated, "soft": len(dirt.soft), "baseline": len(baseline),
            "declared_already_dirty": sorted(set(declared) & set(dirt.real)),
            "real": [p for p in (after.real if after.state == "ok" else dirt.real)
                     if p in set(declared)]}


def update(wc: DirectWc, paths: list[str]) -> dict[str, Any]:
    """`svn update` the given relative paths (never the whole copy: a 777k-entry
    tree would make an online session wait on the network for minutes)."""
    if not paths:
        return {"output": "", "rev": wc.revision, "conflict": False}
    _code, out, _err = svn_run(wc, ["update"] + list(paths), timeout=600)
    rev = updated_rev(out)
    if rev:
        wc.revision = rev
    return {"output": out.strip()[-1200:], "rev": rev, "conflict": has_conflict_text(out)}


def missing_paths(wc: DirectWc, paths: list[str]) -> list[str]:
    """Which declared paths do not exist on disk at all (one SSH round trip)."""
    if not paths:
        return []
    script = 'for p in "$@"; do [ -e "$p" ] || printf \'%s\\n\' "$p"; done'
    code, out, _err = sh_run(wc, script, list(paths), timeout=120)
    if code != 0:
        raise DirectError(f"检查声明路径是否存在失败（exit {code}）：{out.strip()[:200]}")
    return [_clean(line) for line in out.splitlines() if line.strip()]


def record(wc: DirectWc, item: dict, files: list[str], *, message: str = "") -> dict[str, Any]:
    """Measure this item's uncommitted change set and register it as the draft.

    Three proofs, all measured rather than asserted: every declared path exists
    and really changed; nothing that was clean when the copy was claimed is dirty
    now without being declared; and no declared file is behind the repository
    without being reported. A pre-existing modification among the declared files
    is reported as contamination -- `svn ci` would carry it along.
    """
    declared = [_clean(f) for f in files]
    if not declared:
        raise DirectError("--files 必填：逗号分隔的改动文件（工作副本内相对路径）")
    draft = db.get_live_draft_for_item(item["id"], item.get("target") or "bug")
    if draft is None:
        raise DirectError(f"该条目还没有领取工作副本，先执行 "
                          f"python -m app.cli checkout {item_slug(item['zentao_id'], item['target'])}")
    gone = missing_paths(wc, declared)
    if gone:
        raise DirectError("以下声明的路径在工作副本里不存在（拼错、还没创建，或已被删掉）："
                          + ", ".join(gone[:6]))
    dirt = status(wc, paths=declared)
    if dirt.state == "failed":
        raise DirectError(f"读不出工作副本状态：{dirt.why}")
    fresh = [p for p in declared if dirt.entries.get(p, {}).get("item") == "unversioned"]
    added = add_paths(wc, fresh) if fresh else []
    if added:
        dirt = status(wc, paths=declared)
    untouched = [p for p in declared
                 if dirt.entries.get(p, {}).get("item") not in REAL_ITEMS]
    if untouched:
        raise DirectError(
            "以下声明的文件在工作副本里没有任何改动：" + ", ".join(untouched[:6])
            + "（不许登记空草稿；只差大小写的同名文件被改到了也会落在这里，"
              "请用 svn diff 确认改动到底落在哪个拼名上）")
    whole = status(wc)
    if whole.state == "failed":
        raise DirectError(f"全副本状态读不出来，无法证明改动归属：{whole.why}")
    if whole.conflicts:
        raise DirectError("存在冲突文件，先解决再登记：" + ", ".join(whole.conflicts[:6]))
    baseline = set(draft.get("baseline_files") or [])
    new_dirt = [p for p in whole.real if p not in baseline and p not in set(declared)]
    if new_dirt:
        raise DirectError("领副本之后出现的改动不在本条声明里，拒绝登记（不替别人提交）："
                          + ", ".join(new_dirt[:6]))
    contaminated = sorted(set(declared) & baseline)
    stale = status(wc, paths=declared, against_repo=True)
    if stale.state == "failed":
        raise DirectError(f"读不出这些文件相对仓库的状态（未知不等于不落后）：{stale.why}")
    behind = [p for p in stale.stale if p in declared]
    ambiguous = case_ambiguity(wc, declared)
    stats = [diff_stat(wc, p) for p in declared]
    lines = [f"{dirt.entries[p]['flag']} {p} +{s['added']}/-{s['removed']}"
             for p, s in zip(declared, stats)]
    summary = "\n".join(lines)
    db.update_svn_draft(draft["id"], {
        "files": declared,
        "diff_stat": summary[:4000],
        "status": "recorded",
        "note": message[:400],
    })
    saved = db.get_svn_draft(draft["id"]) or {}
    out: dict[str, Any] = {
        "draft_id": draft["id"], "base_rev": saved.get("base_rev", ""),
        "files": declared, "summary": summary, "added_files": added,
        "wc_url": wc.url, "behind": behind, "contaminated": contaminated,
        "ambiguous": [a["file"] for a in ambiguous],
        "baseline_count": len(baseline), "counts": whole.counts(),
    }
    warns = []
    if behind:
        warns.append("这些文件在仓库里已有新版本，先 svn update 再重验：" + ", ".join(behind[:6]))
    if contaminated:
        warns.append("这些文件在你领副本之前就是脏的，`svn ci` 会把别人的改动一起带走："
                     + ", ".join(contaminated[:6]))
    if ambiguous:
        warns.append("这些路径所在目录存在只差大小写的同名条目，请核对改动落在哪个拼名上："
                     + "; ".join(f"{a['file']} -> {','.join(a['candidates'])}"
                                 for a in ambiguous[:4]))
    if warns:
        out["warning"] = "；".join(warns)
    return out


def add_paths(wc: DirectWc, paths: list[str]) -> list[str]:
    """`svn add` newly created files. Local-only (no repository write) and
    fully undone by `svn revert`, so an executor may do it."""
    done: list[str] = []
    for rel in paths:
        code, out, err = svn_run(wc, ["add", rel], check=False, timeout=120)
        if code == 0 and ("A" in out or "added" in out.lower()):
            done.append(rel)
        elif code != 0 and "already under version control" not in (err or out):
            raise DirectError(f"svn add {rel} 失败：{(err or out).strip()[:300]}")
    return done


def precheck(wc: DirectWc, files: list[str], base_rev: str) -> dict[str, Any]:
    """Read-only: what would happen if these files were committed right now."""
    dirt = status(wc, paths=files, against_repo=True)
    if dirt.state == "failed":
        raise DirectError(f"提交前状态读不出来：{dirt.why}")
    missing = [f for f in files if dirt.entries.get(f, {}).get("item") not in REAL_ITEMS]
    behind = [p for p in dirt.stale]
    conflicts = [p for p in dirt.entries if dirt.entries[p]["item"] == "conflicted"]
    ok = not (missing or behind or conflicts)
    reasons = []
    if missing:
        reasons.append("没有本地改动：" + ", ".join(missing[:6]))
    if behind:
        reasons.append("trunk 上已有更新的版本，先 svn update 再重验：" + ", ".join(behind[:6]))
    if conflicts:
        reasons.append("冲突态：" + ", ".join(conflicts[:6]))
    return {"ok": ok, "base_rev": base_rev, "files": list(files),
            "behind": behind, "conflicts": conflicts, "unchanged": missing,
            "reason": "；".join(reasons)}


def push(wc: DirectWc, item: dict, files: list[str], message: str, *,
         owner_approved: bool, dry_run: bool = False,
         trunk_url: str = "", allow_dirty_base: bool = False) -> dict[str, Any]:
    """Commit the declared files with `svn ci`.

    This is the only path in direct mode that writes to the repository, and three
    independent locks guard it: the caller must pass the owner's explicit
    acknowledgement (nobody tells an executor to type that flag), no declared
    file may have been dirty *before* this item claimed the copy (otherwise
    somebody else's work leaves with yours), and the global
    `SVN_ALLOW_TRUNK_WRITE` switch must be on when the copy points at trunk. If
    any lock is closed nothing is committed and the reason is printed.
    """
    if not owner_approved:
        raise DirectError(
            "svn_direct 模式默认不提交：改完由主人自己 svn ci。"
            "确要本系统代提交 trunk，必须显式带 --owner-approved（闸门 G11 的 direct 例外，"
            "只在主人当面要求时使用），并先跑一次 push --dry-run 看预检。")
    if trunk_url and wc.url.rstrip("/") == trunk_url.rstrip("/") \
            and not get_settings().svn_allow_trunk_write:
        raise DirectError(
            f"该副本指向 trunk（{wc.url}），而 .env 的 SVN_ALLOW_TRUNK_WRITE=false。"
            "这是本系统唯一能写主干的开关：要么主人自己 svn ci，要么显式改配置后再 push。")
    draft = db.get_live_draft_for_item(item["id"], item.get("target") or "bug")
    if draft is None:
        raise DirectError("该条目没有在册草稿：先 checkout 再 draft，才能确定要提交哪些文件")
    declared = [_clean(f) for f in files]
    if sorted(declared) != sorted(draft.get("files") or []):
        raise DirectError(
            "提交清单与登记的草稿不一致（草稿："
            + ", ".join((draft.get("files") or [])[:6]) + "；本次：" + ", ".join(declared[:6])
            + "）。重新 draft 后再推，别把两处改动合成一笔。")
    baseline = set(draft.get("baseline_files") or [])
    dirty_base = sorted(set(declared) & baseline)
    if dirty_base and not allow_dirty_base:
        raise DirectError(
            "这些文件在本条目领取工作副本之前就已经是改动状态，svn ci 会把别人的改动一起带走："
            + ", ".join(dirty_base[:6])
            + "。先确认那些改动该不该走（该走就归到那条条目去提交），"
              "确实要一起提交再显式加 --allow-dirty-base。")
    check = precheck(wc, declared, draft.get("base_rev", ""))
    if not check["ok"]:
        raise DirectError("预检不通过，未提交任何东西：" + check["reason"])
    if dry_run:
        return {"dry_run": True, "revision": "", "files": declared, "precheck": check,
                "contaminated": dirty_base,
                "note": "预检通过；未执行 svn ci"}
    _code, out, _err = svn_run(wc, ["commit", "-m", message] + declared, timeout=600)
    rev = parse_rev(out)
    if not rev:
        raise DirectError(f"svn ci 已执行但读不到修订号，请人工核对 trunk 日志：{out.strip()[:400]}")
    db.close_svn_draft(draft["id"], "pushed", revision=f"r{rev}", note="owner-approved push")
    return {"dry_run": False, "revision": f"r{rev}", "files": declared,
            "precheck": check, "contaminated": dirty_base,
            "output": out.strip()[-800:], "draft_id": draft["id"]}


def revert(wc: DirectWc, item: dict, files: list[str] | None = None) -> dict[str, Any]:
    """Throw away this item's uncommitted changes -- and only its own."""
    draft = db.get_live_draft_for_item(item["id"], item.get("target") or "bug")
    if draft is None:
        raise DirectError("该条目没有在册草稿，没什么可回滚")
    declared = draft.get("files") or []
    targets = [_clean(f) for f in (files or declared)]
    if not targets:
        raise DirectError("草稿里没有登记文件，无法确定回滚范围（拒绝整仓 revert）")
    outside = [p for p in targets if p not in declared]
    if outside:
        raise DirectError("以下路径不属于本条草稿，拒绝回滚（不丢别人的改动）："
                          + ", ".join(outside[:6]))
    # An added file becomes unversioned again; a modified/deleted one returns to
    # the pristine base. Both need --depth empty so a directory is never swept.
    reverted: list[str] = []
    for rel in targets:
        svn_run(wc, ["revert", "--depth", "empty", rel], timeout=180)
        reverted.append(rel)
    after = status(wc, paths=targets)
    leftover = [p for p in (after.real or []) if p in targets] if after.state == "ok" else targets
    state = "reverted" if not leftover else "editing"
    db.close_svn_draft(draft["id"], state, note=f"revert {len(reverted)} 个文件"
                       + (f"，仍有残留：{','.join(leftover[:4])}" if leftover else ""))
    return {"reverted": reverted, "leftover": leftover, "status": state,
            "draft_id": draft["id"],
            "note": "" if after.state == "ok" else f"回滚后状态读不出来：{after.why}"}


def describe(target: RepoTarget, item: dict, *, against_repo: bool = False,
             full: bool = False) -> dict[str, Any]:
    """What `mirror` reports in direct mode: read-only, never a write, never a revert.

    A whole-copy `svn status` on this line costs about a minute (measured 58.6 s
    on `trunk/3.0-86`), so the probe reads only this item's declared files unless
    the caller asks for the full sweep with `--full`. The ownership proof still
    happens over a full scan -- in `checkout` and `draft`, once per round.
    """
    wc = resolve(target, item=item)
    draft = db.get_live_draft_for_item(item["id"], item.get("target") or "bug")
    declared = list((draft or {}).get("files") or [])
    # No declared files and no --full means there is nothing of this item's to
    # read: skip the scan entirely instead of falling through to a whole-copy
    # `svn status .`, which costs about a minute and proves nothing about this item.
    if declared or full:
        dirt = status(wc, paths=declared if declared else None,
                      against_repo=against_repo)
        scanned = "declared" if declared else "whole-copy"
    else:
        dirt = Dirt(state="skipped", why="本条目还没有登记文件；全副本扫描在 checkout/draft 里做")
        scanned = "none"
        against_repo = False
    holder = db.get_live_draft_for_wc(wc.remote_path)
    held_here = bool(holder and draft and holder["id"] == draft["id"])
    out: dict[str, Any] = {
        "code_mode": "svn_direct", "wc_local": wc.local_path, "wc_remote": wc.remote_path,
        "url": wc.url, "revision": wc.revision, "svn_version": wc.svn_version,
        "draft_id": (draft or {}).get("id"), "draft_status": (draft or {}).get("status"),
        "base_rev": (draft or {}).get("base_rev"), "files": declared,
        "diff_stat": (draft or {}).get("diff_stat"),
        "scanned": scanned,
        "worktree_clean": dirt.state == "ok" and not dirt.real,
        "dirty_real": dirt.real[:12], "dirty_real_count": len(dirt.real),
        "dirty_soft_count": len(dirt.soft),
        "conflicts": dirt.conflicts[:12], "stale": dirt.stale[:12],
        "baseline_count": len((draft or {}).get("baseline_files") or []),
        "dirt": {"state": dirt.state, "svn": wc.svn_version, "why": dirt.why},
        "held_by": (db.draft_holder_label(holder) if holder else ""),
        "held_by_this_item": held_here,
        "mirror_ready": bool(wc.url and wc.revision and dirt.state != "failed"),
        "summary": (f"{wc.local_path} @ r{wc.revision}："
                    f"{'本条改动 ' + str(len(declared)) + ' 个文件' if declared else '尚未登记改动'}、"
                    f"冲突 {len(dirt.conflicts)}、落后 trunk {len(dirt.stale)}"),
    }
    if holder and not held_here:
        out["error"] = f"工作副本被 {out['held_by']} 占用，本条目不能开工"
    return out


def mark_landed(item: dict, revision: str) -> str:
    """Free the working claim once a real r number is registered for this item.

    Called when the owner commits by hand and backfills `--revision r<号>`: the
    copy stops belonging to that item, so the next item may claim it.
    """
    draft = db.get_live_draft_for_item(item["id"], item.get("target") or "bug")
    if draft is None:
        return ""
    rev = str(revision or "").lstrip("r")
    if not rev:
        return ""
    db.close_svn_draft(draft["id"], "pushed", revision=f"r{rev}", note="主人回填真实 r 号")
    return rev


def reconcile(item: dict) -> dict[str, Any]:
    """Direct counterpart of `reconcile`: a held copy with nothing registered here.

    An online session that ends before `draft`/`commit` leaves the copy dirty and
    the database unaware of it. This only reports that shape.
    """
    draft = db.get_live_draft_for_item(item["id"], item.get("target") or "bug")
    if draft is None:
        return {}
    registered = [str(r.get("revision") or "") for r in (item.get("revisions") or [])]
    local = [r for r in registered if r.startswith("local@")]
    landed = [r for r in registered if re.fullmatch(r"r?\d+", r)]
    if local or landed:
        return {}
    return {
        "zentao_id": item["zentao_id"], "kind": item.get("target"),
        "draft_id": draft["id"], "draft_status": draft["status"],
        "wc": draft["wc_path"], "base_rev": draft["base_rev"],
        "files": draft["files"],
        "why": "SVN 副本里有该条目的在册草稿，但库里没有任何 local@/r 登记"
               "（跑完没收口，或改动其实还没做）",
    }
