"""Orchestration layer used by both the web UI and the AI executor CLI.

* :func:`sync_now`        pull assigned active bugs (and tasks) from Zentao
* :func:`push_comment`    write a comment back to Zentao (never resolve/close)

Both Zentao object kinds run through the same functions; ``target`` picks which
table, which endpoint and which comment route to use.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any

from . import db
from . import imgsize
from .config import get_settings
from .zentao_client import ZentaoError
from .zentao_session import make_client

COMMIT_TEMPLATE = "AI 已处理分支 {branch}：{revision_note} 待人工审查。{extra}"
BLOCK_TEMPLATE = "AI 阻塞，需要主人决策。\n问题：{question}\n可选方案：{options}\nAI 建议：{advice}"
PLAN_TEMPLATE = ("AI 已给出任务方案（修改细则），未动代码，等主人确认。\n"
                 "需求理解：{question}\n可选方案：{options}\nAI 建议：{advice}\n"
                 "确认或改细则后，AI 才会落码提交。")
REVIEW_PASS_TEMPLATE = "人工审查通过，已合入 trunk r{revision}，请主人/测试在禅道确认结案。"

# Extensions treated as pictures when reporting how much was downloaded.
IMAGE_EXTS = {"png", "jpg", "jpeg", "gif", "bmp", "webp"}


def sync_now(client: ZentaoClient | None = None,
             kinds: tuple[str, ...] = db.ITEM_KINDS) -> dict[str, Any]:
    """Fetch items assigned to me and upsert them into their own tables."""
    settings = get_settings()
    settings.require_zentao()
    client = client or make_client()
    result: dict[str, Any] = {
        "created": 0, "updated": 0, "skipped": 0,
        "products": [], "errors": [], "total": 0,
        "by_kind": {},
    }
    try:
        if settings.zentao_product_ids:
            product_ids = list(settings.zentao_product_ids)
            result["products"] = product_ids
        else:
            products = client.list_products()
            product_ids = [p["id"] for p in products]
            result["products"] = product_ids
            db.log_sync("sync", None, {"stage": "products", "products": products})
        if not product_ids:
            raise ZentaoError("禅道产品列表为空，请检查账号权限或设置 ZENTAO_PRODUCT_IDS")
    except (ZentaoError, RuntimeError) as exc:
        db.log_sync("error", None, {"message": str(exc)})
        result["errors"].append(str(exc))
        db.set_meta("last_sync_result", str(exc))
        return result

    for target in kinds:
        try:
            rows = (client.fetch_assigned_bugs(product_ids) if target == "bug"
                    else client.fetch_assigned_tasks(product_ids))
        except (ZentaoError, RuntimeError) as exc:
            # One kind must not take the other down with it: a build without the
            # task module, or a paging hiccup, would otherwise hide every bug.
            db.log_sync("error", None, {"kind": target, "message": str(exc)})
            result["errors"].append(f"{target}: {exc}")
            result["by_kind"][target] = {"created": 0, "updated": 0, "skipped": 0,
                                         "total": 0, "error": str(exc)}
            continue
        counts = {"created": 0, "updated": 0, "skipped": 0, "total": len(rows)}
        for row in rows:
            if not row.get("zentao_id"):
                counts["skipped"] += 1
                continue
            if target == "bug":
                item_id, action, zentao_id = db.upsert_bug_from_zentao(row)
            else:
                item_id, action, zentao_id = db.upsert_task_from_zentao(row)
            counts[action] += 1
            db.log_sync("update", zentao_id, {
                "action": action,
                "kind": target,
                "item_id": item_id,
                "title": row.get("title"),
                "status": row.get("zentao_status"),
                "pri": row.get("pri"),
                "severity": row.get("severity"),
                "assigned_to": row.get("assigned_to"),
                "opened_build": row.get("opened_build"),
                "project_name": row.get("project_name"),
                "product_id": row.get("product_id"),
            })
        for key in ("created", "updated", "skipped", "total"):
            result[key] += counts[key]
        result["by_kind"][target] = counts

    db.set_meta("last_sync_at", db.now_str())
    db.log_sync("sync", None, {
        "stage": "done",
        "created": result["created"],
        "updated": result["updated"],
        "total": result["total"],
        "by_kind": result["by_kind"],
    })
    db.set_meta("last_sync_result", "ok" if not result["errors"] else "；".join(result["errors"]))
    return result


def refresh_detail(zentao_id: int, *, download: bool | None = None,
                   client: Any = None, target: str = "bug") -> dict[str, Any]:
    """Pull one item's full detail: screenshots, notes ("备注"), attachments.

    The list sync only gets the raw steps HTML, so this is what makes an image
    or a reviewer note visible to the AI and to the review page.
    """
    settings = get_settings()
    settings.require_zentao()
    client = client or make_client()
    getter = getattr(client, "get_bug_detail" if target == "bug" else "get_task_detail",
                     None)
    if getter is None:
        return {"ok": False, "detail": "当前禅道通道不支持详情抓取"}
    item = db.get_item_by_zentao_id(zentao_id, target)
    if item is None:
        return {"ok": False, "detail": f"库里没有禅道 {db.item_slug(zentao_id, target)}，请先同步"}
    fields = getter(zentao_id, download=download) if download is not None else getter(zentao_id)
    fields.pop("zentao_id", None)
    # A detail payload that did not carry a value must not erase what the list sync
    # already stored: Zentao answers `/task-view-*` with an empty product name for a
    # task whose project has no product node, and that is "not shown", not "none".
    fields = {k: v for k, v in fields.items() if v is not None and v != ""}
    updated = db.update_item_detail(item["id"], fields, target)
    images = [a for a in fields.get("attachments", []) if str(a.get("ext", "")).lower() in IMAGE_EXTS]
    saved = [a for a in images if a.get("local_path")]
    failed = [a for a in images if a.get("error")]
    skipped = [a for a in images if a.get("unreadable")]
    db.log_sync("update", zentao_id, {
        "action": "detail",
        "kind": target,
        "comments": len(fields.get("comments") or []),
        "images": len(images),
        "saved": len(saved),
        "failed": len(failed),
        "unreadable": len(skipped),
    })
    return {
        "ok": True,
        "zentao_id": zentao_id,
        "target": target,
        "steps_length": len(fields.get("steps") or ""),
        "comments": len(fields.get("comments") or []),
        "images": len(images),
        "images_saved": len(saved),
        "images_unreadable": [f"{a.get('file_id')}: {a.get('unreadable')}" for a in skipped],
        "images_failed": [f"{a.get('file_id')}: {a.get('error')}" for a in failed],
        "module": fields.get("module"),
        "product_name": fields.get("product_name"),
        "assigned_to_name": fields.get("assigned_to_name"),
        "bug": updated,
    }


def pull_details(limit: int | None = None, *, download: bool | None = None,
                 statuses: tuple[str, ...] = ("pending", "fixing", "rejected",
                                              "need_solution", "await_review"),
                 client: Any = None,
                 targets: tuple[str, ...] = db.ITEM_KINDS) -> dict[str, Any]:
    """Bulk-refresh item details (screenshots / notes / attachments).

    `limit` selects how many items, ordered by the same priority the executor
    uses; ``None`` or ``0`` means every item in those statuses.
    """
    settings = get_settings()
    settings.require_zentao()
    client = client or make_client()
    if getattr(client, "get_bug_detail", None) is None:
        return {"ok": False, "detail": "当前禅道通道不支持详情抓取"}

    placeholders = ", ".join("?" for _ in statuses)
    targets = tuple(targets) or db.ITEM_KINDS
    rows: list[dict] = []
    for target in targets:
        sql = (f"SELECT zentao_id, '{target}' AS target FROM {tables_of(target)}"
               f" WHERE status IN ({placeholders}) ORDER BY pri ASC, id ASC")
        if limit:
            sql += " LIMIT ?"
            rows.extend(db.query_all(sql, tuple(statuses) + (int(limit),)))
        else:
            rows.extend(db.query_all(sql, statuses))

    done = failed = images = notes = 0
    errors: list[dict] = []
    for row in rows:
        result = refresh_detail(row["zentao_id"], download=download, client=client,
                                target=row["target"])
        if result.get("ok"):
            done += 1
            images += int(result.get("images_saved") or 0)
            notes += int(result.get("comments") or 0)
        else:
            failed += 1
            errors.append({"zentao_id": row["zentao_id"], "kind": row["target"],
                           "error": result.get("detail")})
    return {
        "ok": failed == 0,
        "target": len(rows),
        "done": done,
        "failed": failed,
        "images_saved": images,
        "comments": notes,
        "errors": errors[:20],
    }


def tables_of(target: str) -> str:
    """Table name of one item kind (kept here so SQL strings stay readable)."""
    return db.tables_for(target).item


def gate_images(apply: bool = True) -> dict[str, Any]:
    """Re-measure every stored attachment without touching Zentao.

    Screenshots downloaded before the size gate existed are already on disk, and
    re-fetching them needs a live session. The header is all the answer needs, so
    this walks the JSON already in the database instead.
    """
    rows: list[dict] = []
    for target in db.ITEM_KINDS:
        found = db.query_all(
            f"SELECT id, zentao_id, attachments, '{target}' AS target FROM {tables_of(target)}"
            " WHERE attachments IS NOT NULL AND attachments != ''"
        )
        rows.extend(found)
    changed = flagged = 0
    for row in rows:
        items = db._json_load(row.get("attachments")) or []
        if not isinstance(items, list) or not items:
            continue
        touched = False
        for item in items:
            path = str(item.get("local_path") or "")
            if not path or not Path(path).exists():
                continue
            pixels, reason = imgsize.gate(path, str(item.get("ext") or ""))
            if pixels and item.get("pixels") != pixels:
                item["pixels"] = pixels
                touched = True
            if reason and not item.get("unreadable"):
                item["unreadable"] = reason
                touched = True
                flagged += 1
            elif not reason and item.get("unreadable"):
                item.pop("unreadable", None)
                touched = True
        if touched:
            changed += 1
            if apply:
                db.execute(f"UPDATE {tables_of(row['target'])} SET attachments = ? WHERE id = ?",
                           (db._json_dump(items), row["id"]))
    return {"ok": True, "bugs_scanned": len(rows), "bugs_updated": changed,
            "pictures_flagged": flagged, "applied": apply}


def reconcile_drafts(apply: bool = False, zentao_id: int | None = None,
                     statuses: tuple[str, ...] = ("fixing", "pending", "rejected"),
                     targets: tuple[str, ...] = db.ITEM_KINDS) -> dict[str, Any]:
    """Match the database against the draft branches that actually exist.

    A run that ends without calling ``commit`` leaves its item stuck in ``fixing``
    even though the work is already sitting on its ``bugfix/zentao-<ID>`` branch (a
    task's is ``bugfix/zentao-T<ID>``). Trusting the executor to report is not
    enough, so this reads the mirror and names every branch whose commits were
    never registered here. ``apply`` adopts them: the draft commit is recorded as
    ``git:<hash>`` and the item moves to ``await_review``, which is where a
    finished-but-unreported fix belongs.
    """
    from . import svn_promote

    found: list[dict[str, Any]] = []
    skipped: list[dict[str, Any]] = []
    scanned = 0
    placeholders = ", ".join("?" for _ in statuses)
    for target in tuple(targets) or db.ITEM_KINDS:
        sql = (f"SELECT *, '{target}' AS target FROM {tables_of(target)}"
               f" WHERE status IN ({placeholders}) ")
        params: list[Any] = list(statuses)
        if zentao_id:
            sql += " AND zentao_id = ?"
            params.append(int(zentao_id))
        sql += " ORDER BY pri ASC, id ASC"
        rows = db.query_all(sql, tuple(params))
        scanned += len(rows)
        for row in rows:
            bug = db.decorate_item(row, target)
            try:
                state = svn_promote.draft_state(bug)
            except Exception as exc:  # noqa: BLE001 - one unreachable mirror must not stop the scan
                skipped.append({"zentao_id": bug["zentao_id"], "kind": target,
                                "why": f"探测失败：{exc}"})
                continue
            if state.error:
                skipped.append({"zentao_id": bug["zentao_id"], "kind": target,
                                "why": state.error})
                continue
            if not state.present or state.ahead <= 0:
                continue
            known = {str(r.get("git_commit") or "") for r in bug["revisions"]}
            known |= {str(r.get("revision") or "").replace("git:", "")[:10]
                      for r in bug["revisions"]}
            if state.short_tip in known or state.tip in known:
                skipped.append({"zentao_id": bug["zentao_id"], "kind": target,
                                "why": f"{state.short_tip} 已登记过，无需认领"})
                continue
            item = {
                "zentao_id": bug["zentao_id"],
                "kind": target,
                "status": bug["status"],
                "branch": state.branch,
                "tip": state.short_tip,
                "subject": state.subject,
                "author": state.author,
                "when": state.when,
                "ahead": state.ahead,
                "commits": state.commits,
                "files": [f.split(" ", 1)[-1] for f in state.files],
            }
            if apply:
                paths = item["files"]
                db.add_revision(
                    bug["id"], f"git:{state.short_tip}", branch=state.branch,
                    message=state.subject[:500], author=state.author,
                    files=paths, git_commit=state.tip, target=target)
                db.set_item_status(
                    bug["id"], "await_review", target=target,
                    branch=state.branch, files_changed=paths,
                    fix_summary=(f"由对账认领：镜像分支 {state.branch} 上有 {state.ahead} 笔提交"
                                 f"（{state.short_tip}），但执行器没有回登记。"
                                 f"提交说明：{state.subject}"[:500]),
                    verify_steps=("这条的结论来自 git 提交信息，不是执行器写的分析；"
                                  "审查前请自行核对 diff，别当成已验证"))
                db.add_analysis(
                    bug["id"],
                    {"conclusion": "对账认领：改动在镜像分支上，系统内原本无登记",
                     "change": state.subject[:500],
                     "evidence": f"git 分支 {state.branch} @ {state.short_tip}，"
                                 f"领先 {state.base_source} {state.ahead} 笔",
                     "impact": "、".join(paths)[:500] or "（未取到文件清单）",
                     "unverified": "未验证：认领动作不代跑编译与复现，全部结论待人工核对"},
                    kind="manual", target=target)
                item["applied"] = True
            found.append(item)
    return {"ok": True, "scanned": scanned, "apply": apply,
            "unregistered": found, "skipped": skipped}


def handoff_leftovers(root: Path | None = None) -> tuple[list[dict], list[dict]]:
    """Executor scratch files sitting in the project root, split by safety.

    An ``a_<ID>.json`` is the analysis the executor wrote for a bug. If that bug
    has no analysis in the database, the file is the only surviving copy of the
    conclusion, so it is held back and reported instead of swept.
    """
    from .config import PROJECT_ROOT

    base = Path(root) if root is not None else PROJECT_ROOT
    safe: list[dict] = []
    held: list[dict] = []
    try:
        entries = sorted(base.iterdir())
    except OSError:
        return safe, held
    for path in entries:
        if not path.is_file():
            continue
        name = path.name
        matched = re.match(r"^(?:a|analysis)_(T?\d+)\.json$", name)
        if matched:
            # `a_T5417.json` is a task's write-up; a bare number stays a bug.
            target, zentao = db.parse_ref(matched.group(1))
            bug = db.get_item_by_zentao_id(zentao, target)
            registered = bool(bug and db.has_analysis(bug["id"], target))
            item = {"file": name, "bytes": path.stat().st_size, "zentao_id": zentao,
                    "kind": target, "registered": registered}
            (safe if registered else held).append(item)
            continue
        if (re.match(r"^(?:blk|block|note|gate)_?\d*\.json$", name)
                or re.match(r"^py_\d+\.py$", name)
                or re.match(r"^_tmp_[\w-]*\.(?:py|out|txt|json)$", name)):
            safe.append({"file": name, "bytes": path.stat().st_size})
    return safe, held


def push_comment(zentao_id: int, text: str, *, client: ZentaoClient | None = None,
                 target: str = "bug") -> dict:
    """Write one comment to Zentao and log it; failures are returned, not raised."""
    settings = get_settings()
    slug = db.item_slug(zentao_id, target)
    if not settings.zentao_ready:
        db.log_sync("comment", zentao_id, {"skipped": "禅道未配置", "kind": target,
                                           "comment": text})
        return {"ok": False, "detail": "禅道未配置，评论只写入本地日志"}
    client = client or make_client()
    try:
        info = client.add_comment(zentao_id, text, target=target)
        db.log_sync("comment", zentao_id, {"comment": text, "kind": target,
                                           "endpoint": info.get("endpoint")})
        return {"ok": True, "detail": info}
    except (ZentaoError, RuntimeError) as exc:
        db.log_sync("error", zentao_id, {"comment_failed": str(exc), "kind": target,
                                         "comment": text})
        return {"ok": False, "detail": f"{slug}: {exc}"}


def revision_note(revision: str) -> str:
    """Describe a stored revision honestly: SVN number, local git draft, or nothing committed.

    Zentao readers must never infer from a draft hash that the fix is already in the SVN repo.
    """
    text = str(revision or "").strip()
    if text.isdigit():
        return f"SVN r{text}，"
    if text.startswith("git:"):
        return f"本地 git 提交 {text[4:]}（尚未进 SVN），"
    return "尚未提交（占位待人工提交），"


def comment_commit(zentao_id: int, branch: str, revision: str, extra: str = "",
                   *, target: str = "bug") -> dict:
    text = COMMIT_TEMPLATE.format(branch=branch, revision_note=revision_note(revision),
                                  extra=extra).strip()
    return push_comment(zentao_id, text, target=target)


def comment_block(zentao_id: int, question: str, options: str, advice: str,
                  *, target: str = "bug") -> dict:
    text = BLOCK_TEMPLATE.format(question=question, options=options or "（AI 暂无可选方案）",
                                 advice=advice or "（AI 暂无建议）")
    return push_comment(zentao_id, text, target=target)


def comment_plan(zentao_id: int, question: str, options: str, advice: str) -> dict:
    """Tell Zentao a task's proposal is up for approval and no code was written."""
    text = PLAN_TEMPLATE.format(question=question,
                                options=options or "（AI 暂无可选方案）",
                                advice=advice or "（AI 暂无建议）")
    return push_comment(zentao_id, text, target="task")


def comment_review_pass(zentao_id: int, merged_revision: str,
                        *, target: str = "bug") -> dict:
    return push_comment(zentao_id, REVIEW_PASS_TEMPLATE.format(revision=merged_revision),
                        target=target)
