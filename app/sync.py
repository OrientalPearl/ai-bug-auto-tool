"""Orchestration layer used by both the web UI and the AI executor CLI.

* :func:`sync_now`        pull assigned active bugs from Zentao into SQLite
* :func:`push_comment`    write a comment back to Zentao (never resolve/close)
"""

from __future__ import annotations

from typing import Any

from . import db
from .config import get_settings
from .zentao_client import ZentaoError
from .zentao_session import make_client

COMMIT_TEMPLATE = "AI 已提交分支 {branch}，SVN r{revision}，待人工审查。{extra}"
BLOCK_TEMPLATE = "AI 阻塞，需要主人决策。\n问题：{question}\n可选方案：{options}\nAI 建议：{advice}"
REVIEW_PASS_TEMPLATE = "人工审查通过，已合入 trunk r{revision}，请主人/测试在禅道确认结案。"

# Extensions treated as pictures when reporting how much was downloaded.
IMAGE_EXTS = {"png", "jpg", "jpeg", "gif", "bmp", "webp"}


def sync_now(client: ZentaoClient | None = None) -> dict[str, Any]:
    """Fetch bugs assigned to me and upsert them into the `bugs` table."""
    settings = get_settings()
    settings.require_zentao()
    client = client or make_client()
    result: dict[str, Any] = {
        "created": 0, "updated": 0, "skipped": 0,
        "products": [], "errors": [], "total": 0,
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
        bugs = client.fetch_assigned_bugs(product_ids)
    except (ZentaoError, RuntimeError) as exc:
        db.log_sync("error", None, {"message": str(exc)})
        result["errors"].append(str(exc))
        db.set_meta("last_sync_result", str(exc))
        return result

    for bug in bugs:
        if not bug.get("zentao_id"):
            result["skipped"] += 1
            continue
        bug_id, action, zentao_id = db.upsert_bug_from_zentao(bug)
        result[action] += 1
        db.log_sync("update", zentao_id, {
            "action": action,
            "bug_id": bug_id,
            "title": bug.get("title"),
            "status": bug.get("zentao_status"),
            "severity": bug.get("severity"),
            "pri": bug.get("pri"),
            "assigned_to": bug.get("assigned_to"),
            "opened_build": bug.get("opened_build"),
        })
    result["total"] = len(bugs)
    db.set_meta("last_sync_at", db.now_str())
    db.log_sync("sync", None, {
        "stage": "done",
        "created": result["created"],
        "updated": result["updated"],
        "total": result["total"],
    })
    db.set_meta("last_sync_result", "ok")
    return result


def refresh_detail(zentao_id: int, *, download: bool | None = None,
                   client: Any = None) -> dict[str, Any]:
    """Pull one bug's full detail: screenshots, notes ("备注"), attachments.

    The list sync only gets the raw steps HTML, so this is what makes an image
    or a reviewer note visible to the AI and to the review page.
    """
    settings = get_settings()
    settings.require_zentao()
    client = client or make_client()
    getter = getattr(client, "get_bug_detail", None)
    if getter is None:
        return {"ok": False, "detail": "当前禅道通道不支持详情抓取"}
    bug = db.get_bug_by_zentao_id(zentao_id)
    if bug is None:
        return {"ok": False, "detail": f"库里没有禅道 #{zentao_id}，请先同步"}
    fields = getter(zentao_id, download=download) if download is not None else getter(zentao_id)
    fields.pop("zentao_id", None)
    updated = db.update_bug_detail(bug["id"], fields)
    images = [a for a in fields.get("attachments", []) if str(a.get("ext", "")).lower() in IMAGE_EXTS]
    saved = [a for a in images if a.get("local_path")]
    failed = [a for a in images if a.get("error")]
    db.log_sync("update", zentao_id, {
        "action": "detail",
        "comments": len(fields.get("comments") or []),
        "images": len(images),
        "saved": len(saved),
        "failed": len(failed),
    })
    return {
        "ok": True,
        "zentao_id": zentao_id,
        "steps_length": len(fields.get("steps") or ""),
        "comments": len(fields.get("comments") or []),
        "images": len(images),
        "images_saved": len(saved),
        "images_failed": [f"{a.get('file_id')}: {a.get('error')}" for a in failed],
        "module": fields.get("module"),
        "product_name": fields.get("product_name"),
        "assigned_to_name": fields.get("assigned_to_name"),
        "bug": updated,
    }


def pull_details(limit: int | None = None, *, download: bool | None = None,
                 statuses: tuple[str, ...] = ("pending", "fixing", "rejected",
                                              "need_solution", "await_review"),
                 client: Any = None) -> dict[str, Any]:
    """Bulk-refresh bug details (screenshots / notes / attachments).

    `limit` selects how many bugs, ordered by the same priority the executor
    uses; ``None`` or ``0`` means every bug in those statuses.
    """
    settings = get_settings()
    settings.require_zentao()
    client = client or make_client()
    if getattr(client, "get_bug_detail", None) is None:
        return {"ok": False, "detail": "当前禅道通道不支持详情抓取"}

    placeholders = ", ".join("?" for _ in statuses)
    sql = (f"SELECT zentao_id FROM bugs WHERE status IN ({placeholders}) "
           "ORDER BY pri ASC, severity DESC, id ASC")
    if limit:
        sql += " LIMIT ?"
        rows = db.query_all(sql, tuple(statuses) + (int(limit),))
    else:
        rows = db.query_all(sql, statuses)

    done = failed = images = notes = 0
    errors: list[dict] = []
    for row in rows:
        result = refresh_detail(row["zentao_id"], download=download, client=client)
        if result.get("ok"):
            done += 1
            images += int(result.get("images_saved") or 0)
            notes += int(result.get("comments") or 0)
        else:
            failed += 1
            errors.append({"zentao_id": row["zentao_id"], "error": result.get("detail")})
    return {
        "ok": failed == 0,
        "target": len(rows),
        "done": done,
        "failed": failed,
        "images_saved": images,
        "comments": notes,
        "errors": errors[:20],
    }


def push_comment(zentao_id: int, text: str, *, client: ZentaoClient | None = None) -> dict:
    """Write one comment to Zentao and log it; failures are returned, not raised."""
    settings = get_settings()
    if not settings.zentao_ready:
        db.log_sync("comment", zentao_id, {"skipped": "禅道未配置", "comment": text})
        return {"ok": False, "detail": "禅道未配置，评论只写入本地日志"}
    client = client or ZentaoClient()
    try:
        info = client.add_comment(zentao_id, text)
        db.log_sync("comment", zentao_id, {"comment": text, "endpoint": info.get("endpoint")})
        return {"ok": True, "detail": info}
    except (ZentaoError, RuntimeError) as exc:
        db.log_sync("error", zentao_id, {"comment_failed": str(exc), "comment": text})
        return {"ok": False, "detail": str(exc)}


def comment_commit(zentao_id: int, branch: str, revision: str, extra: str = "") -> dict:
    text = COMMIT_TEMPLATE.format(branch=branch, revision=revision, extra=extra).strip()
    return push_comment(zentao_id, text)


def comment_block(zentao_id: int, question: str, options: str, advice: str) -> dict:
    text = BLOCK_TEMPLATE.format(question=question, options=options or "（AI 暂无可选方案）",
                                 advice=advice or "（AI 暂无建议）")
    return push_comment(zentao_id, text)


def comment_review_pass(zentao_id: int, merged_revision: str) -> dict:
    return push_comment(zentao_id, REVIEW_PASS_TEMPLATE.format(revision=merged_revision))
