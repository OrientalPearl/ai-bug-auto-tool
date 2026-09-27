"""Zentao web-session client: an alternative to the REST (`api.php`) channel.

Why this exists
---------------
Some Zentao deployments put a unified-login / OAuth gateway in front of
`api.php`, which makes the documented `POST /api.php/v1/tokens` flow unusable
(it answers `{"errcode":401,"errmsg":"缺少code参数"}` for every path).
The regular web endpoints (`/user-login.html`, `/xxx.json`) are still served by
Zentao itself, so this client authenticates like the browser does and then
reads the same pages the UI uses.

Two authentication sources, in order of preference:

1. ``ZENTAO_COOKIE``  -- reuse a `zentaosid` copied from an already logged-in
   browser. No password is ever sent, so a wrong password cannot lock the
   account. Recommended when SSO/LDAP is in play.
2. account/password  -- byte-for-byte replay of the browser login request:
   ``password = md5(md5(plain) + verifyRand)`` plus `passwordStrength`,
   `referer`, `verifyRand`, `keepLogin` and the `X-Requested-With` header.
   Only used when ``ZENTAO_AUTH_MODE=session`` is set explicitly, because every
   failure costs one attempt against the server's lockout counter.
"""

from __future__ import annotations

import hashlib
import html as html_lib
import json
import re
from pathlib import Path
from typing import Any

import requests

from . import zentao_parse as parse
from .config import PROJECT_ROOT, get_settings
from .net_retry import call_with_retry
from .zentao_client import ZentaoError, _pick, _strip_html

BROWSER_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
)

# Rows per request on `/my-bug-*`; Zentao caps this server side anyway.
PER_PAGE = 100

# `.env` browseType -> the `type` argument of the `my->bug()` page.
MY_BUG_TYPE_BY_BROWSE = {"assigntome": "assignedTo", "assignedtome": "assignedTo",
                         "active": "assignedTo", "closedbyme": "closed",
                         "resolvedbyme": "resolved"}

# Endpoints worth probing, in the order a Zentao 12.x install usually answers.
BUG_URL_CANDIDATES = (
    "/my-bug.json",
    "/my-bug.html",
    "/bug-browse-assigntome.json",
    "/bug-browse-assigntome.html",
    "/my-index.json",
)
PRODUCT_URL_CANDIDATES = (
    "/product-index.json",
    "/product-browse.json",
    "/product-index.html",
)


def _dump_dir() -> Path:
    return PROJECT_ROOT / "session_dump"


def _clean_name(value: Any) -> str:
    """Zentao double-escapes names ("A&amp;amp;B"), so decode until it settles."""
    text = _strip_html(str(value if value is not None else ""))
    for _ in range(3):
        decoded = html_lib.unescape(text)
        if decoded == text:
            break
        text = decoded
    return re.sub(r"\s+", " ", text).strip()


def _dump(name: str, text: str) -> str:
    """Persist a raw response so parsing can be calibrated offline."""
    target = _dump_dir()
    target.mkdir(parents=True, exist_ok=True)
    safe = re.sub(r"[^0-9A-Za-z_.-]", "_", name) + ".txt"
    (target / safe).write_text(text, encoding="utf-8")
    return str(target / safe)


def _md5(text: str) -> str:
    return hashlib.md5(text.encode("utf-8")).hexdigest()


def _password_strength(password: str) -> int:
    """Mirror the buckets Zentao's `computePasswordStrength()` posts."""
    score = 0
    if len(password) >= 8:
        score += 1
    if re.search(r"[a-z]", password) and re.search(r"[A-Z]", password):
        score += 1
    if re.search(r"\d", password):
        score += 1
    if re.search(r"[^0-9a-zA-Z]", password):
        score += 1
    return max(score, 1)


class ZentaoSessionClient:
    """Duck-typed replacement for :class:`~app.zentao_client.ZentaoClient`."""

    def __init__(self, *, use_cookie: bool | None = None):
        settings = get_settings()
        self.base_url = settings.zentao_base_url.rstrip("/")
        self.account = settings.zentao_account
        self.password = settings.zentao_password
        self.timeout = settings.zentao_timeout
        self.verify = settings.zentao_verify_ssl
        self.cookie = settings.zentao_cookie.strip()
        if not self.base_url:
            raise ZentaoError("ZENTAO_BASE_URL 未配置")
        self.use_cookie = (bool(self.cookie) if use_cookie is None else use_cookie)
        if not self.use_cookie and not (self.account and self.password):
            raise ZentaoError("会话模式需要 ZENTAO_COOKIE，或 ZENTAO_ACCOUNT + ZENTAO_PASSWORD")
        self.session = requests.Session()
        self.session.verify = self.verify
        if not self.verify:
            # Self-hosted Zentao usually has a private CA; keep the console readable.
            requests.packages.urllib3.disable_warnings(
                requests.packages.urllib3.exceptions.InsecureRequestWarning
            )
        self.session.headers.update({
            "User-Agent": BROWSER_UA,
            "Accept-Language": "zh-cn,zh;q=0.8",
        })
        if self.use_cookie:
            self._install_cookie(self.cookie)
        self._authenticated: bool | None = None
        self._logged_in = False
        self._login_error = ""

    # ------------------------------------------------------------------
    # auth
    # ------------------------------------------------------------------
    def _install_cookie(self, raw: str) -> None:
        """Accept either a bare `zentaosid` value or a full cookie header."""
        if "=" in raw:
            for part in raw.split(";"):
                if "=" in part:
                    key, _, value = part.partition("=")
                    self.session.cookies.set(key.strip(), value.strip())
        else:
            self.session.cookies.set("zentaosid", raw)

    def login(self, force: bool = False) -> str:
        """Return the session id, sending the password at most once per client.

        Retrying a failed password within one run would burn extra slots of the
        server's lockout counter, so the first failure is remembered and replayed.
        """
        if self.use_cookie:
            sid = self.session.cookies.get("zentaosid") or ""
            if not sid:
                raise ZentaoError("ZENTAO_COOKIE 里没有 zentaosid")
            return sid
        if self._logged_in and not force:
            return self.session.cookies.get("zentaosid") or ""
        if self._login_error and not force:
            raise ZentaoError(self._login_error)
        try:
            sid = self._password_login()
        except ZentaoError as exc:
            self._login_error = str(exc)
            raise
        self._logged_in = True
        self._login_error = ""
        self._authenticated = True
        return sid

    def _password_login(self) -> str:
        page = self._raw_get("/user-login.html")
        match = re.search(r"id='verifyRand'[^>]*value='(\d+)'", page) or \
            re.search(r'id="verifyRand"[^>]*value="(\d+)"', page)
        verify_rand = match.group(1) if match else ""
        if not verify_rand:
            raise ZentaoError("登录页没抓到 verifyRand，页面结构可能已变化（已存 session_dump/login_page.txt）")
        plain = self.password
        payload = {
            "account": self.account,
            "password": _md5(_md5(plain) + verify_rand),
            "passwordStrength": str(_password_strength(plain)),
            "referer": "/",
            "verifyRand": verify_rand,
            "keepLogin": "0",
        }
        resp = self.session.post(
            f"{self.base_url}/user-login.html", data=payload, timeout=self.timeout,
            headers={
                "Referer": f"{self.base_url}/user-login.html",
                "Origin": self.base_url,
                "X-Requested-With": "XMLHttpRequest",
                "Content-Type": "application/x-www-form-urlencoded; charset=UTF-8",
            },
        )
        body = (resp.text or "").strip()
        try:
            data = json.loads(body)
        except ValueError:
            data = {}
        if isinstance(data, dict) and data.get("result") == "fail":
            raise ZentaoError(
                f"禅道会话登录失败：{data.get('message') or data.get('reason') or body[:200]}"
                "（账号或密码不正确；连续失败会锁账号，建议改用 ZENTAO_COOKIE）"
            )
        if resp.status_code >= 400 or ("fail" in body.lower() and "success" not in body.lower()):
            raise ZentaoError(f"禅道会话登录异常 HTTP {resp.status_code}: {body[:200]}")
        return self.session.cookies.get("zentaosid") or ""

    # ------------------------------------------------------------------
    # transport
    # ------------------------------------------------------------------
    def _raw_get(self, path: str) -> str:
        try:
            resp = call_with_retry(
                lambda: self.session.get(f"{self.base_url}{path}", timeout=self.timeout,
                                         headers={"Accept": "text/html,application/json"}),
                label=f"禅道会话 GET {path}",
            )
        except requests.RequestException as exc:
            raise ZentaoError(f"无法连接禅道 {path}: {exc}") from exc
        return resp.text or ""

    def get(self, path: str) -> dict[str, Any]:
        """GET a `.json` page and unwrap Zentao's AJAX envelope."""
        try:
            resp = call_with_retry(
                lambda: self.session.get(
                    f"{self.base_url}{path}", timeout=self.timeout,
                    headers={"Accept": "application/json", "X-Requested-With": "XMLHttpRequest"},
                ),
                label=f"禅道会话 GET {path}",
            )
        except requests.RequestException as exc:
            raise ZentaoError(f"无法连接禅道 {path}: {exc}") from exc
        text = resp.text or ""
        if resp.status_code >= 400:
            raise ZentaoError(f"禅道 {path} -> HTTP {resp.status_code}: {text[:200]}")
        if "locate" in text[:400] and "user-login" in text:
            raise ZentaoError("会话已失效（被要求重新登录）：请重新粘贴 ZENTAO_COOKIE")
        try:
            payload = json.loads(text)
        except ValueError:
            return {"raw": text}
        if isinstance(payload, dict) and isinstance(payload.get("data"), str):
            inner = payload["data"]
            try:
                return {"json": json.loads(inner)}
            except ValueError:
                return {"html": inner}
        return {"json": payload} if isinstance(payload, (dict, list)) else {"raw": text}

    def is_authenticated(self) -> bool:
        if self._authenticated is None:
            try:
                text = self._raw_get("/my-bug.json")
                self._authenticated = "locate" not in text[:400] or "user-login" not in text
            except ZentaoError:
                self._authenticated = False
        return self._authenticated

    # ------------------------------------------------------------------
    # calibration
    # ------------------------------------------------------------------
    def probe_endpoints(self, paths: tuple[str, ...] | None = None) -> list[dict]:
        """Read-only sweep used to calibrate the parsers. Dumps raw bodies."""
        out = []
        for path in (paths or BUG_URL_CANDIDATES + PRODUCT_URL_CANDIDATES):
            entry: dict[str, Any] = {"path": path}
            try:
                resp = self.session.get(
                    f"{self.base_url}{path}", timeout=self.timeout,
                    headers={"Accept": "text/html,application/json",
                             "X-Requested-With": "XMLHttpRequest"},
                )
                text = resp.text or ""
                entry.update({
                    "status": resp.status_code,
                    "length": len(text),
                    "content_type": resp.headers.get("Content-Type", "-"),
                    "session_ok": not ("locate" in text[:400] and "user-login" in text),
                    "bug_ids": sorted({int(x) for x in re.findall(r"bug-view-(\d+)", text)})[:20],
                    "saved_to": _dump(path, text),
                })
            except requests.RequestException as exc:
                entry["error"] = str(exc)
            out.append(entry)
        return out

    # ------------------------------------------------------------------
    # reads -- calibrated against the live responses in session_dump/
    # ------------------------------------------------------------------
    def _json_data(self, path: str) -> dict:
        """Fetch a `.json` page and return its unwrapped `data` object."""
        payload = self.get(path)
        node = payload.get("json")
        if isinstance(node, dict):
            return node
        _dump(path, payload.get("html") or payload.get("raw") or "")
        raise ZentaoError(f"{path} 返回的不是预期 JSON（原始内容已存 session_dump/）")

    def list_products(self) -> list[dict]:
        """`/product-index.json` -> data.products is a `{id: name}` mapping."""
        self.login()
        data = self._json_data("/product-index.json")
        products = data.get("products") or {}
        out: list[dict] = []
        if isinstance(products, dict):
            for key, name in products.items():
                if not str(key).isdigit():
                    continue
                out.append({
                    "id": int(key),
                    "name": _clean_name(name),
                    "status": "",
                    "bug_count": None,
                })
        elif isinstance(products, list):
            for row in products:
                pid = _pick(row, "id", "productID")
                if pid and str(pid).isdigit():
                    out.append({"id": int(pid), "name": _clean_name(_pick(row, "name", default="")),
                                "status": "", "bug_count": None})
        return sorted(out, key=lambda p: p["id"])

    def module_names(self) -> dict[int, str]:
        """Best effort `{moduleID: name}`.

        Zentao only ships this map per product on browse pages, so bugs of other
        products keep their numeric module id -- one request per product is not
        worth it during a sync.
        """
        flat: dict[int, str] = {}
        for path in ("/bug-browse-assigntome.json", "/my-bug.json"):
            try:
                data = self._json_data(path)
            except ZentaoError:
                continue
            for source in (data.get("modulePairs"), data.get("modules")):
                if isinstance(source, dict):
                    for mid, name in source.items():
                        if str(mid).isdigit() and isinstance(name, str):
                            flat[int(mid)] = _strip_html(name)
                elif isinstance(source, list):
                    for item in source:
                        if isinstance(item, dict) and str(item.get("id") or "").isdigit():
                            flat[int(item["id"])] = _strip_html(str(item.get("name") or ""))
            if flat:
                break
        return flat

    def fetch_assigned_bugs(self, product_ids: Any = None, *, with_detail: bool = True,
                            max_pages: int = 20) -> list[dict]:
        """Page through `/my-bug-assignedTo-...json`.

        One endpoint already carries every field the workflow needs, including
        `steps`, so no per-bug detail request is required.
        """
        settings = get_settings()
        self.login()
        bug_type = MY_BUG_TYPE_BY_BROWSE.get(settings.zentao_browse_type, "assignedTo")
        wanted = {int(p) for p in (product_ids or []) if str(p).isdigit()}
        modules = self.module_names() if with_detail else {}
        # One cheap request gives every product id -> name, which is what the
        # per-product repository binding is keyed on.
        try:
            names = {p["id"]: p["name"] for p in self.list_products()}
        except ZentaoError:
            names = {}
        collected: dict[int, dict] = {}
        for page in range(1, max_pages + 1):
            path = f"/my-bug-{bug_type}-pri_asc-0-{PER_PAGE}-{page}.json"
            data = self._json_data(path)
            rows = data.get("bugs") or []
            if isinstance(rows, dict):
                rows = [v for v in rows.values() if isinstance(v, dict)]
            for row in rows:
                if not isinstance(row, dict) or str(row.get("deleted") or "0") != "0":
                    continue
                bug = self.normalize(row, modules=modules)
                bug["product_name"] = names.get(bug["product_id"], "")
                if not bug["zentao_id"]:
                    continue
                if wanted and bug["product_id"] not in wanted:
                    continue
                if settings.zentao_bug_status and bug["zentao_status"] != settings.zentao_bug_status:
                    continue
                if bug["assigned_to"] and bug["assigned_to"].lower() != self.account.lower():
                    continue
                collected[bug["zentao_id"]] = bug
            pager = data.get("pager") or {}
            total_pages = int(pager.get("pageTotal") or page)
            if not rows or page >= total_pages:
                break
        return sorted(collected.values(),
                      key=lambda b: (b["pri"], -b["severity"], b["zentao_id"]))

    # ------------------------------------------------------------------
    # bug detail: screenshots, notes, attachments
    # ------------------------------------------------------------------
    def get_bug_detail(self, zentao_id: int, *, download: bool | None = None) -> dict:
        """Fetch `/bug-view-{id}.json` and expand it into storable fields.

        The list endpoint only carries the raw steps HTML; the notes ("备注")
        live in `actions` and the screenshots only resolve through an
        authenticated file download, which is what this adds.
        """
        self.login()
        node = self._json_data(f"/bug-view-{zentao_id}.json")
        detail = parse.build_detail(node, zentao_id)
        if download is None:
            download = get_settings().download_attachments
        if download:
            self.download_attachments(zentao_id, detail["attachments"])
        # Point the inline markers at the local copy so the AI can open them.
        for item in detail["attachments"]:
            web = item.get("web_path")
            url = item.get("url")
            if web and url:
                detail["steps"] = detail["steps"].replace(url, web)
        return detail

    def download_attachments(self, bug_id: int, attachments: list[dict]) -> list[dict]:
        """Save screenshots/uploads under `attachments/zentao-<id>/`."""
        settings = get_settings()
        target = Path(settings.attachment_dir) / f"zentao-{bug_id}"
        for item in attachments:
            url = str(item.get("url") or "")
            if not url:
                continue
            filename = str(item.get("filename") or f"{item.get('file_id')}.bin")
            dest = target / filename
            item["web_path"] = f"/files/{bug_id}/{filename}"
            if dest.exists() and dest.stat().st_size > 0:
                item["local_path"] = str(dest)
                item["size"] = dest.stat().st_size
                continue
            try:
                resp = call_with_retry(
                    lambda: self.session.get(f"{self.base_url}{url}", timeout=self.timeout),
                    label=f"禅道附件 GET {url}",
                )
            except requests.RequestException as exc:
                item["error"] = str(exc)
                continue
            if resp.status_code >= 400 or not resp.content:
                item["error"] = f"HTTP {resp.status_code}"
                continue
            target.mkdir(parents=True, exist_ok=True)
            dest.write_bytes(resp.content)
            item["local_path"] = str(dest)
            item["size"] = len(resp.content)
        return attachments

    # ------------------------------------------------------------------
    # writes
    # ------------------------------------------------------------------
    def add_comment(self, bug_id: int, comment: str, *, extra: dict | None = None) -> dict:
        """Post one comment. Deliberately NOT retried: a lost 5xx response may
        still have been applied, and a duplicate comment on a tracked bug is
        worse than the caller reporting a failed push."""
        del extra
        self.login()
        candidates = [f"/bug-comment-{bug_id}.json", f"/bug-comment-{bug_id}.html"]
        errors: list[str] = []
        for path in candidates:
            try:
                resp = self.session.post(
                    f"{self.base_url}{path}", data={"comment": comment}, timeout=self.timeout,
                    headers={"Referer": f"{self.base_url}/bug-view-{bug_id}.html",
                             "Origin": self.base_url,
                             "X-Requested-With": "XMLHttpRequest",
                             "Content-Type": "application/x-www-form-urlencoded; charset=UTF-8"},
                )
            except requests.RequestException as exc:
                errors.append(f"{path}: {exc}")
                continue
            if resp.status_code < 400 and "locate" not in (resp.text or "")[:400]:
                return {"endpoint": path, "result": (resp.text or "")[:200]}
            errors.append(f"{path}: HTTP {resp.status_code} {(resp.text or '')[:120]}")
        raise ZentaoError("会话评论写入失败：" + " | ".join(errors))

    # ------------------------------------------------------------------
    # helpers
    # ------------------------------------------------------------------
    def normalize(self, raw: dict, *, modules: dict[int, str] | None = None) -> dict:
        """Map a Zentao bug row onto the internal bug shape.

        Field names come from the live `/my-bug-*.json` payload, which carries
        `steps` inline, so a bug row is enough on its own.
        """
        modules = modules or {}
        mid = str(raw.get("module") or "")
        return {
            "zentao_id": int(raw.get("id") or 0),
            "title": _strip_html(str(raw.get("title") or ""))[:500],
            "severity": int(raw.get("severity") or 3),
            "pri": int(raw.get("pri") or 3),
            "steps": _strip_html(str(raw.get("steps") or "")),
            "module": modules.get(int(mid), mid) if mid.isdigit() else mid,
            "assigned_to": str(raw.get("assignedTo") or ""),
            "opened_by": str(raw.get("openedBy") or ""),
            "opened_build": str(raw.get("openedBuild") or ""),
            "zentao_status": str(raw.get("status") or ""),
            "zentao_resolution": str(raw.get("resolution") or ""),
            "opened_date": str(raw.get("openedDate") or ""),
            "product_id": int(raw.get("product") or 0),
            "raw": raw,
        }

    def probe(self) -> dict:
        """Session-aware diagnosis, same report shape as the REST client."""
        settings = get_settings()
        steps: list[dict] = []

        def step(name: str, ok: Any, detail: str) -> None:
            steps.append({"name": name, "ok": ok, "detail": detail})

        source = "ZENTAO_COOKIE" if self.use_cookie else "账号密码"
        step("认证方式", True, f"session（{source}）；密码发送次数：{'0' if self.use_cookie else '1'}")
        try:
            self.login(force=not self.use_cookie)
            sid = self.session.cookies.get("zentaosid") or ""
            step("会话", bool(sid), f"zentaosid={sid[:6]}****（长度 {len(sid)}）")
        except ZentaoError as exc:
            step("会话", False, str(exc))
            return {"ok": False, "steps": steps, "hint": "会话建立失败"}

        ok = self.is_authenticated()
        step("登录态校验", ok,
             "已能读取 /my-bug.json" if ok else "仍被要求登录：cookie 可能已过期或被 IP/UA 绑定")
        if not ok:
            return {"ok": False, "steps": steps, "hint": "请重新从浏览器复制 zentaosid 到 .env"}

        sweep = self.probe_endpoints()
        usable = [e["path"] for e in sweep if e.get("session_ok")]
        step("可用端点", bool(usable),
             "；".join(f"{e['path']}({e.get('length')}B,bug数={len(e.get('bug_ids') or [])})"
                       for e in sweep if e.get("session_ok")) or "没有命中已知端点")
        dumped = ", ".join(sorted({Path(e["saved_to"]).name for e in sweep if e.get("saved_to")}))
        step("原始样本", None, f"已写入 session_dump/：{dumped}")
        try:
            products = self.list_products()
            step("产品解析", bool(products), f"{len(products)} 个："
                 + ", ".join(f"{p['id']}:{p['name']}" for p in products[:8]))
        except ZentaoError as exc:
            products = []
            step("产品解析", False, str(exc))
        try:
            bugs = self.fetch_assigned_bugs([p["id"] for p in products] or None,
                                            with_detail=False)
            sample = bugs[0] if bugs else None
            step("Bug 解析", bool(bugs),
                 f"{len(bugs)} 条指派给我的 {settings.zentao_bug_status or 'active'} bug；"
                 + (f"样例 #{sample['zentao_id']} P{sample['pri']}/S{sample['severity']} "
                    f"模块={sample['module'] or '-'} 迭代={sample['opened_build'] or '-'} "
                    f"描述长度={len(sample['steps'])}" if sample else "（空）"))
        except ZentaoError as exc:
            step("Bug 解析", False, str(exc))
        return {"ok": True, "steps": steps, "hint": "会话通道可用，同步按钮已可正常拉取"}


def make_client() -> Any:
    """Pick the client for the configured auth mode.

    `auto` deliberately never fires a password login: a wrong password costs a
    slot in the server's lockout counter, so that has to be an explicit choice.
    """
    settings = get_settings()
    settings.require_zentao()
    mode = settings.zentao_auth_mode or "auto"
    if mode == "rest":
        from .zentao_client import ZentaoClient

        return ZentaoClient()
    if mode in {"session", "cookie"}:
        return ZentaoSessionClient(use_cookie=(mode == "cookie") or bool(settings.zentao_cookie))
    if settings.zentao_cookie:
        return ZentaoSessionClient(use_cookie=True)
    from .zentao_client import ZentaoClient

    return ZentaoClient()
