"""Shared parsing helpers for Zentao bug payloads (both REST and session channels).

Zentao stores the repro steps as HTML with inline screenshots
(`<img src="/file-read-171063.png">`) and keeps every note/comment in the
`actions` list. Naively stripping HTML loses both, so these helpers keep:

* a readable plain-text version of `steps` where images become explicit markers
* the ordered list of image/attachment references
* the comments ("备注") extracted from `actions`
"""

from __future__ import annotations

import html as html_lib
import re

# Zentao serves uploads as /file-read-<id>.<ext> (PATH_INFO) or as
# /index.php?m=file&f=read&t=<ext>&fileID=<id> (GET mode).
FILE_PATTERNS = (
    re.compile(r"/file-read-(\d+)\.(\w+)"),
    re.compile(r"fileID=(\d+)[^\"']*$", re.M),
)

IMG_TAG = re.compile(r"<img[^>]*>", flags=re.I)
SRC_ATTR = re.compile(r"src=[\"']([^\"']+)[\"']", flags=re.I)


def file_ref_from_url(url: str) -> dict | None:
    """`/file-read-171063.png` -> {file_id, ext, url, filename}."""
    if not url:
        return None
    match = re.search(r"/file-read-(\d+)\.(\w+)", url)
    if match:
        file_id, ext = match.group(1), match.group(2)
        return {"file_id": file_id, "ext": ext, "url": url, "filename": f"{file_id}.{ext}"}
    match = re.search(r"fileID=(\d+)", url)
    if match:
        file_id = match.group(1)
        ext_match = re.search(r"[&?]t=(\w+)", url)
        ext = ext_match.group(1) if ext_match else "png"
        return {"file_id": file_id, "ext": ext, "url": url, "filename": f"{file_id}.{ext}"}
    return None


def extract_images(steps_html: str) -> list[dict]:
    """Ordered image references found inside the steps HTML."""
    out: list[dict] = []
    for tag in IMG_TAG.findall(steps_html or ""):
        src = SRC_ATTR.search(tag)
        ref = file_ref_from_url(src.group(1)) if src else None
        if ref and ref["file_id"] not in {x["file_id"] for x in out}:
            out.append(ref)
    return out


def extract_links(steps_html: str) -> list[str]:
    hrefs = re.findall(r"href=[\"']([^\"']+)[\"']", steps_html or "")
    return [h for h in hrefs if not h.startswith("javascript")][:30]


def steps_to_text(steps_html: str) -> str:
    """Plain text with images and links kept as explicit markers."""
    if not steps_html:
        return ""
    text = steps_html
    # Mark images inline so the reader knows a screenshot belongs to that step.
    for index, tag in enumerate(IMG_TAG.findall(text), start=1):
        src = SRC_ATTR.search(tag)
        ref = file_ref_from_url(src.group(1)) if src else None
        label = f"[图片{index}: {ref['url']}]" if ref else f"[图片{index}]"
        text = text.replace(tag, f"\n{label}\n", 1)
    text = re.sub(r"<a[^>]*href=[\"']([^\"']+)[\"'][^>]*>(.*?)</a>", r"\2 <\1>",
                  text, flags=re.I | re.S)
    text = re.sub(r"<br\s*/?>", "\n", text, flags=re.I)
    text = re.sub(r"</p>|</div>|</li>|<li>", "\n", text, flags=re.I)
    text = re.sub(r"<[^>]+>", "", text)
    text = (text.replace("&nbsp;", " ").replace("&lt;", "<").replace("&gt;", ">")
                .replace("&quot;", '"').replace("&amp;", "&"))
    text = re.sub(r"[ \t]+\n", "\n", text)
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text.strip()


def actions_to_comments(actions: object, *, keep_history: bool = True) -> list[dict]:
    """Turn Zentao's `actions` map into the notes/comments a human reads.

    Keeps every action that carries a comment, plus the workflow-relevant ones
    (assigned / resolved / closed), newest last.
    """
    rows: list[dict] = []
    items = actions.values() if isinstance(actions, dict) else (actions or [])
    interesting = {"opened", "assigned", "resolved", "closed", "activated", "commented"}
    for action in items:
        if not isinstance(action, dict):
            continue
        comment = str(action.get("comment") or "").strip()
        name = str(action.get("action") or "")
        if not comment and name not in interesting:
            continue
        entry = {
            "actor": str(action.get("actor") or ""),
            "action": name,
            "date": str(action.get("date") or ""),
            "comment": re.sub(r"<[^>]+>", " ", comment).strip(),
        }
        history = action.get("history") if keep_history else None
        if isinstance(history, list) and history:
            entry["changed"] = [
                f"{h.get('field')}:{h.get('old')}->{h.get('new')}"
                for h in history[:12] if isinstance(h, dict)
            ]
        rows.append(entry)
    rows.sort(key=lambda r: r["date"])
    return rows


def files_to_attachments(files: object, images: list[dict]) -> list[dict]:
    """Merge uploaded `bug.files` with the inline images into one list."""
    out: list[dict] = []
    items = files.values() if isinstance(files, dict) else (files or [])
    for item in items:
        if not isinstance(item, dict):
            continue
        file_id = str(item.get("id") or item.get("fileID") or "")
        if not file_id:
            continue
        ext = str(item.get("extension") or item.get("ext") or "")
        name = str(item.get("title") or item.get("name") or "")
        out.append({
            "file_id": file_id,
            "ext": ext,
            "name": name or f"{file_id}.{ext}",
            "url": f"/file-read-{file_id}.{ext}" if ext else f"/file-read-{file_id}",
            "filename": f"{file_id}.{ext}" if ext else file_id,
            "kind": "upload",
        })
    known = {row["file_id"] for row in out}
    for image in images:
        if image["file_id"] not in known:
            out.append({**image, "name": image["filename"], "kind": "inline"})
    return out


def realname(users: object, account: str) -> str:
    """Resolve an account to its real name using the `users` map if present."""
    if not account or not isinstance(users, dict):
        return account
    node = users.get(account)
    if isinstance(node, dict):
        return str(node.get("realname") or node.get("account") or account)
    if isinstance(node, str):
        return node or account
    return account


def module_name(node: dict, bug: dict) -> str:
    """Prefer a readable module path, fall back to the numeric module id."""
    module_id = str(bug.get("module") or "")
    module = node.get("bugModule")
    if isinstance(module, dict) and module.get("name"):
        return str(module["name"])
    path = node.get("modulePath")
    if isinstance(path, dict) and module_id and path.get(module_id):
        return str(path[module_id])
    if isinstance(path, list):
        names = [str(x) for x in path if isinstance(x, str) and x]
        if names:
            return " / ".join(names[-2:])
    return module_id


def build_detail(node: dict, zentao_id: int | None = None) -> dict:
    """Expand a `/bug-view-*` payload into the columns the `bugs` table stores.

    Shared by the session and REST channels so both end up with the same shape;
    the local attachment paths are filled in afterwards by the session client.
    """
    bug = node.get("bug") if isinstance(node.get("bug"), dict) else node
    steps_html = str(bug.get("steps") or "")
    images = extract_images(steps_html)
    assigned = str(bug.get("assignedTo") or "")
    product = node.get("product") if isinstance(node.get("product"), dict) else {}
    return {
        "zentao_id": int(bug.get("id") or zentao_id or 0),
        "title": _plain(str(bug.get("title") or ""))[:500],
        "severity": int(bug.get("severity") or 3),
        "pri": int(bug.get("pri") or 3),
        "steps": steps_to_text(steps_html),
        "steps_html": steps_html,
        "comments": actions_to_comments(node.get("actions")),
        "attachments": files_to_attachments(bug.get("files"), images),
        "module": module_name(node, bug),
        "product_name": _plain(str(product.get("name") or node.get("productName") or "")),
        "assigned_to": assigned,
        "assigned_to_name": realname(node.get("users"), assigned),
        "opened_by": str(bug.get("openedBy") or ""),
        "opened_build": str(bug.get("openedBuild") or ""),
        "zentao_status": str(bug.get("status") or ""),
        "zentao_resolution": str(bug.get("resolution") or ""),
        "opened_date": str(bug.get("openedDate") or ""),
        "raw_json": bug,
    }


def build_task_detail(node: dict, zentao_id: int | None = None) -> dict:
    """Expand a `/task-view-*` payload into the columns the `tasks` table stores.

    A task carries almost nothing itself: its real requirement text sits in the
    linked story's spec, and the product (which decides the repository) only shows
    up on the action rows and the module path. Both are folded in here so the
    executor sees one readable body and a product id it can bind a repo to.
    """
    task = node.get("task") if isinstance(node.get("task"), dict) else node
    desc_html = str(task.get("desc") or "")
    spec_html = str(task.get("storySpec") or "")
    parts: list[str] = []
    if _has_body(desc_html):
        parts.append(desc_html)
    if _has_body(spec_html):
        parts.append("<p>[需求说明 storySpec]</p>" + spec_html)
    steps_html = "\n".join(parts)
    images = extract_images(steps_html)
    assigned = str(task.get("assignedTo") or "")
    project = node.get("project") if isinstance(node.get("project"), dict) else {}
    product = node.get("product") if isinstance(node.get("product"), dict) else {}
    deadline = str(task.get("deadline") or "")
    if deadline.startswith("0000"):
        deadline = ""
    return {
        "zentao_id": int(task.get("id") or zentao_id or 0),
        "title": _plain(str(task.get("name") or ""))[:500],
        "pri": int(task.get("pri") or 3),
        "steps": steps_to_text(steps_html),
        "steps_html": steps_html,
        "comments": actions_to_comments(node.get("actions")),
        "attachments": files_to_attachments(task.get("files"), images),
        "module": module_path_names(node.get("modulePath")),
        "project_id": int(task.get("project") or project.get("id") or 0),
        "project_name": _plain(str(project.get("name") or task.get("projectName") or "")),
        "story_id": int(task.get("storyID") or task.get("story") or 0),
        "story_title": _plain(str(task.get("storyTitle") or "")),
        "product_id": task_product_id(node, task),
        "product_name": _plain(str(product.get("name") or "")),
        "assigned_to": assigned,
        "assigned_to_name": realname(node.get("users"), assigned),
        "opened_by": str(task.get("openedBy") or ""),
        "opened_date": str(task.get("openedDate") or ""),
        "zentao_status": str(task.get("status") or ""),
        "deadline": deadline,
        "estimate": str(task.get("estimate") or ""),
        "need_confirm": bool(task.get("needConfirm")),
        "raw_json": task,
    }


def task_product_id(node: dict, task: dict) -> int:
    """Which product a task belongs to, i.e. which repository must carry the fix.

    Zentao keeps it off the task row: the action log records it as `",25,"` and the
    module path records the story tree's root, which is the same product id.
    """
    items = node.get("actions")
    rows = items.values() if isinstance(items, dict) else (items or [])
    for action in rows:
        if not isinstance(action, dict):
            continue
        for digits in re.findall(r"\d+", str(action.get("product") or "")):
            if int(digits) > 0:
                return int(digits)
    path = node.get("modulePath")
    nodes = path.values() if isinstance(path, dict) else (path or [])
    for module in nodes:
        if isinstance(module, dict) and str(module.get("root") or "").isdigit():
            return int(module["root"])
    return int(task.get("product") or 0) if str(task.get("product") or "").isdigit() else 0


def module_path_names(path: object) -> str:
    """"对象 / URL识别" from Zentao's `modulePath` list; "" when there is none."""
    nodes = path.values() if isinstance(path, dict) else (path or [])
    names = [_plain(str(node.get("name"))).strip()
             for node in nodes if isinstance(node, dict) and node.get("name")]
    return " / ".join([n for n in names if n][-2:])


def _has_body(html: str) -> bool:
    """True when an editor field holds something a human would read."""
    return bool(_plain(html or "").strip())


def _plain(text: str) -> str:
    """Drop tags and decode entities; Zentao escapes names twice ("A&amp;amp;B")."""
    clean = re.sub(r"<[^>]+>", " ", text or "")
    for _ in range(3):
        decoded = html_lib.unescape(clean)
        if decoded == clean:
            break
        clean = decoded
    return " ".join(clean.split())
