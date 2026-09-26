"""Command line interface used by the AI executor (see AGENTS.md).

Every command prints a single JSON object so the agent can parse the result
without scraping human oriented text.

Examples
--------
    python -m app.cli init
    python -m app.cli sync
    python -m app.cli tasks --limit 5
    python -m app.cli claim 1024
    python -m app.cli branch 1024
    python -m app.cli note 1024 --summary "修复空指针" --verify "打开页面 A" --files a.py,b.py
    python -m app.cli commit 1024 --message "修复空指针导致的崩溃"
    python -m app.cli block 1024 --question "该走方案A还是方案B" --options "A:...;B:..." --advice "建议A"
    python -m app.cli report
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path
from typing import Any

from . import db, svn_client, sync
from .config import get_settings


def _out(payload: dict[str, Any]) -> None:
    print(json.dumps(payload, ensure_ascii=False, indent=2))


def _bug_ref(ref: str) -> dict:
    """Resolve an argument as either a Zentao id or a local bug id."""
    bug = db.get_bug_by_zentao_id(int(ref)) or db.get_bug(int(ref))
    if bug is None:
        raise SystemExit(json.dumps({"ok": False, "error": f"bug not found: {ref}"}, ensure_ascii=False))
    return bug


# ---------------------------------------------------------------------------
# commands
# ---------------------------------------------------------------------------

def cmd_init(_args: argparse.Namespace) -> None:
    path = db.init_db()
    _out({"ok": True, "action": "init", "db": str(path), "tables": list(db.BUG_STATUSES)})


def cmd_sync(args: argparse.Namespace) -> None:
    result = sync.sync_now()
    # Optionally enrich the queue so screenshots/notes are on disk before the AI
    # reasons about a bug; `--details -1` means every queued bug.
    if args.details:
        if args.details < 0:
            summary = sync.pull_details(limit=None)
            result["details"] = [summary]
        else:
            enriched = []
            for task in db.fetch_task_queue(limit=args.details):
                item = sync.refresh_detail(task["zentao_id"])
                enriched.append({
                    "zentao_id": item.get("zentao_id"),
                    "ok": item.get("ok"),
                    "images_saved": item.get("images_saved"),
                    "comments": item.get("comments"),
                    "error": item.get("detail"),
                })
            result["details"] = enriched
    _out({"ok": not result["errors"], "action": "sync", **result})


def cmd_tasks(args: argparse.Namespace) -> None:
    db.ensure_db()
    tasks = db.fetch_task_queue(limit=args.limit)
    _out({"ok": True, "action": "tasks", "count": len(tasks), "tasks": tasks})


def cmd_claim(args: argparse.Namespace) -> None:
    bug = _bug_ref(args.ref)
    updated = db.set_bug_status(bug["id"], "fixing")
    _out({"ok": True, "action": "claim", "bug": updated})


def cmd_branch(args: argparse.Namespace) -> None:
    """Create/switch the bugfix branch in the repository that owns this bug."""
    bug = _bug_ref(args.ref)
    target = svn_client.resolve_target(bug=bug)
    branch_name = f"{target.branch_prefix}{bug['zentao_id']}"
    db.set_bug_status(bug["id"], "fixing", branch=branch_name)
    report = {
        "ok": True,
        "action": "branch",
        "branch": branch_name,
        "product_id": bug.get("product_id"),
        "product_name": bug.get("product_name") or "",
        "repo": target.repo_url or "(未配置)",
        "repo_source": target.source,
        "trunk": target.trunk_url(),
        "working_copy": str(target.working_copy),
    }
    if args.dry_run:
        hint = "dry-run"
        if not target.repo_url:
            hint = (f"dry-run，但产品 {bug.get('product_id')} 未绑定代码库且 SVN_REPO_URL 为空，"
                    f"真正执行前请先跑 python -m app.cli bind-repo {bug.get('product_id')} <仓库地址>")
        _out({**report, "svn": "skipped", "reason": hint})
        return
    if not target.repo_url:
        _out({**report, "svn": "skipped",
              "reason": f"产品 {bug.get('product_id')} 未绑定代码库且 SVN_REPO_URL 为空，"
                        f"请执行 python -m app.cli bind-repo {bug.get('product_id')} <仓库地址>"})
        return
    url = svn_client.create_branch(str(bug["zentao_id"]), target=target,
                                   message=f"branch for zentao bug #{bug['zentao_id']}")
    _out({**report, "svn": url})


def _add_analysis_args(parser: argparse.ArgumentParser) -> None:
    """Attach the shared analysis options; the JSON key is `verify`, the flag is
    `--verify-analysis` so it never collides with the review field `--verify`."""
    parser.add_argument("--analysis-file", help="JSON 文件：一次性写入全部分析字段")
    parser.add_argument("--analysis-stdin", action="store_true",
                        help="从标准输入读同一种 JSON（适合长文本，免转义）")
    parser.add_argument("--symptom", help="现象：用户看到什么、在哪个版本/入口复现")
    parser.add_argument("--root-cause", dest="root_cause", help="根因（一句话讲清为什么坏）")
    parser.add_argument("--evidence", help="定位依据：真实读过的文件/函数/日志/复现输出")
    parser.add_argument("--chain", help="涉及链路：入口 -> 中间层 -> 最终实现")
    parser.add_argument("--change", help="改了什么（含为什么这样改）")
    parser.add_argument("--impact", help="影响面与同构路径自审结论")
    parser.add_argument("--verify-result", dest="verify_analysis",
                        help="分析里的验证结论（编译/测试/复现的实际输出）；"
                             "区别于审查页的 --verify（人工验证步骤）")
    parser.add_argument("--unverified", help="仍未验证的点（没有就留空，别写「无」以外的含糊话）")
    parser.add_argument("--rollback", help="回退方式")
    parser.add_argument("--conclusion", help="一句话结论")
    parser.add_argument("--gates", help="闸门逐条结论，例：G1=pass,G5=未验证:未跑 OEM 同名页")


def _parse_gates(raw: str | None) -> dict[str, str]:
    """Turn ``G1=pass,G5=未验证:xxx`` into ``{"G1": "pass", "G5": "未验证:xxx"}``."""
    gates: dict[str, str] = {}
    for item in re.split(r"[,;，；]\s*", str(raw or "").strip()):
        if not item or "=" not in item:
            continue
        key, value = item.split("=", 1)
        gates[key.strip().upper()] = value.strip()
    return gates


def _normalized_analysis(args: argparse.Namespace) -> dict[str, Any] | None:
    """Merge `--analysis-file` / `--analysis-stdin` / per-field flags into one payload."""
    payload: dict[str, Any] = {}
    raw = ""
    if getattr(args, "analysis_file", None):
        raw = Path(args.analysis_file).read_text(encoding="utf-8")
    elif getattr(args, "analysis_stdin", False):
        raw = sys.stdin.read()
    if raw.strip():
        try:
            loaded = json.loads(raw)
        except json.JSONDecodeError as exc:
            raise SystemExit(json.dumps(
                {"ok": False, "error": f"分析 JSON 解析失败：{exc}"}, ensure_ascii=False))
        if not isinstance(loaded, dict):
            raise SystemExit(json.dumps(
                {"ok": False, "error": "分析内容必须是一个 JSON 对象"}, ensure_ascii=False))
        payload.update(loaded)
    flags = {
        "symptom": getattr(args, "symptom", None),
        "root_cause": getattr(args, "root_cause", None),
        "evidence": getattr(args, "evidence", None),
        "call_chain": getattr(args, "chain", None),
        "change_desc": getattr(args, "change", None),
        "impact": getattr(args, "impact", None),
        "verify": getattr(args, "verify_analysis", None),
        "unverified": getattr(args, "unverified", None),
        "rollback": getattr(args, "rollback", None),
        "conclusion": getattr(args, "conclusion", None),
    }
    payload.update({key: value for key, value in flags.items() if value})
    gates = payload.get("gates") if isinstance(payload.get("gates"), dict) else {}
    gates = dict(gates)
    gates.update(_parse_gates(getattr(args, "gates", None)))
    known = set(db.ANALYSIS_FIELDS) | {"author"}
    payload = {key: value for key, value in payload.items() if key in known}
    if not payload and not gates:
        return None
    payload["_gates"] = gates
    return payload


def _write_analysis(bug: dict, payload: dict[str, Any], kind: str) -> dict:
    """Persist a normalized analysis payload."""
    gates = payload.pop("_gates", {}) or {}
    author = str(payload.pop("author", "") or "") or get_settings().zentao_account or "ai"
    return db.add_analysis(bug["id"], payload, kind=kind, gates=gates, author=author)


def _require_analysis_or_die(bug: dict, payload: dict[str, Any] | None) -> None:
    """When REQUIRE_ANALYSIS is on, a bug cannot be committed without a written analysis."""
    if payload is not None or not get_settings().require_analysis:
        return
    if db.has_analysis(bug["id"]):
        return
    raise SystemExit(json.dumps({
        "ok": False,
        "error": "提交前必须把分析结果写入系统（REQUIRE_ANALYSIS=true）",
        "hint": "加 --root-cause/--evidence/--impact/--verify/--gates ...，"
                "或先跑 python -m app.cli analyze <禅道ID> --analysis-file a.json；"
                "字段规范见 AUTO_LOOP.md §2 提交闸门",
    }, ensure_ascii=False))


def cmd_analyze(args: argparse.Namespace) -> None:
    """Store the executor's written analysis for one bug."""
    bug = _bug_ref(args.ref)
    payload = _normalized_analysis(args)
    if payload is None:
        raise SystemExit(json.dumps({
            "ok": False,
            "error": "没有收到分析内容：用 --analysis-file <json> / --analysis-stdin，"
                     "或至少给 --root-cause / --impact / --verify / --gates 之一",
        }, ensure_ascii=False))
    saved = _write_analysis(bug, payload, args.kind)
    _out({"ok": True, "action": "analyze", "zentao_id": bug["zentao_id"],
          "analysis_id": saved["id"], "kind": saved["kind"], "gates": saved["gates"],
          "analysis": saved})


def cmd_note(args: argparse.Namespace) -> None:
    bug = _bug_ref(args.ref)
    files = [f.strip() for f in (args.files or "").split(",") if f.strip()]
    fields: dict[str, Any] = {}
    if args.summary is not None:
        fields["fix_summary"] = args.summary
    if args.verify is not None:
        fields["verify_steps"] = args.verify
    if files:
        fields["files_changed"] = files
    updated = db.set_bug_status(bug["id"], bug["status"], **fields)
    payload = _normalized_analysis(args)
    saved = _write_analysis(bug, payload, "manual") if payload else None
    _out({"ok": True, "action": "note", "analysis_id": (saved or {}).get("id"), "bug": updated})


def cmd_commit(args: argparse.Namespace) -> None:
    bug = _bug_ref(args.ref)
    payload = _normalized_analysis(args)
    _require_analysis_or_die(bug, payload)
    target = svn_client.resolve_target(bug=bug)
    branch = bug.get("branch") or f"{target.branch_prefix}{bug['zentao_id']}"
    message = f"fix #{bug['zentao_id']} {args.message or bug['title']}".strip()
    files = [f.strip() for f in (args.files or "").split(",") if f.strip()]

    revision = args.revision
    svn_note = ""
    if not revision and not args.no_svn:
        if not target.repo_url:
            raise SystemExit(json.dumps({
                "ok": False,
                "error": f"产品 {bug.get('product_id')} 没有可用仓库地址："
                         f"请 bind-repo 绑定该产品或在 .env 设置 SVN_REPO_URL",
            }, ensure_ascii=False))
        revision = svn_client.commit(files or None, message, target=target)
    if not revision:
        revision = f"DRYRUN-{db.now_str().replace(':', '').replace('-', '').replace(' ', '')}"
        svn_note = "SVN 未实际提交（--no-svn 或仓库未配置）"

    db.add_revision(bug["id"], revision, branch=branch, message=message,
                    author=args.author or get_settings().svn_username or "ai",
                    files=files or (bug.get("files_changed") or []))
    fields: dict[str, Any] = {"branch": branch}
    if args.summary:
        fields["fix_summary"] = args.summary
    if args.verify:
        fields["verify_steps"] = args.verify
    if files:
        fields["files_changed"] = files
    db.set_bug_status(bug["id"], "await_review", **fields)

    # The written analysis travels with the commit so the review page has something to read.
    analysis_id = None
    if payload is not None:
        analysis_id = _write_analysis(bug, payload, "commit")["id"]

    comment = sync.comment_commit(bug["zentao_id"], branch, revision,
                                 extra=args.extra or "")
    if args.need_done:
        for need_id in args.need_done:
            db.close_need(int(need_id))
    _out({
        "ok": True,
        "action": "commit",
        "zentao_id": bug["zentao_id"],
        "revision": revision,
        "branch": branch,
        "repo": target.repo_url or "(未配置)",
        "repo_source": target.source,
        "working_copy": str(target.working_copy),
        "note": svn_note,
        "status": "await_review",
        "analysis_id": analysis_id,
        "analysis_fields": sorted(k for k in (payload or {}) if not k.startswith("_")),
        "zentao_comment": comment,
    })


def cmd_block(args: argparse.Namespace) -> None:
    bug = _bug_ref(args.ref)
    payload = _normalized_analysis(args)
    need = db.create_need(bug["id"], args.question, args.options or "", args.advice or "")
    db.set_bug_status(bug["id"], "need_solution")
    comment = sync.comment_block(bug["zentao_id"], args.question, args.options or "", args.advice or "")
    analysis = _write_analysis(bug, payload, "block") if payload else None
    _out({"ok": True, "action": "block", "need": need, "status": "need_solution",
          "analysis_id": (analysis or {}).get("id"), "zentao_comment": comment})


def cmd_need_done(args: argparse.Namespace) -> None:
    _out({"ok": True, "action": "need-done", "need": db.close_need(int(args.need_id))})


def cmd_comment(args: argparse.Namespace) -> None:
    bug = _bug_ref(args.ref)
    _out({"ok": True, "action": "comment", "result": sync.push_comment(bug["zentao_id"], args.text)})


def cmd_report(_args: argparse.Namespace) -> None:
    counts = db.counts_by_status()
    queue = db.fetch_task_queue(limit=1000)
    _out({
        "ok": True,
        "action": "report",
        "counts": counts,
        "submitted_await_review": [b["zentao_id"] for b in db.query_all(
            "SELECT zentao_id FROM bugs WHERE status='await_review' ORDER BY pri")],
        "need_owner_solution": [b["zentao_id"] for b in db.query_all(
            "SELECT zentao_id FROM bugs WHERE status='need_solution' ORDER BY pri")],
        "resumed_after_reply": [n["id"] for n in db.query_all(
            "SELECT id FROM need_solution WHERE status='replied'")],
        "remaining_queue": [
            {"zentao_id": t["zentao_id"], "kind": t["queue_kind"], "title": t["title"]}
            for t in queue
        ],
        "remaining_work": len(queue),
    })


def cmd_status(args: argparse.Namespace) -> None:
    bug = _bug_ref(args.ref)
    _out({"ok": True, "action": "status", "bug": bug})


def cmd_svn_check(args: argparse.Namespace) -> None:
    """Read-only SVN self test for one repo, every bound repo, or a product."""
    _out({"action": "svn-check",
          **svn_client.preflight(product_id=args.product, all_targets=bool(args.all))})


def cmd_repos(_args: argparse.Namespace) -> None:
    """List products seen in bugs plus their repository bindings."""
    bound = {r["product_id"]: r for r in db.list_product_repos()}
    rows = []
    for item in db.products_in_use():
        repo = bound.get(item["product_id"])
        target = svn_client.resolve_target(product_id=item["product_id"])
        rows.append({
            "product_id": item["product_id"],
            "product_name": item["product_name"] or (repo or {}).get("product_name", ""),
            "bugs": item["bugs"],
            "pending": item["pending"],
            "bound": repo is not None,
            "repo_url": target.repo_url or "",
            "effective_source": target.source,
            "working_copy": str(target.working_copy),
            "trunk": target.trunk_url(),
            "branch_root": target.branch_root_url(),
            "build_command": (repo or {}).get("build_command", ""),
            "test_command": (repo or {}).get("test_command", ""),
            "note": (repo or {}).get("note", ""),
        })
    _out({"ok": True, "action": "repos", "count": len(rows),
          "global_repo": get_settings().svn_repo_url or "(未配置)",
          "unbound_products": sum(1 for r in rows if not r["bound"]),
          "repos": rows})


def cmd_bind_repo(args: argparse.Namespace) -> None:
    """Bind a Zentao product to its own SVN repository."""
    if args.unbind:
        db.unbind_product_repo(args.product_id)
        _out({"ok": True, "action": "bind-repo", "unbind": args.product_id,
              "note": "该产品将回落到全局 SVN_REPO_URL（若为空则 branch/commit 会被拒绝）"})
        return
    name = args.name
    if not name:
        row = db.query_one("SELECT product_name FROM bugs WHERE product_id = ? "
                           "AND product_name IS NOT NULL AND product_name != '' LIMIT 1",
                           (args.product_id,))
        name = (row or {}).get("product_name", "")
    repo = db.bind_product_repo(args.product_id, {
        "product_name": name or "",
        "repo_url": args.repo_url or "",
        "trunk_path": args.trunk_path,
        "branch_root": args.branch_root,
        "working_copy": args.working_copy,
        "branch_prefix": args.branch_prefix,
        "build_command": args.build,
        "test_command": args.test,
        "note": args.note,
        "enabled": 0 if args.disabled else 1,
    })
    _out({"ok": True, "action": "bind-repo", "repo": repo,
          "trunk": svn_client.resolve_target(product_id=args.product_id).trunk_url()})


def cmd_config(_args: argparse.Namespace) -> None:
    _out({"ok": True, "action": "config", "settings": get_settings().describe()})


def cmd_detail(args: argparse.Namespace) -> None:
    """Pull screenshots, notes and attachments: one bug, or the whole queue."""
    bulk = bool(args.all) or (args.ref is None and bool(args.limit))
    if args.ref is None and not bulk:
        raise SystemExit(json.dumps(
            {"ok": False, "error": "请给出禅道ID，或使用 --all / --limit N 批量抓取"},
            ensure_ascii=False))
    if bulk:
        limit = None if args.all else int(args.limit)
        summary = sync.pull_details(limit=limit, download=not args.no_images)
        _out({"ok": bool(summary.get("ok")), "action": "detail-all", **summary})
        return
    bug = _bug_ref(args.ref)
    result = sync.refresh_detail(bug["zentao_id"], download=not args.no_images)
    _out({"action": "detail", **result})


def cmd_doctor(_args: argparse.Namespace) -> None:
    """Diagnose the configured Zentao channel step by step (read-only)."""
    from .zentao_session import make_client

    settings = get_settings()
    if not settings.zentao_base_url:
        _out({"ok": False, "action": "doctor", "auth_mode": settings.zentao_auth_mode,
              "steps": [{"name": "配置", "ok": False,
                         "detail": "ZENTAO_BASE_URL 未填写，请编辑 .env"}],
              "hint": "先配置禅道地址再诊断"})
        return
    try:
        client = make_client()
    except Exception as exc:  # missing account / password / token
        _out({"ok": False, "action": "doctor", "auth_mode": settings.zentao_auth_mode,
              "steps": [{"name": "配置", "ok": False, "detail": str(exc)}],
              "hint": "配置不完整"})
        return
    report = client.probe()
    _out({"action": "doctor", "auth_mode": type(client).__name__, **report})


def cmd_session_probe(_args: argparse.Namespace) -> None:
    """Authenticate with the session channel and dump raw endpoint samples."""
    from .zentao_session import ZentaoSessionClient

    client = ZentaoSessionClient(use_cookie=_args.use_cookie)
    if not client.use_cookie:
        client.login()
    sweep = client.probe_endpoints()
    _out({
        "ok": True,
        "action": "session-probe",
        "auth": "cookie" if client.use_cookie else "password",
        "authenticated": client.is_authenticated(),
        "endpoints": sweep,
        "note": "原始响应已存 session_dump/，解析器按其中的样本对齐",
    })


DEMO_BUGS = [
    # (zentao_id, title, severity, pri, status, module, steps)
    (9001, "[DEMO] 点击保存按钮报 NullPointerException", 1, 1, "pending", "订单管理",
     "1. 打开订单列表\n2. 不填备注直接点保存\n期望：提示必填\n实际：500 空指针"),
    (9002, "[DEMO] 导出 Excel 日期列少一天（时区问题）", 2, 2, "pending", "报表",
     "导出任意报表，对比页面日期。"),
    (9003, "[DEMO] 权限变更后缓存未刷新，需重启服务", 2, 2, "fixing", "权限",
     "修改角色权限后 5 分钟内接口仍返回旧权限。"),
    (9004, "[DEMO] 结算金额四舍五入规则需产品确认", 3, 3, "need_solution", "结算",
     "分账尾差 0.01，方案A 向上取整 / 方案B 银行家舍入。"),
    (9005, "[DEMO] 列表页翻页丢失筛选条件", 3, 1, "await_review", "公共组件",
     "筛选后点第 2 页，筛选条件被重置。"),
    (9006, "[DEMO] 附件上传超过 5MB 无提示", 4, 4, "await_review", "附件",
     "上传 6MB 文件，前端无反应，后端 500。"),
    (9007, "[DEMO] 已合入的登录超时时间配置", 3, 3, "merged", "登录",
     "会话 30 分钟过期后仍显示已登录。"),
    (9008, "[DEMO] 已结案的首页样式错位", 4, 4, "closed", "首页",
     "1920*1080 下 banner 右侧留白。"),
]


def cmd_demo(args: argparse.Namespace) -> None:
    """Insert/clear obviously-marked sample rows so the UI can be reviewed offline."""
    if args.clear:
        with db.tx() as conn:
            rows = conn.execute("SELECT id FROM bugs WHERE title LIKE '[DEMO]%'").fetchall()
            for row in rows:
                conn.execute("DELETE FROM bugs WHERE id = ?", (row["id"],))
        _out({"ok": True, "action": "demo-clear", "db": str(db.db_path())})
        return

    ts = db.now_str()
    inserted = 0
    for zentao_id, title, severity, pri, status, module, steps in DEMO_BUGS:
        bug_id, action, _ = db.upsert_bug_from_zentao({
            "zentao_id": zentao_id, "title": title, "severity": severity, "pri": pri,
            "steps": steps, "module": module, "assigned_to": get_settings().zentao_account or "demo",
            "opened_by": "qa", "opened_build": "trunk", "zentao_status": "active",
            "zentao_resolution": "", "opened_date": ts, "raw": {"demo": True},
        })
        if action == "created":
            inserted += 1
        db.set_bug_status(bug_id, status, branch=f"{get_settings().branch_prefix}{zentao_id}")

    # attach demo revisions / needs / reviews for the richer statuses
    for zentao_id, title, _sev, _pri, status, _mod, _steps in DEMO_BUGS:
        bug = db.get_bug_by_zentao_id(zentao_id)
        if bug is None or bug["revisions"] or bug["needs"] or bug["reviews"]:
            continue
        if status in {"await_review", "merged", "closed"}:
            db.add_revision(bug["id"], "120%02d" % (zentao_id % 100), branch=bug["branch"],
                            message=f"fix #{zentao_id} 演示提交，等待人工审查",
                            author="ai", files=["src/demo/module.py", "src/demo/module_test.py"])
            db.set_bug_status(bug["id"], status,
                              fix_summary="演示：定位到未校验空值，补充判空与单测。",
                              verify_steps="演示：按禅道复现步骤回归，观察不再报错。",
                              files_changed=["src/demo/module.py", "src/demo/module_test.py"])
        if status == "need_solution":
            db.create_need(bug["id"], "尾差处理规则属于业务口径，AI 无法自行决定。",
                           "方案A：分位向上取整；方案B：银行家舍入；方案C：尾差计入最后一笔",
                           "建议方案C，与现有对账逻辑一致，改动面最小。")
        if status == "merged":
            db.record_review(bug["id"], "pass", merged_revision="12999")
    _out({"ok": True, "action": "demo", "inserted": inserted, "db": str(db.db_path()),
          "note": "所有演示数据标题带 [DEMO] 前缀，用 `python -m app.cli demo --clear` 清除"})


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="app.cli", description="Bug 自动修复闭环执行器接口")
    sub = parser.add_subparsers(dest="command", required=True)

    p = sub.add_parser("init", help="创建 SQLite 数据库与全部表")
    p.set_defaults(func=cmd_init)

    p = sub.add_parser("sync", help="从禅道拉取指派给我的 active bug")
    p.add_argument("--details", type=int, default=0, metavar="N",
                   help="同步后再抓队列前 N 条的详情（截图/备注/附件）")
    p.set_defaults(func=cmd_sync)

    p = sub.add_parser("detail", help="抓 bug 的截图、备注、附件并入库（不给 ID 时用 --all 全量）")
    p.add_argument("ref", nargs="?", default=None)
    p.add_argument("--all", action="store_true", help="全量抓取队列内所有 bug 的详情")
    p.add_argument("--limit", type=int, default=0, help="只抓前 N 条（按 pri/severity 排序）")
    p.add_argument("--no-images", action="store_true", help="只取文字，不下载附件")
    p.set_defaults(func=cmd_detail)

    p = sub.add_parser("tasks", help="读取待处理队列（pri 升序 / severity 降序）")
    p.add_argument("--limit", type=int, default=get_settings().batch_size)
    p.set_defaults(func=cmd_tasks)

    p = sub.add_parser("claim", help="把 bug 标记为 fixing")
    p.add_argument("ref")
    p.set_defaults(func=cmd_claim)

    p = sub.add_parser("branch", help="创建/切换到 bugfix 分支")
    p.add_argument("ref")
    p.add_argument("--dry-run", action="store_true")
    p.set_defaults(func=cmd_branch)

    p = sub.add_parser("analyze", help="写入这条 bug 的分析结论（根因/证据/影响面/闸门逐条/未验证项）")
    p.add_argument("ref")
    p.add_argument("--kind", choices=list(db.ANALYSIS_KINDS), default="manual",
                   help="这条分析的场合：commit=修复完成 block=卡点 manual=随手记")
    _add_analysis_args(p)
    p.set_defaults(func=cmd_analyze)

    p = sub.add_parser("note", help="写入修改说明/验证步骤/文件列表（供审查页展示）")
    p.add_argument("ref")
    p.add_argument("--summary")
    p.add_argument("--verify")
    p.add_argument("--files")
    _add_analysis_args(p)
    p.set_defaults(func=cmd_note)

    p = sub.add_parser("commit", help="提交 SVN、记录修订号、置为 await_review 并回写禅道评论")
    p.add_argument("ref")
    p.add_argument("--message", help="修复说明，最终 commit message 为 fix #<ID> <说明>")
    p.add_argument("--files")
    p.add_argument("--revision", help="外部已提交时直接指定修订号")
    p.add_argument("--summary")
    p.add_argument("--verify")
    p.add_argument("--author")
    p.add_argument("--extra", help="附加到禅道评论的补充说明")
    p.add_argument("--need-done", type=int, nargs="*", help="提交后关闭这些 need_solution 记录")
    p.add_argument("--no-svn", action="store_true", help="不调用 svn，只登记记录")
    _add_analysis_args(p)
    p.set_defaults(func=cmd_commit)

    p = sub.add_parser("block", help="登记需方案并置为 need_solution，同时回写禅道评论")
    p.add_argument("ref")
    p.add_argument("--question", required=True)
    p.add_argument("--options", default="")
    p.add_argument("--advice", default="")
    _add_analysis_args(p)
    p.set_defaults(func=cmd_block)

    p = sub.add_parser("need-done", help="把某条 need_solution 标记为 done")
    p.add_argument("need_id")
    p.set_defaults(func=cmd_need_done)

    p = sub.add_parser("comment", help="仅回写一条禅道评论")
    p.add_argument("ref")
    p.add_argument("--text", required=True)
    p.set_defaults(func=cmd_comment)

    p = sub.add_parser("status", help="查看某个 bug 的完整信息")
    p.add_argument("ref")
    p.set_defaults(func=cmd_status)

    p = sub.add_parser("report", help="输出汇总报告")
    p.set_defaults(func=cmd_report)

    p = sub.add_parser("svn-check", help="SVN 只读自检：客户端/仓库/认证/trunk/工作副本/主干保护")
    p.add_argument("--product", type=int, default=None, help="只检查某个产品解析出的仓库")
    p.add_argument("--all", action="store_true", help="检查所有已绑定产品的仓库 + 全局默认")
    p.set_defaults(func=cmd_svn_check)

    p = sub.add_parser("repos", help="列出各产品的 bug 数量与其绑定的代码库")
    p.set_defaults(func=cmd_repos)

    p = sub.add_parser("bind-repo", help="把一个禅道产品绑定到它自己的 SVN 仓库")
    p.add_argument("product_id", type=int)
    p.add_argument("repo_url", nargs="?", default="", help="仓库根地址（不含 /trunk）")
    p.add_argument("--name", default="", help="产品名，留空则自动从库里取")
    p.add_argument("--trunk-path", default="/trunk")
    p.add_argument("--branch-root", default="/branches")
    p.add_argument("--working-copy", default="", help="留空则自动用 svn_workspace/p<产品ID>-<仓库哈希>")
    p.add_argument("--branch-prefix", default="", help="留空则用 BRANCH_PREFIX")
    p.add_argument("--build", default="", help="该库的编译命令，供 AI 修复后执行")
    p.add_argument("--test", default="", help="该库的测试命令，供 AI 修复后执行")
    p.add_argument("--note", default="", help="备注：代码结构、注意事项等，AI 会读")
    p.add_argument("--disabled", action="store_true", help="登记但先停用该绑定")
    p.add_argument("--unbind", action="store_true", help="删除该产品的绑定")
    p.set_defaults(func=cmd_bind_repo)

    p = sub.add_parser("config", help="查看当前配置（密码已脱敏）")
    p.set_defaults(func=cmd_config)

    p = sub.add_parser("doctor", help="禅道连通性分步诊断（按 ZENTAO_AUTH_MODE 选通道，只读）")
    p.set_defaults(func=cmd_doctor)

    p = sub.add_parser("session-probe", help="会话通道：登录并抓取各端点原始样本到 session_dump/")
    p.add_argument("--use-cookie", action="store_true", default=False,
                   help="强制使用 ZENTAO_COOKIE 而不是账号密码")
    p.set_defaults(func=cmd_session_probe)

    p = sub.add_parser("demo", help="写入/清除带 [DEMO] 前缀的演示数据，便于先看页面效果")
    p.add_argument("--clear", action="store_true")
    p.set_defaults(func=cmd_demo)

    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    db.ensure_db()
    try:
        args.func(args)
        return 0
    except SystemExit:
        raise
    except Exception as exc:  # CLI boundary: report machine readable failure
        print(json.dumps({"ok": False, "error": f"{type(exc).__name__}: {exc}"},
                         ensure_ascii=False), file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
