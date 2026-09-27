"""Flask web application: kanban / sync / need-solution / review / svn views."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from flask import (Flask, abort, flash, jsonify, redirect, render_template,
                   request, send_from_directory, url_for)

from . import db, svn_client, sync
from .config import PROJECT_ROOT, get_settings, reload_settings
from .zentao_client import ZentaoError
from .zentao_session import make_client

STATUS_LABELS = {
    "pending": "待处理",
    "fixing": "修复中",
    "need_solution": "需方案",
    "await_review": "待审查",
    "rejected": "已打回",
    "merged": "已合入",
    "closed": "已结案",
}

# Kanban shows three stacked bands instead of one column per status:
# (key, label, hint, statuses) in the order the owner reads them.
KANBAN_BANDS = (
    ("mine", "要我处理", "需要你答复或拍板", ("need_solution", "await_review", "rejected")),
    ("ai", "AI 在跑", "等 AI 出结果", ("pending", "fixing")),
    ("done", "已收尾", "已合入 / 已结案，只作回溯", ("merged", "closed")),
)


def create_app() -> Flask:
    app = Flask(__name__)
    app.secret_key = "bug-system-local"
    db.init_db()

    # ------------------------------------------------------------------
    # template helpers
    # ------------------------------------------------------------------
    def _rev_label(value: Any) -> str:
        """SVN numbers get the r prefix; git:/PENDING/DRYRUN- stay verbatim."""
        text = str(value or "")
        return "r" + text if text.isdigit() else text

    app.jinja_env.filters["rev_label"] = _rev_label

    @app.context_processor
    def inject_helpers() -> dict[str, Any]:
        settings = get_settings()
        counts = db.counts_by_status()
        return {
            "status_labels": STATUS_LABELS,
            "settings_view": settings.describe(),
            "zentao_ready": settings.zentao_ready,
            "svn_ready": settings.svn_ready,
            "counts": counts,
            "total_bugs": sum(counts.values()),
            "last_sync": db.last_sync(),
            "project_root": str(PROJECT_ROOT),
        }

    def _form() -> dict[str, str]:
        return {k: (v.strip() if isinstance(v, str) else v) for k, v in request.form.items()}

    # ------------------------------------------------------------------
    # 1) kanban
    # ------------------------------------------------------------------
    @app.route("/")
    def kanban():
        return render_template("kanban.html", bands=_kanban_bands())

    @app.route("/api/bug/<int:bug_id>")
    def api_bug(bug_id: int):
        bug = db.get_bug(bug_id)
        if bug is None:
            abort(404)
        return jsonify(bug)

    # ------------------------------------------------------------------
    # 2) zentao sync
    # ------------------------------------------------------------------
    @app.route("/sync")
    def sync_page():
        return render_template(
            "sync.html",
            logs=db.list_sync_log(40),
            summary=_sync_summary(),
        )

    @app.route("/sync", methods=["POST"])
    def sync_run():
        try:
            result = sync.sync_now()
        except (ZentaoError, RuntimeError) as exc:
            flash(f"同步失败：{exc}", "error")
            return redirect(url_for("sync_page"))
        if result["errors"]:
            flash("同步出现错误：" + " | ".join(result["errors"])[:500], "error")
        else:
            flash(
                f"同步完成：新增 {result['created']} 条，更新 {result['updated']} 条，"
                f"共 {result['total']} 条待修 bug。",
                "ok",
            )
        return redirect(url_for("sync_page"))

    @app.route("/sync/doctor")
    def sync_doctor():
        """Step-by-step read-only diagnosis of the configured Zentao channel."""
        settings = get_settings()
        if not settings.zentao_base_url:
            report = {
                "ok": False,
                "steps": [{"name": "配置", "ok": False, "detail": "ZENTAO_BASE_URL 未填写，请编辑 .env"}],
                "hint": "先配置禅道地址再诊断",
            }
        else:
            try:
                client = make_client()
            except Exception as exc:  # show any failure inside the page, never a 500
                client = None
                report = {
                    "ok": False,
                    "steps": [{"name": "配置", "ok": False, "detail": f"{type(exc).__name__}: {exc}"}],
                    "hint": "配置不完整或客户端初始化失败",
                }
            if client is not None:
                report = client.probe()
        return render_template(
            "sync.html", logs=db.list_sync_log(40), summary=_sync_summary(), doctor=report
        )

    @app.route("/sync/details", methods=["POST"])
    def sync_details():
        """Bulk pull screenshots / notes / attachments; limit 0 means everything."""
        raw = _form().get("limit", "0")
        limit = int(raw) if str(raw).isdigit() and int(raw) > 0 else None
        try:
            result = sync.pull_details(limit=limit)
        except (ZentaoError, RuntimeError) as exc:
            flash(f"批量抓取失败：{exc}", "error")
            return redirect(url_for("sync_page"))
        scope = "全部待处理 bug" if limit is None else f"优先级最高的 {limit} 条 bug"
        if result.get("ok"):
            flash(
                f"{scope} 详情抓取完成：成功 {result['done']} 条，"
                f"图片 {result['images_saved']} 张，备注 {result['comments']} 条。",
                "ok",
            )
        else:
            flash(
                f"{scope} 详情抓取：成功 {result['done']} 条，失败 {result['failed']} 条；"
                + " | ".join(f"#{e['zentao_id']} {str(e['error'])[:80]}"
                             for e in (result.get("errors") or [])[:3]),
                "error",
            )
        return redirect(url_for("sync_page"))

    # ------------------------------------------------------------------
    # 3) need solution list
    # ------------------------------------------------------------------
    @app.route("/need")
    def need_page():
        status = request.args.get("status", "awaiting")
        if status not in {"awaiting", "replied", "done", "all"}:
            status = "awaiting"
        return render_template("need_solution.html", needs=db.list_needs(status), current=status)

    @app.route("/need/<int:need_id>/reply", methods=["POST"])
    def need_reply(need_id: int):
        reply = _form().get("owner_reply", "")
        if not reply:
            flash("答复不能为空。", "error")
            return redirect(url_for("need_page"))
        if db.reply_need(need_id, reply) is None:
            abort(404)
        flash("已记录答复，AI 下次运行会优先处理该 bug。", "ok")
        return redirect(request.referrer or url_for("need_page"))

    @app.route("/need/<int:need_id>/done", methods=["POST"])
    def need_done(need_id: int):
        if db.close_need(need_id) is None:
            abort(404)
        flash("该阻塞项已标记为处理完成。", "ok")
        return redirect(url_for("need_page", status="all"))

    # ------------------------------------------------------------------
    # 4) review list
    # ------------------------------------------------------------------
    @app.route("/review")
    def review_page():
        return render_template("review.html", queue=db.list_review_queue())

    @app.route("/review/<int:bug_id>/pass", methods=["POST"])
    def review_pass(bug_id: int):
        bug = db.get_bug(bug_id)
        if bug is None:
            abort(404)
        merged = _form().get("merged_revision", "")
        review = db.record_review(bug_id, "pass", merged_revision=merged)
        note = "人工审查通过，已合入 trunk"
        if merged:
            note += f" r{merged}"
        result = sync.push_comment(bug["zentao_id"], f"{note}。")
        flash(
            f"bug #{bug['zentao_id']} 已标记为已合入（trunk r{merged or '未填写'}）。"
            + ("" if result["ok"] else f" 禅道评论未写入：{result['detail'][:120]}"),
            "ok",
        )
        _ = review
        return redirect(url_for("review_page"))

    @app.route("/review/<int:bug_id>/reject", methods=["POST"])
    def review_reject(bug_id: int):
        bug = db.get_bug(bug_id)
        if bug is None:
            abort(404)
        reason = _form().get("reject_reason", "")
        if not reason:
            flash("打回必须填写原因。", "error")
            return redirect(url_for("review_page"))
        db.record_review(bug_id, "reject", reject_reason=reason)
        sync.push_comment(bug["zentao_id"], f"人工审查打回，原因：{reason}")
        flash(f"bug #{bug['zentao_id']} 已打回，AI 下次运行会重新处理。", "ok")
        return redirect(url_for("review_page"))

    @app.route("/bug/<int:bug_id>/close", methods=["POST"])
    def bug_close(bug_id: int):
        """Human-only step: mark locally closed after the trunk merge."""
        bug = db.get_bug(bug_id)
        if bug is None:
            abort(404)
        db.set_bug_status(bug_id, "closed")
        result = sync.push_comment(
            bug["zentao_id"],
            "已合入 trunk 并完成人工确认，本地系统标记为已结案（禅道状态请主人手动更新）。",
        )
        flash(
            f"bug #{bug['zentao_id']} 已标记为已结案。"
            + ("" if result["ok"] else f" 禅道评论未写入：{result['detail'][:120]}"),
            "ok",
        )
        return redirect(request.referrer or url_for("kanban"))

    @app.route("/bug/<int:bug_id>/detail", methods=["POST"])
    def bug_refresh_detail(bug_id: int):
        """Pull screenshots / notes / attachments for one bug."""
        bug = db.get_bug(bug_id)
        if bug is None:
            abort(404)
        result = sync.refresh_detail(bug["zentao_id"])
        if result.get("ok"):
            flash(
                f"bug #{bug['zentao_id']} 详情已更新：{result['steps_length']} 字描述、"
                f"{result['comments']} 条备注、图片 {result['images_saved']}/{result['images']} 张已下载",
                "ok",
            )
        else:
            flash(f"详情抓取失败：{result.get('detail')}", "error")
        return redirect(request.referrer or url_for("kanban"))

    @app.route("/files/<int:zentao_id>/<path:filename>")
    def attachment_file(zentao_id: int, filename: str):
        """Serve a downloaded Zentao attachment to the browser."""
        directory = Path(get_settings().attachment_dir) / f"zentao-{zentao_id}"
        if not directory.is_dir():
            abort(404)
        return send_from_directory(directory, filename)

    # ------------------------------------------------------------------
    # 5) svn revisions
    # ------------------------------------------------------------------
    @app.route("/svn")
    def svn_page():
        return render_template("svn.html", groups=db.list_revisions_by_bug())

    @app.route("/svn/check")
    def svn_check():
        """Read-only SVN self test, shown on the SVN page."""
        check_all = request.args.get("all") in ("1", "true", "yes")
        product = request.args.get("product")
        product_id = int(product) if product and str(product).isdigit() else None
        try:
            check = svn_client.preflight(product_id=product_id, all_targets=check_all)
        except Exception as exc:  # never 500 on a diagnostic click
            check = {"ok": False, "steps": [{"name": "自检", "ok": False, "detail": str(exc)}],
                     "hint": "自检过程异常"}
        return render_template("svn.html", groups=db.list_revisions_by_bug(), check=check)

    # ------------------------------------------------------------------
    # 6) product -> repository bindings
    # ------------------------------------------------------------------
    @app.route("/repos")
    def repos_page():
        rows = _repo_rows()
        # Prefill payloads for the bind dialog: keyed by product id, "new" = defaults.
        forms = {"new": _repo_form(None)}
        for row in rows:
            forms[str(row["product_id"])] = _repo_form(row["product_id"])
        return render_template(
            "repos.html",
            rows=rows,
            forms=forms,
            global_repo=svn_client.global_target(),
            products=db.products_in_use(),
        )

    @app.route("/repos/bind", methods=["POST"])
    def repos_bind():
        form = _form()
        raw_id = form.get("product_id", "")
        if not str(raw_id).isdigit():
            flash("产品 ID 必须是数字。", "error")
            return redirect(url_for("repos_page") + "#/repo/new")
        fields = {
            "product_name": form.get("product_name", ""),
            "repo_url": form.get("repo_url", ""),
            "trunk_path": form.get("trunk_path") or "/trunk",
            "branch_root": form.get("branch_root") or "/branches",
            "working_copy": form.get("working_copy", ""),
            "branch_prefix": form.get("branch_prefix", ""),
            "build_command": form.get("build_command", ""),
            "test_command": form.get("test_command", ""),
            "note": form.get("note", ""),
            "enabled": 0 if form.get("enabled") in ("0", "off", "", None) else 1,
        }
        try:
            db.bind_product_repo(int(raw_id), fields)
        except ValueError as exc:
            flash(str(exc), "error")
            return redirect(url_for("repos_page") + f"#/repo/{raw_id}")
        flash(f"产品 {raw_id} 的代码库已保存，AI 建分支/提交会自动使用该仓库。", "ok")
        return redirect(url_for("repos_page"))

    @app.route("/repos/<int:product_id>/unbind", methods=["POST"])
    def repos_unbind(product_id: int):
        db.unbind_product_repo(product_id)
        flash(f"产品 {product_id} 已解绑，回落到 .env 的全局 SVN 配置。", "ok")
        return redirect(url_for("repos_page"))

    # ------------------------------------------------------------------
    # config / usage
    # ------------------------------------------------------------------
    @app.route("/config")
    def config_page():
        return render_template("config.html", info=get_settings().describe())

    @app.route("/config/reload")
    def config_reload():
        reload_settings()
        db.init_db()
        flash("已重新读取 .env 配置。", "ok")
        return redirect(url_for("config_page"))

    @app.route("/healthz")
    def healthz():
        return jsonify({"ok": True, "db": str(db.db_path()), "counts": db.counts_by_status()})

    @app.errorhandler(404)
    def not_found(_e):
        return render_template("error.html", code=404, message="页面不存在"), 404

    return app


def _kanban_bands() -> list[dict[str, Any]]:
    """Statuses folded into the three bands the owner works through."""
    columns = {col["status"]: col for col in db.kanban_columns()}
    bands: list[dict[str, Any]] = []
    for key, label, hint, statuses in KANBAN_BANDS:
        groups = [columns[s] for s in statuses if columns.get(s) and columns[s]["count"]]
        bands.append({
            "key": key,
            "label": label,
            "hint": hint,
            "groups": groups,
            "total": sum(group["count"] for group in groups),
        })
    return bands


def _repo_form(product_id: int | None) -> dict[str, Any]:
    """Prefill values for the bind form: existing binding, else global defaults."""
    settings = get_settings()
    binding = db.get_product_repo(product_id) if product_id else None
    binding = binding or {}
    name = ""
    if product_id and not binding.get("product_name"):
        row = db.query_one(
            "SELECT MAX(COALESCE(product_name, '')) AS name FROM bugs WHERE product_id = ?",
            (int(product_id),),
        )
        name = (row or {}).get("name") or ""
    return {
        "product_id": product_id or "",
        "product_name": binding.get("product_name") or name,
        "repo_url": binding.get("repo_url") or settings.svn_repo_url,
        "trunk_path": binding.get("trunk_path") or "/trunk",
        "branch_root": binding.get("branch_root") or "/branches",
        "working_copy": binding.get("working_copy") or "",
        "branch_prefix": binding.get("branch_prefix") or settings.branch_prefix,
        "build_command": binding.get("build_command") or "",
        "test_command": binding.get("test_command") or "",
        "note": binding.get("note") or "",
        "enabled": int(binding.get("enabled", 1) or 0),
    }


def _repo_row(product_id: int, name: str, bugs: int, pending: int,
              binding: dict | None) -> dict[str, Any]:
    """One table row: what is stored for the product and what actually applies."""
    binding = binding or {}
    error = ""
    try:
        target = svn_client.resolve_target(product_id=product_id)
    except svn_client.SvnError as exc:
        target = None
        error = str(exc)
    return {
        "product_id": product_id,
        "product_name": binding.get("product_name") or name,
        "bugs": bugs,
        "pending": pending,
        "bound": bool(binding),
        "enabled": int(binding.get("enabled", 1) or 0) if binding else 1,
        "repo_url": binding.get("repo_url") or "",
        "trunk_path": binding.get("trunk_path") or "",
        "working_copy": binding.get("working_copy") or "",
        "build_command": binding.get("build_command") or "",
        "test_command": binding.get("test_command") or "",
        "source": target.source if target else "",
        "effective_trunk": target.trunk_url() if target else "",
        "effective_wc": str(target.working_copy) if target else "",
        "error": error,
    }


def _repo_rows() -> list[dict[str, Any]]:
    """Every Zentao product seen in the bug table, joined with its binding."""
    bindings = {int(row["product_id"]): row for row in db.list_product_repos()}
    rows: list[dict[str, Any]] = []
    seen: set[int] = set()
    for product in db.products_in_use():
        pid = int(product["product_id"])
        seen.add(pid)
        rows.append(_repo_row(pid, product["product_name"] or "", int(product["bugs"]),
                              int(product["pending"] or 0), bindings.get(pid)))
    for pid, binding in bindings.items():
        if pid in seen:
            continue
        count = db.query_one(
            "SELECT COUNT(*) AS bugs FROM bugs WHERE product_id = ?", (pid,)
        ) or {}
        rows.append(_repo_row(pid, binding.get("product_name") or "", int(count.get("bugs") or 0),
                              0, binding))
    rows.sort(key=lambda item: (-item["bugs"], item["product_id"]))
    return rows


def _sync_summary() -> dict[str, Any]:
    """Numbers for the sync page header."""
    counts = db.counts_by_status()
    last = db.last_sync()
    today = db.now_str()[:10]
    rows = db.query_all(
        "SELECT action, payload FROM sync_log WHERE substr(created_at,1,10)=? ORDER BY id DESC",
        (today,),
    )
    created = updated = 0
    for row in rows:
        try:
            payload = json.loads(row["payload"] or "{}")
        except json.JSONDecodeError:
            continue
        if row["action"] == "update":
            if payload.get("action") == "created":
                created += 1
            elif payload.get("action") == "updated":
                updated += 1
    open_rows = db.query_all(
        "SELECT detail_synced_at FROM bugs WHERE status IN "
        "('pending','fixing','rejected','need_solution','await_review')"
    )
    missing = sum(1 for row in open_rows if not row["detail_synced_at"])
    return {
        "total_bugs": sum(counts.values()),
        "pending": counts.get("pending", 0),
        "created_today": created,
        "updated_today": updated,
        "details_missing": missing,
        "details_total": len(open_rows),
        "last_sync": last,
    }


app = create_app()

if __name__ == "__main__":  # pragma: no cover - manual entry point
    settings = get_settings()
    print(f"[bug-system] SQLite : {db.db_path()}")
    print(f"[bug-system] Web    : http://{settings.host}:{settings.port}")
    app.run(host=settings.host, port=settings.port, debug=settings.debug, use_reloader=False)
