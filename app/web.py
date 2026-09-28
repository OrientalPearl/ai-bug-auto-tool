"""Flask web application: kanban / sync / need-solution / review / svn views."""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

from flask import (Flask, abort, flash, jsonify, redirect, render_template,
                   request, send_from_directory, url_for)

from . import db, svn_client, svn_promote, sync
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

# Headings in AUTO_LOOP.md whose code block is meant to be pasted verbatim;
# the 下达 page renders them instead of duplicating the text.
LOOP_PROMPT_SECTIONS = (r"^## 0\.", r"^### 3\.1 ", r"^### 3\.2 ", r"^## 9\.")

# Kanban shows three stacked bands instead of one column per status:
# (key, label, hint, statuses) in the order the owner reads them.
KANBAN_BANDS = (
    ("mine", "要我处理", "需要你答复或拍板", ("need_solution", "await_review", "rejected")),
    ("ai", "AI 在跑", "等 AI 出结果", ("pending", "fixing")),
    ("done", "已收尾", "已合入 / 已结案，只作回溯", ("merged", "closed")),
)

# AUTO_LOOP.md sections the dispatch page can build a scoped prompt from.
DISPATCH_BASES = {
    "0": "§0 最短下达语",
    "3.1": "§3.1 主循环（调度器）",
}


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
        """Send the bug back for another round. The draft branch stays where it is:
        the next run continues on top of it and the eventual push carries every
        round's changes as one SVN commit."""
        bug = db.get_bug(bug_id)
        if bug is None:
            abort(404)
        reason = _form().get("reject_reason", "")
        if not reason:
            flash("打回必须填写原因。", "error")
            return redirect(url_for("review_page"))
        db.record_review(bug_id, "reject", reject_reason=reason)
        sync.push_comment(bug["zentao_id"], f"人工审查打回，原因：{reason}")
        flash(f"bug #{bug['zentao_id']} 已打回，AI 下次运行会在原分支 {bug.get('branch') or '（未建分支）'} "
              f"上继续修改（改动未回滚），最终推送时几轮改动会合成一次 svn 提交。", "ok")
        return redirect(url_for("review_page"))

    @app.route("/bug/<int:bug_id>/svn-push", methods=["POST"])
    def bug_svn_push(bug_id: int):
        """The owner's promotion click: turn this bug's draft into a real SVN revision.

        AUTO_LOOP.md G11 reserves every remote write for the owner, and this route
        is the only place the system performs one -- because a human asked for it
        and typed the log message itself. ``mode=dry`` runs the identical checks
        (mirror, base, trunk drift, pending list) without writing anywhere.
        """
        bug = db.get_bug(bug_id)
        if bug is None:
            abort(404)
        payload = _form()
        message = str(payload.get("message") or "").strip()
        dry = str(payload.get("mode") or "push") == "dry"
        inline = request.headers.get("X-Requested-With") == "fetch-dialog"

        def reply(data: dict[str, Any], level: str, text: str):
            """JSON for the dialog (it must stay open), flash+redirect otherwise."""
            if inline:
                return jsonify(data)
            flash(text, level)
            return redirect(url_for("review_page"))

        if bug["status"] != "await_review" and not dry:
            text = (f"bug #{bug['zentao_id']} 当前状态是"
                    f"「{STATUS_LABELS.get(bug['status'], bug['status'])}」，不是待审查，未推送。")
            return reply({"ok": False, "summary": text, "detail": text}, "error", text)

        try:
            prom = svn_promote.promote(bug, message, dry_run=dry)
        except svn_promote.PromoteError as exc:
            text = f"bug #{bug['zentao_id']} 推送被拒绝：{exc}"
            return reply({"ok": False, "summary": text, "detail": text}, "error", text)

        files = [item.split("|", 1)[1] for item in prom.want if "|" in item]
        # Several draft commits travel as one push: the diff is taken against the
        # merge base with origin/trunk, so the owner sees the whole set here.
        travelling = {"drafts": prom.drafts, "base": prom.base, "commit": prom.commit}
        if dry:
            clean = bool(prom.ready) and not prom.conflicts
            text = prom.detail()
            return reply({
                "ok": clean, "dry_run": True, "summary": prom.summary(), "detail": text,
                "conflicts": prom.conflicts, "files": files, "url": prom.url,
                "commit": prom.commit, "base": prom.base, "wc": prom.wc,
                **travelling,
            }, "ok" if clean else "error", text)

        if prom.ok:
            db.add_revision(bug["id"], prom.revision, branch=bug.get("branch") or "",
                            message=message,
                            author=get_settings().svn_username or "owner", files=files)
            db.record_review(bug["id"], "pass", merged_revision=prom.revision)
            note = (f"已由主人从审查页正式推入 SVN：{prom.url} r{prom.revision}"
                    f"（{len(files)} 个文件）。提交说明：{message}")
            result = sync.push_comment(bug["zentao_id"], note)
            text = (f"bug #{bug['zentao_id']} 已推入 SVN r{prom.revision}，"
                    f"状态改为已合入。"
                    + ("" if result["ok"] else f" 禅道评论未写入：{result['detail'][:120]}"))
            return reply({"ok": True, "summary": text, "detail": prom.detail(),
                          "revision": prom.revision, "status": "merged",
                          "files": files, **travelling}, "ok", text)

        # Nothing landed. That is a push failure, not a verdict on the fix: the bug
        # stays exactly where it was (await_review), the git:<hash> draft record
        # stays untouched, and only a trail is written. Re-trying, or deliberately
        # sending it back to the AI, remains the owner's call.
        reason = prom.detail()
        db.add_analysis(
            bug["id"],
            {
                "conclusion": "推入 SVN 未成功（留痕）：改动与状态都未变动",
                "symptom": f"点击「推 SVN」失败：{prom.error or '未知原因'}",
                "evidence": (prom.raw or "")[-1200:],
                "impact": "trunk 未被改动（远端写入未发生或已回退）；镜像草稿分支未动",
                "verify": "推送清单：" + ("、".join(files) if files else "无"),
                "unverified": "失败原因见 evidence 原始输出；修好原因后可直接重推",
                "rollback": "无远端写入，无需回退",
            },
            kind="manual", author="owner",
        )
        sync.push_comment(
            bug["zentao_id"],
            f"人工推入 SVN 未成功（仅留痕，状态仍是待审查，AI 草稿未变）：{reason[:300]}",
        )
        text = (f"bug #{bug['zentao_id']} 推送失败：已留痕，状态仍是"
                f"「{STATUS_LABELS.get(bug['status'], bug['status'])}」，git 草稿记录未删。"
                f"可直接重试，或你判定后手工打回。")
        return reply({"ok": False, "summary": text, "detail": reason,
                      "conflicts": prom.conflicts, "status": bug["status"],
                      "unchanged": True}, "error", text)

    @app.route("/bug/<int:bug_id>/reject-rollback", methods=["POST"])
    def bug_reject_rollback(bug_id: int):
        """Refuse the fix: roll the draft branch back and close the bug.

        The opposite of the promotion button. It is only allowed to delete this
        bug's own ``bugfix/*`` draft branch (the commit is parked under
        ``refs/rejected/`` first, so the owner can get it back); if the mirror
        refuses -- typically because another session has HEAD on that branch --
        nothing is changed on the bug either.
        """
        bug = db.get_bug(bug_id)
        if bug is None:
            abort(404)
        payload = _form()
        reason = str(payload.get("reject_reason") or "").strip()
        inline = request.headers.get("X-Requested-With") == "fetch-dialog"

        def reply(data: dict[str, Any], level: str, text: str):
            if inline:
                return jsonify(data)
            flash(text, level)
            return redirect(url_for("review_page"))

        if bug["status"] not in ("await_review", "rejected"):
            text = (f"bug #{bug['zentao_id']} 当前状态是"
                    f"「{STATUS_LABELS.get(bug['status'], bug['status'])}」，"
                    "不是待审查/已打回，未回滚。")
            return reply({"ok": False, "summary": text, "detail": text}, "error", text)
        if not reason:
            text = f"bug #{bug['zentao_id']} 回滚必须写清原因（这是不可逆动作的留痕）。"
            return reply({"ok": False, "summary": text, "detail": text}, "error", text)

        done = svn_promote.rollback_draft(bug)
        if not done.ok:
            text = f"bug #{bug['zentao_id']} 回滚失败：{done.summary()}"
            return reply({"ok": False, "summary": text, "detail": done.raw[-600:],
                          "status": bug["status"], "unchanged": True}, "error", text)

        db.record_review(bug["id"], "reject", reject_reason=f"拒绝并回滚：{reason}"[:500],
                         change_status=False)
        db.add_analysis(
            bug["id"],
            {
                "conclusion": "主人拒绝本次修改，草稿已回滚，这条按已完结收口",
                "symptom": f"拒绝原因：{reason}",
                "evidence": (f"分支 {done.branch} 已删除；提交 {done.tip} 保留在 "
                             f"{done.shadow}（需要时可取回）" if done.tip
                             else "没有草稿分支需要删除"),
                "impact": "trunk 未被改动（从未推入过正式库）",
                "verify": "镜像里 refs/heads/<分支> 已不存在，影子引用仍在",
                "unverified": "拒绝判定由主人作出，执行器不再重做这条",
                "rollback": f"如需恢复草稿：git branch {done.branch} {done.shadow}",
            },
            kind="manual", author="owner",
        )
        db.set_bug_status(bug["id"], "closed")
        note = (f"人工审查拒绝该修改，已回滚草稿分支 {done.branch}"
                f"{'（提交 ' + done.tip[:10] + ' 保留在影子引用里）' if done.tip else ''}，"
                f"本地系统记为已结案。原因：{reason}")
        result = sync.push_comment(bug["zentao_id"], note)
        text = (f"bug #{bug['zentao_id']} 已拒绝并回滚草稿，状态改为已结案。"
                + ("" if result["ok"] else f" 禅道评论未写入：{result['detail'][:120]}"))
        return reply({"ok": True, "summary": text, "detail": done.summary(),
                      "status": "closed", "branch": done.branch,
                      "shadow": done.shadow, "tip": done.tip}, "ok", text)

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
            i18n_patterns=", ".join(get_settings().i18n_file_patterns) or "(空)",
            i18n_direct="开（I18N_DIRECT_SVN_COMMIT=true）"
            if get_settings().i18n_direct_commit else "关（I18N_DIRECT_SVN_COMMIT=false）",
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
            "svn_working_copy": form.get("svn_working_copy", ""),
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
    # 7) dispatch: the copy-paste下达 page
    # ------------------------------------------------------------------
    @app.route("/dispatch")
    def dispatch_page():
        prompts = _prompt_map()
        base = str(request.args.get("base", "0"))
        if base not in DISPATCH_BASES:
            base = "0"
        rows = _repo_rows()
        queues = _dispatch_queues(rows)
        for group in queues:
            group["dispatch"] = _dispatch_text(group, prompts, base)
            group["picked"] = str(group["ids"]).replace(", ", "+")
        selected = _selected_queue(",".join(request.args.getlist("ids")), rows)
        if selected:
            selected["dispatch"] = _dispatch_text(selected, prompts, base)
            selected["picked"] = str(selected["ids"]).replace(", ", "+")
        return render_template(
            "dispatch.html",
            prompts=_loop_prompts(),
            queues=queues,
            checklist=_dispatch_checklist(queues),
            queue=db.fetch_task_queue(10),
            products=rows,
            selected=selected,
            bases=DISPATCH_BASES,
            base=base,
        )

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


def _loop_prompts() -> list[dict[str, Any]]:
    """Lift the copy-paste prompts straight out of AUTO_LOOP.md.

    Rendering them from the rules file keeps the page from ever offering a prompt
    that has drifted from what the executor is actually held to.
    """
    path = PROJECT_ROOT / "AUTO_LOOP.md"
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except OSError:
        return []
    found: list[dict[str, Any]] = []
    wanted = None
    title = ""
    body: list[str] = []
    in_fence = False
    for line in lines:
        if wanted is None:
            for pattern in LOOP_PROMPT_SECTIONS:
                if re.match(pattern, line):
                    wanted = pattern
                    title = line.lstrip("#").strip()
                    break
            continue
        if not in_fence:
            if line.startswith("```"):
                in_fence = True
            elif line.startswith("#"):
                # The section turned out to have no code block: stop looking at it.
                wanted = None
            continue
        if line.startswith("```"):
            found.append({"title": title, "code": "\n".join(body).rstrip()})
            wanted = None
            body = []
            in_fence = False
        else:
            body.append(line)
    return found


def _prompt_map() -> dict[str, str]:
    """Extracted AUTO_LOOP.md blocks, keyed by their section number."""
    mapping: dict[str, str] = {}
    for section in _loop_prompts():
        head = section["title"].split(" ")[0].rstrip(".")
        mapping[head] = section["code"]
    return mapping


def _make_group(index: int, rows: list[dict[str, Any]]) -> dict[str, Any]:
    """One serial queue: the products that share a single landing directory."""
    directories = {row["effective_wc"] or "(无法解析)" for row in rows}
    group: dict[str, Any] = {
        "no": index,
        "working_copy": rows[0]["effective_wc"] or "(无法解析)",
        "products": rows,
        "bugs": sum(int(row["bugs"] or 0) for row in rows),
        "pending": sum(int(row["pending"] or 0) for row in rows),
        "bound": all(row["source"] == "product" for row in rows),
        "error": next((row["error"] for row in rows if row["error"]), ""),
        "mixed": len(directories) > 1,
        "directories": sorted(directories),
        "ids": ", ".join(str(row["product_id"]) for row in rows),
        "names": "、".join((row["product_name"] or f"产品 {row['product_id']}") for row in rows),
    }
    if group["mixed"]:
        group["serial"] = f"所选产品跨了 {len(directories)} 个目录 → 必须拆成多条下达语"
    elif len(rows) > 1:
        group["serial"] = f"{len(rows)} 个产品共用这一份镜像 → 只能开一条串行队列"
    else:
        group["serial"] = "单产品独占镜像 → 可与其他队列并行"
    return group


def _dispatch_queues(rows: list[dict[str, Any]] | None = None) -> list[dict[str, Any]]:
    """Products grouped by the directory the executor would actually touch.

    One directory is one serial queue (AUTO_LOOP §5): two products sharing a
    mirror must never be worked on in parallel, so the grouping is the thing a
    human needs to see before starting a second main loop.
    """
    buckets: dict[str, list[dict[str, Any]]] = {}
    for row in (rows if rows is not None else _repo_rows()):
        buckets.setdefault(row["effective_wc"] or "(无法解析)", []).append(row)
    queues = [_make_group(index, items)
              for index, (_, items) in enumerate(sorted(
                  buckets.items(),
                  key=lambda kv: (not all(r["source"] == "product" for r in kv[1]),
                                  -sum(int(r["pending"] or 0) for r in kv[1]),
                                  -sum(int(r["bugs"] or 0) for r in kv[1]),
                                  kv[0])), 1)]
    return queues


def _scope_block(group: dict[str, Any], base_label: str) -> str:
    """The scope preamble that turns a generic prompt into this queue's prompt.

    Kept separate from the AUTO_LOOP.md body on purpose: the rules text stays
    verbatim (and keeps updating itself), only this part is filled per selection.
    """
    lines = [f"【本轮范围 · {base_label} · 产品 {group['ids']}】",
             f"只处理禅道产品 ID ∈ {{{group['ids']}}}（{group['names']}）的 bug；"
             f"其他产品一律不接手、不 claim、不改码。"]
    if group["mixed"]:
        lines.append("注意：所选产品跨了 " + " / ".join(group["directories"]) +
                     " 这几个落码目录，不能并成一条队列；本轮只做其中与本目录相关的产品，"
                     "其余的另开一条下达。")
    lines.append(f"落码目录 working_copy = {group['working_copy']}")
    unbound = [str(p["product_id"]) for p in group["products"] if p["source"] != "product"]
    if unbound and len(unbound) < len(group["products"]):
        lines.append(f"其中产品 {'、'.join(unbound)} 还没有绑定代码库（会落到全局目录）："
                     "这几个产品的 bug 本轮一律不许落码，逐条 block 写明「产品未绑定代码库」，"
                     "等主人 bind-repo 之后再接手；其余已绑定的产品照常做。")
    elif unbound:
        lines.append("这些产品都还没有绑定代码库（会落到上面的全局目录）：本轮一律不许落码，"
                     "逐条 block 写明「产品未绑定代码库」，等主人 bind-repo 之后再接手。")
    else:
        lines.append("开工前先验镜像就绪（§1.5 的三条判据全部在 SSH 侧跑）；"
                     "不就绪就直接 block，不改码，也不许转去动正式 SVN 工作副本。")
        lines.append("这个目录两侧是同一条数据：Windows 映射盘只用来读代码，**git 一律走 SSH 侧**，"
                     "命令形态 `ssh <SSH用户>@<SSH主机> \"cd <镜像> && git -c core.ignorecase=false …\"`；"
                     "主机/用户/Linux 侧路径读该库镜像根的 CLAUDE.local.yaml（ssh.* 与 path_aliases）。"
                     "Windows 侧的 ignorecase 会把只差大小写的同名文件混成一个（本库实测 109 组），"
                     "在它里面改 A 会落到 B 上。")
        lines.append("只 add 自己改过的那几个文件，禁止 git add -A / add .；下列是结构性噪音、不是本次改动，"
                     "不许 checkout 复原、不许写进 --files、也不许为此 block（3.0 实测共 92 条）："
                     "整棵 .trae/**（镜像里它是指向主人知识库根的符号链接，checkout 会写穿覆盖知识库）、"
                     "openvpn-2.4.8/INSTALL（仓库里是文件、盘上是同名目录）、"
                     "约 53 条 M（Windows 检出的 CRLF 约 13 条 + $Id$ 关键字形态约 40 条，"
                     "看差异用 --ignore-cr-at-eol）、未跟踪的 CLAUDE.md / CLAUDE.local.yaml / .trae。")
    no_i18n = [str(p["product_id"]) for p in group["products"]
               if p["source"] == "product" and not p.get("i18n_wc")]
    if no_i18n:
        lines.append(f"产品 {'、'.join(no_i18n)} 没配「正式 SVN 工作副本」（svn_working_copy）→ "
                     "G12 词条直连通道不可用：本轮不许打开或修改任何 .po/.mo/.pot/.qm 词条文件，"
                     "需要改词条就 block 写明「未配正式SVN工作副本，词条待主人处理」；"
                     '补齐：python -m app.cli bind-repo <产品ID> "<SVN仓库地址>" --svn-working-copy <正式SVN工作副本>')
    elif all(p["source"] == "product" for p in group["products"]):
        lines.append("词条通道可用：这条 bug 若涉及词条，按 §2.1 走 i18n-up → 该库 AGENTS.md 规定的 "
                     "ai_i18n.py 规程 → i18n-commit 单独立即提交；不跟代码混提交，也不许攒进镜像分支。")
    if group["mixed"]:
        lines.append("本组跨了多个目录：同一目录内部严格串行，不同目录之间才可以并行 —— "
                     "更稳妥的做法是每个目录各下达一条。")
    elif len(group["products"]) > 1:
        lines.append(f"{len(group['products'])} 个产品共用这一份镜像 → 严格串行：一条 bug 提交完"
                     "（或 block 登记完）才允许开下一条，同一时刻只能有一个分支被 checkout；"
                     "禁止为了并行再开第二个主对话动这个目录。")
    else:
        lines.append("本队列独占这个目录 → 可以和其他目录的队列并行，但这个目录同时只允许一个操作方。")
    lines.append("取队列后自行过滤，只留本产品/本目录的条目：python -m app.cli tasks --limit 20")
    lines.append("运行中这三类情况按 §3.4 自己解决，禁止停下来等我（无人值守时等待就是卡死）：")
    lines.append("  a) 模型/接口限流（rate limit、429、overloaded、quota、502/503、稍后再试）"
                 "→ 等 5 分钟重试同一步，最多 3 次（RETRY_WAIT/RETRY_MAX），期间不改状态、不 block、不退循环；"
                 "3 次用完才 block 写「上游限流未恢复」，随后继续下一条；写类动作（评论、i18n-commit）不重试")
    lines.append("  b) 任何需要授权的动作一律不许触发也不许等待：密码弹窗、SSH 密码登录（只准免密 key）、"
                 "IDE 命令审批、沙箱越权提示、sudo、yes/no 交互、提问工具；"
                 "确实绕不开授权就 block 写清「要主人做什么授权/为什么/影响面/怎么回滚」，接着做下一条")
    lines.append("  c) 出现「本地文件比仓库新，是否比较」：自己在 SSH 侧 status + diff --ignore-cr-at-eol 取证 —— "
                 "只剩 CRLF/$Id$/大小写对偶/.trae 就当 §1.5 的结构性噪音照常做；"
                 "是别人写的真实改动就不覆盖、不 stash、不 revert、不 checkout 复原，block 写明摘要")
    lines.append("以下整段是 AUTO_LOOP.md 原文，规则以它为准：")
    return "\n".join(lines)


def _dispatch_text(group: dict[str, Any], prompts: dict[str, str], base: str) -> str:
    label = DISPATCH_BASES.get(base, base)
    body = prompts.get(base) or prompts.get("0") or "(读不到 AUTO_LOOP.md 的下达语，请检查该文件是否存在)"
    return f"{_scope_block(group, label)}\n\n{body}"


def _selected_queue(raw_ids: str, rows: list[dict[str, Any]]) -> dict[str, Any] | None:
    """A queue built from an arbitrary product selection instead of by directory."""
    picked = [int(x) for x in re.findall(r"\d+", raw_ids or "")]
    if not picked:
        return None
    chosen = [row for row in rows if int(row["product_id"]) in picked]
    if not chosen:
        return None
    # Keep the order the user picked, then any product that only exists in the table.
    ordered = sorted(chosen, key=lambda r: picked.index(int(r["product_id"])))
    return _make_group(0, ordered)


def _dispatch_checklist(queues: list[dict[str, Any]]) -> list[dict[str, str]]:
    """What still has to be fixed before the loop prompt is worth pasting."""
    settings = get_settings()
    counts = db.counts_by_status()
    items: list[dict[str, str]] = []

    def add(level: str, text: str, cmd: str = "") -> None:
        items.append({"level": level, "text": text, "cmd": cmd})

    if settings.zentao_ready:
        add("ok", f"禅道已配置（{settings.zentao_base_url}），同步与评论回写可用。")
    else:
        add("err", "禅道账号未配置：同步、抓详情、回写评论都不通。先编辑 .env，再到「配置」页重新加载。")

    open_rows = db.query_all(
        "SELECT detail_synced_at FROM bugs WHERE status IN "
        "('pending','fixing','rejected','need_solution','await_review')"
    )
    missing = sum(1 for row in open_rows if not row["detail_synced_at"])
    if missing:
        add("warn", f"{missing} 条未抓详情（截图 / 备注 / 附件）：执行器看不到现场只能靠猜。",
            "python -m app.cli sync --details -1")
    else:
        add("ok", "未关闭 bug 的详情都已抓到（描述 / 截图 / 备注）。")

    for group in queues:
        if not group["bound"]:
            add("err", f"队列 #{group['no']}：产品 {group['ids']}（{group['names']}）没有绑定仓库，"
                       f"会落到全局目录 {group['working_copy']} —— 那很可能是你日常开发那份代码，"
                       f"先 bind-repo 指到它自己的镜像再下达。",
                'python -m app.cli bind-repo <产品ID> "<SVN仓库根地址>" --working-copy <镜像目录>')
    for group in queues:
        if len(group["products"]) > 1:
            add("warn", f"队列 #{group['no']}：产品 {group['ids']} 共用 {group['working_copy']}，"
                        f"只能由同一个主对话串行做；要并行只能给其中一个另开一份镜像。")
        elif group["bound"]:
            add("ok", f"队列 #{group['no']}：产品 {group['ids']} 独占 {group['working_copy']}，"
                      f"可以和其他队列并行。")

    add("info", "每条队列开跑前验一次镜像就绪（三条都要有输出；最后一条失败 = ref 在但 index/工作树没落盘）。"
                "全部在 SSH 侧跑 —— Windows 侧那份只读代码不跑 git，主机与路径见该库镜像根的 CLAUDE.local.yaml：",
        'ssh <SSH用户>@<SSH主机> "cd <镜像目录> && git -c core.ignorecase=false rev-parse --verify refs/remotes/origin/trunk" ; '
        'ssh <SSH用户>@<SSH主机> "cd <镜像目录> && git -c core.ignorecase=false rev-parse --verify HEAD" ; '
        'ssh <SSH用户>@<SSH主机> "cd <镜像目录> && git -c core.ignorecase=false ls-files --error-unmatch AGENTS.md"')
    add("info", "index 缺失只在 SSH（Linux）侧修：Windows 侧 ignorecase 遇上 109 组只差大小写的路径修不出完整工作树；"
                "清单里排掉 .trae/，否则会写穿符号链接覆盖主人知识库。")
    add("info", "两条线的 SSH 侧不是一套环境（实测）：3.0 那台 git 2.33.0 认 -c / -C；"
                "2.3 那台是 git 1.7.1（RHEL6），两个都不认 —— 改用 cd <镜像> + git --git-dir=.git，"
                "覆盖配置用 GIT_CONFIG=<写着 core.ignorecase=false 的文件>；"
                "它的 OpenSSH 5.3 只认 RSA 密钥，ssh 要带 -i <identity_file> -o IdentitiesOnly=yes "
                "-o HostKeyAlgorithms=+ssh-rsa -o PubkeyAcceptedKeyTypes=+ssh-rsa（值见该库 CLAUDE.local.yaml）。")
    if settings.require_analysis:
        add("ok", "REQUIRE_ANALYSIS=true：没写分析结论的 commit 会被命令直接拒掉。")
    else:
        add("warn", "REQUIRE_ANALYSIS=false：没有分析结论也能提交，审查页会是空的。建议改回 true。")

    if counts.get("await_review"):
        add("info", f"已攒 {counts['await_review']} 条待审查：先审再下达，草稿不会自己进正式库。")
    if counts.get("need_solution"):
        add("info", f"{counts['need_solution']} 条在等你给方案（/need）：答复后它们以最高优先级回队列。")
    add("info", "G11：执行器只做本地 git commit；dcommit / git push / svn ci 由你亲自做，"
                "回填时用 --revision r<真实号>。唯一例外是 G12 的多语言词条通道（白名单文件单独立即提交）。")
    if not settings.i18n_direct_commit:
        add("warn", "I18N_DIRECT_SVN_COMMIT=false：多语言通道（G12）已关闭，词条改动会留在镜像里等"
                    "回灌，攒久了必然冲突。")
    else:
        add("info", f"G12：多语言文件走直连通道（{', '.join(settings.i18n_file_patterns)}）—— "
                    "i18n-up 改前先 up，改完 i18n-commit 单独立即提交 SVN。")
        missing = [f"#{q['no']}（产品 {q['ids']}）" for q in queues
                   if not any(p["i18n_wc"] for p in q["products"])]
        if missing:
            add("warn", f"{'、'.join(missing)} 没配「正式 SVN 工作副本」，"
                        "这些产品的 i18n-commit 会被拒绝。",
                'python -m app.cli bind-repo <产品ID> "<SVN仓库地址>" --svn-working-copy <正式SVN工作副本>')
    add("info", f"限流与暂时失败不许中止任务（AUTO_LOOP.md §3.4）：读禅道那侧已内置等待重试 —— "
                f"等 {settings.retry_wait} 秒、总共 {settings.retry_max} 次（.env 的 RETRY_WAIT / RETRY_MAX），"
                "等待过程打到 stderr 的 [retry] 行；评论推送与 i18n-commit 是写动作，刻意不重试。"
                "模型侧的限流要你自己按同一条口径等，不许整轮退出。")
    stale = db.query_all(
        "SELECT zentao_id, updated_at FROM bugs WHERE status = 'fixing'"
        " AND updated_at < datetime('now', 'localtime', ?)",
        (f"-{int(settings.stale_claim_minutes)} minutes",),
    ) if int(settings.stale_claim_minutes) > 0 else []
    if stale:
        add("warn", f"{len(stale)} 条停在 fixing 已超过 {settings.stale_claim_minutes} 分钟"
                    "（上一轮被限流或中断打死）：它们已自动回到队列，下一轮会以 queue_kind=stale 重跑，"
                    "不需要手工改状态。",
            'python -m app.cli tasks --limit 20')
    else:
        add("ok", f"没有超时未收的 fixing 占位（阈值 STALE_CLAIM_MINUTES="
                  f"{settings.stale_claim_minutes} 分钟，超过就自动回队列）。")
    add("info", "运行中禁止出现任何需要授权的动作：密码弹窗、SSH 密码登录、IDE 命令审批、sudo、"
                "yes/no 交互、提问工具都不许触发；确实绕不开就 block 写清要主人做什么授权，继续下一条，"
                "不要等。")
    add("info", "「本地文件比仓库新，是否比较」由执行器自己判定（§3.4 第三步）："
                "只剩 CRLF / $Id$ / 大小写对偶 / .trae 就当噪音照常做；"
                "是别人写的真实改动就不覆盖不 revert，block 写明摘要。")
    add("info", "草稿进正式库这一步现在有主人的按钮：审查详情弹窗「预检 / 正式推入 SVN」"
                "（app/svn_promote.py，SSH 侧稀疏工作副本 + svn ci 进 trunk，只有成功进库才登记真实 r 号并置 merged；"
                "推送失败只写人工留痕，状态仍是待审查、你的 git:<哈希> 草稿记录不动，所以你不会被莫名打回）。"
                "你只登记 git:<哈希>，"
                "不许调用 /bug/<id>/svn-push，也不许自己复刻那串 svn ci —— 那还是违反 G11。")
    return items


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
        "svn_working_copy": binding.get("svn_working_copy") or "",
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
        "svn_working_copy": binding.get("svn_working_copy") or "",
        "build_command": binding.get("build_command") or "",
        "test_command": binding.get("test_command") or "",
        "source": target.source if target else "",
        "effective_trunk": target.trunk_url() if target else "",
        "effective_wc": str(target.working_copy) if target else "",
        "i18n_wc": str(target.svn_working_copy) if target and target.svn_working_copy else "",
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
