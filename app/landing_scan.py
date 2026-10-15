"""Local discovery of the SVN checkout a direct round should land in.

AUTO_LOOP.md §1.9 needs one working copy per item. The owner should not have to
name it: the product already knows which repository it belongs to, the mirror
tells us which directory the team's checkouts live beside, and every SVN 1.6
working copy declares its own URL inside ``.svn/entries``. Reading that file
answers two questions locally and in microseconds -- "is this a working copy at
all", and "of which repository" -- which is what excludes a checkout of another
product line (measured: ``U:/hy-nmc/3.0`` reports root ``.../hy-nmc-2``) before
anything could ever be edited there.

Nothing here runs `svn` or reaches the build server: the point is to *pick a
candidate*, and `svn_direct.resolve` still does the authoritative verification
over SSH before any edit is allowed. The reads themselves are ordinary directory
reads -- but the mirrors live on mapped network drives, so they are far from free
and are cached accordingly (see `CACHE_TTL_SECONDS`).
"""

from __future__ import annotations

import time
from pathlib import Path
from typing import Any

from . import db

# A page renders one candidate lookup per card. These directories live on SMB
# mapped drives (U:/ and Q:/), where a single-level listing plus four small reads
# measured 117 ms + 209 ms -- and a kanban page did that 118 times (measured:
# GET / went from 0.7 s to 121 s). The scan result is therefore cached per
# (repository, mirror) pair; occupancy is deliberately *not* cached, so a copy
# another item just claimed is reflected on the very next render.
CACHE_TTL_SECONDS = 60.0
_scan_cache: dict[str, tuple[float, list[dict[str, Any]]]] = {}


def cache_key(repo_url: Any, landing_dirs: list[Any]) -> str:
    """Stable identity for one scan: the repository and the mirrors we looked beside."""
    dirs = ";".join(sorted({str(d or "").strip().rstrip("/\\").lower() for d in landing_dirs
                            if str(d or "").strip()}))
    return f"{str(repo_url or '').strip().rstrip('/').lower()}|{dirs}"


def invalidate_cache() -> None:
    """Forget cached listings (after a new checkout, or for tests)."""
    _scan_cache.clear()


def _scan_once(repo_url: str, landing_dirs: list[Any]) -> list[dict[str, Any]]:
    """Uncached candidate scan: enumerate siblings, read manifests, keep our repository."""
    seen: set[str] = set()
    candidates: list[dict[str, Any]] = []
    for mirror in landing_dirs:
        for copy_dir in sibling_copies(mirror):
            key = str(copy_dir).rstrip("/\\").lower()
            if key in seen:
                continue
            seen.add(key)
            info = read_manifest(copy_dir)
            if not info or not _same_repo(info["repo_root"], repo_url):
                continue
            candidates.append(info)
    return candidates


def read_manifest(path: Any) -> dict[str, str] | None:
    """Parse an SVN 1.6 ``.svn/entries`` header into a candidate description.

    Layout (1.6): format number, blank, ``dir``, revision, URL, repository root.
    A 1.7+ working copy keeps this in ``wc.db`` (SQLite) instead and is reported
    as unreadable rather than guessed at -- a wrong URL here would send an edit
    into another line's code.
    """
    root = Path(str(path or ""))
    if not root.is_dir():
        return None
    entries = root / ".svn" / "entries"
    try:
        lines = entries.read_text(encoding="utf-8", errors="replace").splitlines()
    except OSError:
        return None
    if len(lines) < 6 or lines[2].strip() != "dir":
        return None
    url = lines[4].strip()
    repo_root = lines[5].strip()
    if not url or not repo_root:
        return None
    rev = lines[3].strip()
    return {
        "path": str(root),
        "url": url,
        "repo_root": repo_root,
        "revision": rev if rev.isdigit() else "",
    }


def sibling_copies(landing_dir: Any) -> list[Path]:
    """Working copies living beside a mirror -- one directory level, never recursive.

    The scan is deliberately bounded to ``<parent of working_copy>/*``: that is
    where this team's checkouts actually sit (``U:/hy-10G-2/trunk/git.3.0`` and
    ``…/trunk/3.0-86`` are siblings), and a bounded enumeration is also the only
    kind of directory walk this repo's rules allow.
    """
    mirror = Path(str(landing_dir or ""))
    parent = mirror.parent if str(landing_dir or "").strip() else None
    if parent is None or not parent.is_dir():
        return []
    found: list[Path] = []
    try:
        for child in sorted(parent.iterdir()):
            if child.name == mirror.name:
                continue
            if (child / ".svn").is_dir():
                found.append(child)
    except OSError:
        return []
    return found


def _same_repo(candidate_root: str, repo_url: str) -> bool:
    """Repository-root equality, ignoring trailing slashes and case of the drive."""
    a = str(candidate_root or "").rstrip("/").lower()
    b = str(repo_url or "").rstrip("/").lower()
    return bool(a) and a == b


def _occupancy() -> dict[str, str]:
    """local copy path -> the item holding it, for live direct drafts.

    Keys are normalised the way `db.claim_svn_draft` stores them: the draft row
    carries the Windows-side path, and Windows folds case, so both sides are
    lower-cased before they are compared.
    """
    out: dict[str, str] = {}
    for draft in db.list_live_drafts():
        wc_local = db.norm_wc_path(draft.get("wc_path"))
        if wc_local:
            out[wc_local.lower()] = db.item_slug(draft.get("zentao_id"), draft.get("target") or "bug")
    return out


def discover(repo_url: str, landing_dirs: list[Any]) -> list[dict[str, Any]]:
    """Ranked landing candidates for one repository: the copy we would pick is first.

    Ranking rules, in order, all of them mechanical so the choice is explainable:

    1. only checkouts whose *repository root* equals ``repo_url`` survive --
       another product line is excluded, not merely deprioritised;
    2. a copy another item is currently holding moves to the back, because one
       copy carries one uncommitted change set (§1.9);
    3. among the rest, the copy aligned with the newest repository revision wins:
       it is the least likely to need an update before the work can be committed.
    """
    repo_url = str(repo_url or "").strip()
    if not repo_url:
        return []
    key = cache_key(repo_url, landing_dirs)
    hit = _scan_cache.get(key)
    now = time.monotonic()
    if hit and now - hit[0] < CACHE_TTL_SECONDS:
        scanned = hit[1]
    else:
        scanned = _scan_once(repo_url, landing_dirs)
        _scan_cache[key] = (now, scanned)
    # Copy before annotating: the cached rows must never gain per-call fields.
    candidates = [dict(c) for c in scanned]
    held = _occupancy()
    for cand in candidates:
        cand["held_by"] = held.get(str(cand["path"]).rstrip("/\\").lower(), "")
        cand["revision_int"] = int(cand["revision"]) if cand["revision"].isdigit() else -1
    candidates.sort(key=lambda c: (bool(c["held_by"]), -c["revision_int"], c["path"].lower()))
    return candidates


def discover_target(target: Any) -> list[dict[str, Any]]:
    """`discover` for a RepoTarget (used by the web layer, which already has one)."""
    return discover(getattr(target, "repo_url", ""),
                    [getattr(target, "working_copy", None), getattr(target, "svn_working_copy", None)])


def discover_binding(binding: dict[str, Any]) -> list[dict[str, Any]]:
    """`discover` straight from a product_repos row -- no SSH config is read.

    The binding carries both the repository URL and the mirror directory, which is
    all discovery needs; going through `svn_client.resolve_target` here would drag
    in the global settings and, on failure, hide the real reason.
    """
    return discover(str(binding.get("repo_url") or ""),
                    [binding.get("working_copy"), binding.get("svn_working_copy")])


def pick_reason(chosen: dict[str, Any], candidates: list[dict[str, Any]]) -> str:
    """Why this copy, in one line -- and what the owner could pick instead."""
    others = [c["path"] for c in candidates if c is not chosen]
    base = (f"自动判定：{chosen['path']}（URL {chosen['url']}，r{chosen['revision'] or '未知'}），"
            f"依据是与产品绑定的仓库根 {chosen['repo_root']} 一致")
    if not others:
        return base + "，且它是同仓库唯一可见的检出"
    return base + f"；同仓库还有 {len(others)} 份候选（{'; '.join(others)}），" \
                  "排序取的是未被占用且最接近仓库当前版本的那一份"
