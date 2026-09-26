"""Thin wrapper around the `svn` command line client.

Different Zentao products live in different code repositories, so every
operation resolves a :class:`RepoTarget` first: the per-product binding stored
in the `product_repos` table when present, otherwise the global ``SVN_*``
defaults from `.env`.

Hard rules enforced here:
* every automatic commit goes to a branch under ``<repo>/branches/<prefix><id>``
* commits touching the trunk URL are refused unless ``SVN_ALLOW_TRUNK_WRITE=true``
  (that flag exists for the *human* merge step only)
"""

from __future__ import annotations

import hashlib
import re
import subprocess
from dataclasses import dataclass
from pathlib import Path

from . import db
from .config import PROJECT_ROOT, get_settings


class SvnError(RuntimeError):
    pass


@dataclass
class RepoTarget:
    """Everything needed to operate on one repository."""

    product_id: int | None
    product_name: str
    repo_url: str
    trunk_path: str
    branch_root: str
    working_copy: Path
    branch_prefix: str
    source: str = "global"          # "global" | "product"

    def trunk_url(self) -> str:
        return f"{self.repo_url.rstrip('/')}{('/' + self.trunk_path.strip('/')) if self.trunk_path.strip('/') else ''}"

    def branch_root_url(self) -> str:
        return f"{self.repo_url.rstrip('/')}{('/' + self.branch_root.strip('/')) if self.branch_root.strip('/') else ''}"

    def branch_url(self, branch_name: str) -> str:
        return f"{self.branch_root_url().rstrip('/')}/{branch_name}"

    def label(self) -> str:
        who = f"产品 {self.product_id}" if self.product_id else "全局默认"
        return f"{self.product_name or who} -> {self.repo_url}"


@dataclass
class SvnResult:
    code: int
    stdout: str
    stderr: str

    @property
    def output(self) -> str:
        return (self.stdout or "") + (("\n" + self.stderr) if self.stderr else "")

    def raise_for_error(self) -> "SvnResult":
        if self.code != 0:
            raise SvnError(f"svn 执行失败 (exit {self.code}): {self.output.strip()[:600]}")
        return self


def _decode(raw: bytes) -> str:
    """svn on a Chinese Windows console prints GBK, commit messages may be UTF-8."""
    if not raw:
        return ""
    for encoding in ("utf-8", "gbk"):
        try:
            return raw.decode(encoding)
        except UnicodeDecodeError:
            continue
    return raw.decode("utf-8", errors="replace")


def run(args: list[str], *, cwd: Path | None = None, check: bool = True) -> SvnResult:
    settings = get_settings()
    cmd = [settings.svn_bin, "--non-interactive"]
    if settings.svn_username:
        cmd += [f"--username={settings.svn_username}", f"--password={settings.svn_password}",
                "--no-auth-cache"]
    cmd += args
    try:
        proc = subprocess.run(
            cmd, cwd=str(cwd) if cwd else None, capture_output=True, timeout=600,
        )
    except FileNotFoundError as exc:
        raise SvnError(f"找不到 svn 命令（可设置 SVN_BIN）：{exc}") from exc
    except subprocess.TimeoutExpired as exc:
        raise SvnError(f"svn 命令超时: {' '.join(args)}") from exc
    result = SvnResult(proc.returncode, _decode(proc.stdout), _decode(proc.stderr))
    if check:
        result.raise_for_error()
    return result


# ---------------------------------------------------------------------------
# target resolution
# ---------------------------------------------------------------------------

def global_target() -> RepoTarget:
    """The fallback repository described by the global `SVN_*` settings."""
    settings = get_settings()
    wc = Path(settings.svn_working_copy) if str(settings.svn_working_copy) else (PROJECT_ROOT / "svn_workspace")
    return RepoTarget(
        product_id=None,
        product_name="",
        repo_url=settings.svn_repo_url,
        trunk_path=settings.svn_trunk_path,
        branch_root=settings.svn_branch_root,
        working_copy=wc,
        branch_prefix=settings.branch_prefix,
        source="global",
    )


def _short_hash(text: str) -> str:
    return hashlib.md5(text.encode("utf-8")).hexdigest()[:8]


def resolve_target(bug: dict | None = None, product_id: int | None = None) -> RepoTarget:
    """Pick the repository for a bug: per-product binding first, then defaults.

    Each bound product gets its own working copy directory so switching between
    products' branches cannot mix two repositories in one checkout.
    """
    settings = get_settings()
    if product_id is None and bug is not None:
        product_id = bug.get("product_id")
    binding = db.get_product_repo(product_id) if product_id else None
    if binding and not str(binding.get("repo_url") or "").strip():
        raise SvnError(
            f"产品 {product_id} 已绑定代码库但 repo_url 为空，请用 `app.cli bind-repo` 修正"
        )
    if binding and not binding.get("enabled", 1):
        raise SvnError(f"产品 {product_id} 的代码库绑定已被禁用（enabled=0）")

    if binding:
        wc = str(binding.get("working_copy") or "").strip()
        if not wc:
            repo_hash = _short_hash(str(binding["repo_url"]))
            wc = str(Path(settings.svn_working_copy) / f"p{product_id}-{repo_hash}")
        return RepoTarget(
            product_id=int(product_id),
            product_name=str(binding.get("product_name") or (bug or {}).get("product_name") or ""),
            repo_url=str(binding["repo_url"]).rstrip("/"),
            trunk_path=str(binding.get("trunk_path") or "/trunk"),
            branch_root=str(binding.get("branch_root") or "/branches"),
            working_copy=Path(wc),
            branch_prefix=str(binding.get("branch_prefix") or "") or settings.branch_prefix,
            source="product",
        )

    target = global_target()
    if bug and bug.get("product_id"):
        target.product_id = int(bug["product_id"])
        target.product_name = str(bug.get("product_name") or "")
    return target


def require_target(bug: dict | None = None, product_id: int | None = None) -> RepoTarget:
    """Resolve a target and fail early when nothing is configured."""
    target = resolve_target(bug=bug, product_id=product_id)
    if not target.repo_url:
        pid = target.product_id
        raise SvnError(
            f"没有可用仓库地址：产品 {pid} 未绑定代码库，且 .env 的 SVN_REPO_URL 为空。"
            f"绑定命令：python -m app.cli bind-repo {pid} <SVN仓库根地址>"
            if pid else "SVN_REPO_URL 未配置，请写入 .env 或用 bind-repo 绑定产品"
        )
    return target


# ---------------------------------------------------------------------------
# read-only helpers
# ---------------------------------------------------------------------------

def client_version() -> str:
    """Version string of the configured `svn` binary ('' when unavailable)."""
    result = run(["--version", "--quiet"], check=False)
    return result.stdout.strip() if result.code == 0 else ""


def _version_tuple(text: str) -> tuple[int, ...]:
    match = re.search(r"(\d+)\.(\d+)(?:\.(\d+))?", text or "")
    if not match:
        return (0, 0, 0)
    return tuple(int(part or 0) for part in match.groups())


def info(path: Path | str | None = None, *, target: RepoTarget | None = None) -> str:
    arg = path if path is not None else (target.working_copy if target else Path(""))
    return run(["info", str(arg)]).output


def current_url(path: Path | None = None) -> str:
    out = info(path)
    match = re.search(r"^URL:\s*(.+)$", out, flags=re.M)
    return match.group(1).strip() if match else ""


def working_copy(target: RepoTarget) -> Path:
    return Path(target.working_copy)


def branch_exists(branch_name: str, *, target: RepoTarget) -> bool:
    return run(["info", target.branch_url(branch_name)], check=False).code == 0


def _ensure_folder(url: str) -> None:
    """Create an intermediate folder (e.g. `/branches`) when the repo lacks it."""
    if run(["info", url], check=False).code == 0:
        return
    # `check=False`: a conflict with a concurrent creator is fine, we just need it to exist.
    run(["mkdir", url, "-m", "create branch root for bugfix automation"], check=False)


def status(target: RepoTarget, *, wc: Path | None = None) -> list[dict]:
    """Parse `svn st` into [{path, action}] for the review page."""
    result = run(["st", str(wc or target.working_copy)])
    items = []
    for line in result.stdout.splitlines():
        if not line.strip():
            continue
        flag = line[0]
        if flag in ("?", "I"):
            continue
        path = line[8:].strip() if len(line) > 8 else line[1:].strip()
        action = {"A": "added", "M": "modified", "D": "deleted", "R": "replaced",
                  "C": "conflict"}.get(flag, flag or "unchanged")
        items.append({"path": path, "action": action})
    return items


def diff(target: RepoTarget, *, limit_lines: int = 400) -> str:
    result = run(["diff", str(target.working_copy)])
    lines = result.stdout.splitlines()
    if len(lines) > limit_lines:
        return "\n".join(lines[:limit_lines]) + f"\n... (truncated, {len(lines)} lines total)"
    return "\n".join(lines)


def log(target: RepoTarget, limit: int = 20, *, url: str | None = None) -> list[dict]:
    result = run(["log", url or str(target.working_copy), "-l", str(int(limit))])
    entries: list[dict] = []
    record: dict = {}
    for line in result.stdout.splitlines():
        line = line.rstrip()
        if not line or re.fullmatch(r"-{10,}", line):
            continue
        match = re.match(r"^r(\d+)\s*\|\s*(.+?)\s*\|\s*(.+?),", line)
        if match:
            if record:
                entries.append(record)
            record = {"revision": match.group(1), "author": match.group(2),
                      "date": match.group(3), "message": ""}
        elif record:
            record["message"] = (record["message"] + "\n" + line).strip()
    if record:
        entries.append(record)
    return entries


# ---------------------------------------------------------------------------
# write helpers (branch only -- trunk is guarded)
# ---------------------------------------------------------------------------

def ensure_working_copy(target: RepoTarget) -> Path:
    """Checkout the target's trunk into the product's working copy if missing."""
    wc = Path(target.working_copy)
    if (wc / ".svn").exists():
        return wc
    wc.parent.mkdir(parents=True, exist_ok=True)
    run(["checkout", target.trunk_url(), str(wc)])
    return wc


def create_branch(branch_name: str, *, target: RepoTarget, message: str = "",
                  copy_from: str | None = None) -> str:
    """Server side `svn copy` from trunk to the branch, then switch local wc."""
    branch_name = f"{target.branch_prefix}{branch_name}" if not branch_name.startswith(
        target.branch_prefix) else branch_name
    br_full = branch_name
    target_url = target.branch_url(branch_name)
    source = copy_from or target.trunk_url()
    if not branch_exists(branch_name, target=target):
        _ensure_folder(target.branch_root_url())
        run(["copy", source, target_url, "-m", message or f"branch for {br_full}"])
    wc = ensure_working_copy(target)
    if current_url(wc) != target_url:
        run(["switch", target_url, str(wc)])
    return target_url


def parse_revision(text: str) -> str:
    for pattern in (r"Committed revision (\d+)", r"修订版本 (\d+)", r"revision (\d+)", r"^r(\d+)"):
        match = re.search(pattern, text, flags=re.M | re.I)
        if match:
            return match.group(1)
    return ""


def commit(files: list[str] | None, message: str, *, target: RepoTarget) -> str:
    """Commit and return the new revision number (e.g. ``"1234"``)."""
    settings = get_settings()
    wc = Path(target.working_copy)
    if not (wc / ".svn").exists():
        raise SvnError(f"工作副本 {wc} 尚未检出，请先执行 branch 建立/切换分支")
    url = current_url(wc)
    trunk = target.trunk_url().rstrip("/")
    if not settings.svn_allow_trunk_write and url.rstrip("/") == trunk:
        raise SvnError(
            f"当前工作副本指向 trunk（{url}）。AI 禁止直接提交主干，"
            "请先创建/切换到 bugfix 分支。"
        )
    args = ["commit", "-m", message]
    if files:
        args += [str(wc / f) if not Path(f).is_absolute() else f for f in files]
    else:
        args.append(str(wc))
    result = run(args, cwd=wc)
    revision = parse_revision(result.output)
    if not revision:
        raise SvnError(f"提交成功但无法解析修订号: {result.output.strip()[:400]}")
    return revision


# ---------------------------------------------------------------------------
# self test
# ---------------------------------------------------------------------------

def _step(name: str, ok: bool | None, detail: str) -> dict:
    return {"name": name, "ok": ok, "detail": detail}


def preflight_target(target: RepoTarget) -> list[dict]:
    """Read-only checks for one repository."""
    steps: list[dict] = []
    if not target.repo_url:
        steps.append(_step("仓库地址", False, "未配置（.env 的 SVN_REPO_URL 或产品绑定为空）"))
        return steps
    steps.append(_step("仓库地址", True, f"{target.label()}（来源：{target.source}）"))

    # Probe the trunk path, not the repository root: servers with path-level authz deny
    # the root while still allowing the product line, and the root is never operated on.
    trunk = run(["info", target.trunk_url()], check=False)
    if trunk.code != 0:
        text = trunk.output.strip()[:300]
        steps.append(_step("仓库可达", False, f"{target.trunk_url()}：{text or '未知错误'}"))
        return steps
    settings = get_settings()
    steps.append(_step("仓库可达", True, f"svn info {target.trunk_url()} 成功"
                       + ("（用上 SVN_USERNAME/SVN_PASSWORD）" if settings.svn_username
                          else "（用本机缓存的凭证）")))
    steps.append(_step("trunk 目录", True, target.trunk_url()))

    root = run(["info", target.branch_root_url()], check=False)
    steps.append(_step("branches 根目录", None,
                       "已存在" if root.code == 0 else "不存在：首次建分支时会自动 svn mkdir 创建"))

    wc = Path(target.working_copy)
    version = client_version()
    if (wc / ".svn").exists():
        current = current_url(wc)
        steps.append(_step("工作副本", True, f"{wc} 指向 {current or '未知 URL'}"))
        mixed = current and target.repo_url and not current.startswith(target.repo_url.rstrip("/"))
        if mixed:
            steps.append(_step("工作副本归属", False,
                               f"该副本属于 {current}，与本产品仓库不一致，请换目录或删掉重新检出"))
        if (wc / ".svn" / "wc.db").exists() and _version_tuple(version) < (1, 7, 0):
            steps.append(_step("工作副本格式", False,
                               f"本机 svn {version} 读不了 1.7+ 格式（.svn/wc.db），请升级客户端并设 SVN_BIN"))
    else:
        if (wc / ".git").exists():
            steps.append(_step("工作副本", None,
                               f"{wc} 是 git 镜像（不是 SVN 工作副本）：默认方式只在其中 git commit，"
                               f"进正式库由主人 dcommit"))
        else:
            steps.append(_step("工作副本", None, f"{wc} 未检出，首次建分支时自动 checkout"))
    return steps


def preflight(product_id: int | None = None, *, all_targets: bool = False) -> dict:
    """Read-only self test: binary + one repo, or every bound repo."""
    settings = get_settings()
    version = client_version()
    if not version:
        return {"ok": False,
                "steps": [_step("svn 客户端", False,
                                f"找不到 `{settings.svn_bin}`，请安装 SVN 命令行客户端或设置 SVN_BIN")],
                "hint": "先让 svn 命令可用"}

    steps: list[dict] = [
        _step("svn 客户端", True, f"{version}（工作副本格式要求 >= 1.7）"),
        _step("主干保护", not settings.svn_allow_trunk_write,
              "SVN_ALLOW_TRUNK_WRITE=false，AI 无法提交主干"
              if not settings.svn_allow_trunk_write
              else "危险：SVN_ALLOW_TRUNK_WRITE=true，AI 可以提交主干"),
    ]

    targets: list[RepoTarget] = []
    if all_targets:
        bindings = [b for b in db.list_product_repos()]
        targets = [resolve_target(product_id=b["product_id"]) for b in bindings]
        if settings.svn_repo_url:
            targets.append(global_target())
        if not targets:
            steps.append(_step("产品绑定", False,
                               "还没有任何仓库：.env 未填 SVN_REPO_URL，也没有 bind-repo 绑定"))
            return {"ok": False, "steps": steps, "hint": "先配置至少一个代码库"}
    else:
        targets = [require_target(product_id=product_id) if product_id else
                   resolve_target(product_id=product_id)]

    for target in targets:
        label = target.product_name or (f"产品 {target.product_id}" if target.product_id else "全局默认")
        sub = preflight_target(target)
        for item in sub:
            steps.append({"name": f"{label} · {item['name']}", "ok": item["ok"],
                          "detail": item["detail"]})

    ok = all(s["ok"] is not False for s in steps)
    return {"ok": ok, "steps": steps,
            "hint": "配置可用，AI 可以按产品建分支并提交" if ok else "有阻塞项，按上面 FAIL 处理"}
