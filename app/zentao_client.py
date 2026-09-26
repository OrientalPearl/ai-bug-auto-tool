"""Zentao REST API client (v1, `api.php/v1`).

Auth flow
---------
1. ``POST /api.php/v1/tokens`` with ``{"account": ..., "password": ...}``
2. keep the returned token and send it as ``Token: <token>`` header.

Safety
------
This client intentionally implements **read** plus **comment** only.
Resolving / closing a bug is NOT implemented, because the workflow forbids
the AI from closing Zentao tickets automatically.
"""

from __future__ import annotations

import json
import re
import time
from typing import Any, Iterable

import requests

from . import zentao_parse as parse
from .config import get_settings


class ZentaoError(RuntimeError):
    """Any failure while talking to Zentao."""


def _strip_html(text: str) -> str:
    """Zentao stores repro steps as HTML; keep a readable plain text version."""
    if not text:
        return ""
    clean = re.sub(r"<br\s*/?>", "\n", text, flags=re.I)
    clean = re.sub(r"</p>|</div>|</li>", "\n", clean, flags=re.I)
    clean = re.sub(r"<[^>]+>", "", clean)
    clean = (
        clean.replace("&nbsp;", " ")
        .replace("&lt;", "<")
        .replace("&gt;", ">")
        .replace("&amp;", "&")
        .replace("&quot;", '"')
    )
    clean = re.sub(r"\n{3,}", "\n\n", clean)
    return clean.strip()


def _pick(row: dict, *names: str, default: Any = None) -> Any:
    """Fetch the first present key, tolerating camel/snake naming differences."""
    for name in names:
        if name in row and row[name] not in (None, ""):
            return row[name]
        snake = re.sub(r"(?<!^)(?=[A-Z])", "_", name).lower()
        if snake in row and row[snake] not in (None, ""):
            return row[snake]
    return default


def _as_list(payload: Any, *keys: str) -> list[dict]:
    """Zentao wraps collections under different keys depending on the version."""
    if payload is None:
        return []
    if isinstance(payload, list):
        return [x for x in payload if isinstance(x, dict)]
    if isinstance(payload, dict):
        for key in keys:
            value = payload.get(key)
            if isinstance(value, list):
                return [x for x in value if isinstance(x, dict)]
            if isinstance(value, dict):
                return [v for v in value.values() if isinstance(v, dict)]
        # last resort: any list of dicts in the payload
        for value in payload.values():
            if isinstance(value, list) and value and isinstance(value[0], dict):
                return value
    return []


class ZentaoClient:
    """Thin, dependency-light wrapper over the Zentao REST v1 endpoints."""

    def __init__(self, base_url: str | None = None, account: str | None = None,
                 password: str | None = None):
        settings = get_settings()
        settings.require_zentao()
        self.base_url = (base_url or settings.zentao_base_url).rstrip("/")
        self.account = account or settings.zentao_account
        self.password = password or settings.zentao_password
        self.timeout = settings.zentao_timeout
        self.verify = settings.zentao_verify_ssl
        self._token: str | None = None
        self._token_at = 0.0
        self.session = requests.Session()
        if settings.zentao_http_proxy:
            self.session.proxies = {
                "http": settings.zentao_http_proxy,
                "https": settings.zentao_http_proxy,
            }

    # ------------------------------------------------------------------
    # transport
    # ------------------------------------------------------------------
    def _headers(self, with_token: bool = True) -> dict[str, str]:
        headers = {"Content-Type": "application/json", "Accept": "application/json"}
        if with_token and self._token:
            # Official Zentao REST v1 uses `Token`; Bearer is accepted by some builds.
            headers["Token"] = self._token
            headers["Authorization"] = f"Bearer {self._token}"
        return headers

    def _request(self, method: str, path: str, *, json_body: dict | None = None,
                 params: dict | None = None, retried: bool = False) -> Any:
        url = f"{self.base_url}{path}"
        try:
            resp = self.session.request(
                method, url, json=json_body, params=params,
                headers=self._headers(), timeout=self.timeout, verify=self.verify,
            )
        except requests.RequestException as exc:
            raise ZentaoError(f"无法连接禅道 {url}: {exc}") from exc

        if resp.status_code == 401 and not retried:
            self._token = None
            self.login()
            return self._request(method, path, json_body=json_body, params=params, retried=True)
        if resp.status_code >= 400:
            raise ZentaoError(
                f"禅道接口失败 {method} {url} -> HTTP {resp.status_code}: {resp.text[:400]}"
            )
        if not resp.content:
            return {}
        try:
            return resp.json()
        except ValueError as exc:
            raise ZentaoError(f"禅道返回的不是 JSON: {resp.text[:200]}") from exc

    # ------------------------------------------------------------------
    # auth
    # ------------------------------------------------------------------
    def login(self, force: bool = False) -> str:
        """Exchange account/password for a token (cached for ~25 min).

        Zentao builds differ a lot, so this tolerates the known variants:
        * ``{"token": "..."}``
        * ``{"code": 200, "data": {"token": "..."}}``
        * ``{"data": "<raw token>"}``
        and retries once with a form-urlencoded body, because a few builds read
        ``$_POST`` instead of ``php://input``. A manually configured
        ``ZENTAO_TOKEN`` always wins over password login.
        """
        settings = get_settings()
        if self._token and not force and (time.time() - self._token_at) < 1500:
            return self._token
        if settings.zentao_token and not force:
            self._token = settings.zentao_token
            self._token_at = time.time()
            return self._token
        if not self.password:
            raise ZentaoError("禅道密码未配置：请在 .env 填写 ZENTAO_PASSWORD 或手工 ZENTAO_TOKEN")

        url = f"{self.base_url}{settings.zentao_tokens_path}"
        body = {"account": self.account, "password": self.password}
        attempts = (
            ("json", {"Accept": "application/json", "Content-Type": "application/json"}, body, None),
            ("form", {"Accept": "application/json", "Content-Type": "application/x-www-form-urlencoded"},
             None, body),
        )
        failures: list[str] = []
        for label, headers, json_body, form_body in attempts:
            try:
                resp = self.session.post(
                    url, json=json_body, data=form_body, headers=headers,
                    timeout=self.timeout, verify=self.verify,
                )
            except requests.RequestException as exc:
                failures.append(f"[{label}] 连接失败 {exc}")
                continue
            token, detail = self._extract_token(resp)
            if token:
                self._token = token
                self._token_at = time.time()
                return token
            failures.append(f"[{label}] {detail}")
        raise ZentaoError(
            "禅道登录响应里没有 token：" + " | ".join(failures)
            + "。排查：1) ZENTAO_BASE_URL 是否为禅道根地址（不要带 /zentao 之外的子路径）"
            " 2) 后台是否开启 RESTful API（禅道「后台-系统-二次开发/接口」）"
            " 3) 账号是否被锁定或开启了图形验证码（可改用 ZENTAO_TOKEN 手工令牌）"
        )

    @staticmethod
    def _extract_token(resp: requests.Response) -> tuple[str | None, str]:
        """Pull a token out of any known response shape, else describe the failure."""
        raw = (resp.text or "").strip()
        if not raw:
            return None, (
                f"HTTP {resp.status_code} 响应体为空"
                f"（最终 URL: {resp.url}；常见于被重定向到登录页或接口未开启）"
            )
        try:
            payload = json.loads(raw)
        except ValueError:
            head = " ".join(raw[:200].split())
            hint = "返回的是 HTML 页面（接口路径不对或未开启 RESTful API）" if head[0] == "<" else "返回的不是 JSON"
            return None, f"HTTP {resp.status_code} {hint}：{head}"

        snippet = json.dumps(payload, ensure_ascii=False)[:300]
        if isinstance(payload, dict):
            token = _pick(payload, "token", "Token", "tokenId")
            if not token:
                data = payload.get("data")
                if isinstance(data, dict):
                    token = _pick(data, "token", "Token", "tokenId")
                elif isinstance(data, str) and len(data) > 8:
                    token = data
            if token:
                return str(token), f"HTTP {resp.status_code} 登录成功"
            code = _pick(payload, "code", "errcode", "error_code", "status")
            message = _pick(payload, "message", "errmsg", "error", "msg", "errorInfo")
            detail = f"HTTP {resp.status_code}"
            if code is not None:
                detail += f" code={code}"
            if message:
                detail += f" message={message}"
            return None, f"{detail} 响应体：{snippet}"
        return None, f"HTTP {resp.status_code} 响应格式不认识：{snippet}"

    # ------------------------------------------------------------------
    # diagnosis
    # ------------------------------------------------------------------
    def probe(self) -> dict:
        """Step-by-step self check; never raises, returns a report per stage."""
        settings = get_settings()
        steps: list[dict] = []

        def step(name: str, ok: bool | None, detail: str) -> None:
            steps.append({"name": name, "ok": ok, "detail": detail})

        step("配置", settings.zentao_ready,
             f"base={self.base_url or '空'} account={self.account or '空'} "
             f"password={'已填' if self.password else '空'} token={'已填(优先)' if settings.zentao_token else '未填'}")

        try:
            resp = self.session.get(f"{self.base_url}/", timeout=self.timeout, verify=self.verify,
                                    headers={"Accept": "text/html"})
            text = (resp.text or "")[:4000]
            looks_zentao = ("zentao" in text.lower()) or ("禅道" in text)
            step("站点可达", resp.status_code < 500,
                 f"HTTP {resp.status_code}，最终 URL {resp.url}，"
                 f"{'识别到禅道页面特征' if looks_zentao else '未识别到禅道特征（可能地址指错端口/应用）'}"
                 f"，server={resp.headers.get('Server', '-')}")
        except requests.RequestException as exc:
            step("站点可达", False, f"连接失败：{exc}")

        try:
            token = self.login(force=True)
            step("登录取 token", True, f"成功，token 长度 {len(token)}，前 4 位 {token[:4]}****")
        except (ZentaoError, RuntimeError) as exc:
            step("登录取 token", False, str(exc))
            return {"ok": False, "steps": steps, "hint": "登录失败，后面几步已跳过"}

        try:
            products = self.list_products()
            step("产品列表", bool(products),
                 f"{len(products)} 个产品：" + ", ".join(f"{p['id']}:{p['name']}" for p in products[:8]))
        except (ZentaoError, RuntimeError) as exc:
            step("产品列表", False, str(exc))
            return {"ok": False, "steps": steps, "hint": "产品接口不通，检查账号权限"}

        target = list(settings.zentao_product_ids) or [p["id"] for p in products[:3]]
        try:
            rows = self.list_product_bugs(target[0]) if target else []
            mine = [r for r in rows if self._is_mine(self.normalize(r, product_id=target[0] if target else None))]
            step("Bug 列表", True,
                 f"产品 {target[0] if target else '-'} 返回 {len(rows)} 条，"
                 f"其中 assignedTo={self.account} 的 {len(mine)} 条")
        except (ZentaoError, RuntimeError) as exc:
            step("Bug 列表", False, str(exc))

        step("评论接口", None,
             "未实测（避免写数据）。默认依次尝试 /api.php/v1/bugs/{id}/comments、"
             "/comment、/api.php/v1/products/0/bugs/{id}/comments；不符时设 ZENTAO_COMMENT_PATH")
        return {"ok": True, "steps": steps, "hint": "全部关键步骤通过，可正常同步"}

    # ------------------------------------------------------------------
    # reads
    # ------------------------------------------------------------------
    def list_products(self) -> list[dict]:
        self.login()
        payload = self._request("GET", "/api.php/v1/products", params={"page": 1, "limit": 200})
        products = _as_list(payload, "products", "productList", "data")
        out = []
        for p in products:
            pid = _pick(p, "id", "productID", "product_id")
            if pid is None:
                continue
            out.append({
                "id": int(pid),
                "name": _pick(p, "name", "title", default=""),
                "status": _pick(p, "status", default=""),
                "bug_count": _pick(p, "bugCount", "bug_count", default=None),
            })
        return out

    def list_product_bugs(self, product_id: int, *, page: int = 1, limit: int = 100) -> list[dict]:
        payload = self._request(
            "GET", f"/api.php/v1/products/{product_id}/bugs",
            params={"page": page, "limit": limit, "status": get_settings().zentao_bug_status},
        )
        return _as_list(payload, "bugs", "bugList", "data")

    def fetch_bug_raw(self, bug_id: int) -> dict:
        payload = self._request("GET", f"/api.php/v1/bugs/{bug_id}")
        if isinstance(payload, dict) and isinstance(payload.get("bug"), dict):
            return payload["bug"]
        if isinstance(payload, dict) and isinstance(payload.get("data"), dict):
            return payload["data"]
        return payload if isinstance(payload, dict) else {}

    def get_bug_detail(self, bug_id: int, *, download: bool | None = None) -> dict:
        """Expanded detail for one bug (steps text, inline image references).

        The REST detail payload has no `actions`, so notes/comments only come
        through on the session channel; images stay as remote URLs here.
        """
        del download
        self.login()
        return parse.build_detail({"bug": self.fetch_bug_raw(bug_id)}, bug_id)

    def fetch_assigned_bugs(self, product_ids: Iterable[int] | None = None,
                            *, with_detail: bool = True,
                            max_pages: int = 10) -> list[dict]:
        """Collect active bugs assigned to the configured account.

        Rules: ``status=active`` + ``assignedTo == me`` (browseType=assigntome),
        which Zentao REST v1 does not filter server side, so we filter here.
        """
        settings = get_settings()
        if product_ids is None:
            product_ids = settings.zentao_product_ids or [p["id"] for p in self.list_products()]
        collected: dict[int, dict] = {}
        for pid in product_ids:
            for page in range(1, max_pages + 1):
                rows = self.list_product_bugs(int(pid), page=page)
                if not rows:
                    break
                for raw in rows:
                    normalized = self.normalize(raw, product_id=int(pid))
                    if not self._is_mine(normalized):
                        continue
                    if normalized["zentao_status"] and normalized["zentao_status"] != settings.zentao_bug_status:
                        continue
                    collected[normalized["zentao_id"]] = normalized
                if len(rows) < 100:
                    break
        # Enrich with the detail endpoint to obtain the full repro steps.
        if with_detail:
            for zid, bug in list(collected.items()):
                if bug.get("steps"):
                    continue
                try:
                    detail = self.fetch_bug_raw(zid)
                except ZentaoError:
                    continue
                collected[zid] = self.normalize(detail, product_id=bug.get("product_id"))
        return sorted(collected.values(), key=lambda b: (b["pri"], -b["severity"], b["zentao_id"]))

    # ------------------------------------------------------------------
    # writes (comment only)
    # ------------------------------------------------------------------
    def add_comment(self, bug_id: int, comment: str, *, extra: dict | None = None) -> dict:
        """Append a comment to a bug. Never resolves or closes it."""
        self.login()
        settings = get_settings()
        body: dict[str, Any] = {"comment": comment}
        if extra:
            body.update(extra)
        candidates = []
        if settings.zentao_comment_path:
            candidates.append(settings.zentao_comment_path.format(bug_id=bug_id))
        candidates += [
            f"/api.php/v1/bugs/{bug_id}/comments",
            f"/api.php/v1/bugs/{bug_id}/comment",
            f"/api.php/v1/products/0/bugs/{bug_id}/comments",
        ]
        errors: list[str] = []
        for path in candidates:
            try:
                result = self._request("POST", path, json_body=body)
                return {"endpoint": path, "result": result}
            except ZentaoError as exc:
                errors.append(str(exc))
        raise ZentaoError("禅道评论写入失败，可尝试设置 ZENTAO_COMMENT_PATH。原始错误:\n" + "\n".join(errors))

    # ------------------------------------------------------------------
    # helpers
    # ------------------------------------------------------------------
    def _is_mine(self, bug: dict) -> bool:
        assigned = (bug.get("assigned_to") or "").strip().lower()
        account = self.account.strip().lower()
        realname = account.split("@")[0]
        if not assigned:
            return False
        return assigned == account or assigned == realname or account in assigned or assigned in account

    def normalize(self, raw: dict, product_id: int | None = None) -> dict:
        """Map a raw Zentao payload onto our internal bug shape."""
        if not raw:
            return {}
        zid = _pick(raw, "id", "bugID", "bug_id")
        module = _pick(raw, "module", "moduleName", "module_name", default="")
        if isinstance(module, dict):
            module = _pick(module, "name", "title", default="")
        severity = _pick(raw, "severity", "severityNum", default=3)
        pri = _pick(raw, "pri", "priority", default=3)
        opened_build = _pick(raw, "openedBuild", "opened_build", "build", default="")
        if isinstance(opened_build, list):
            opened_build = ", ".join(str(x) for x in opened_build)
        return {
            "zentao_id": int(zid) if zid is not None else 0,
            "title": _strip_html(str(_pick(raw, "title", "name", default="")))[:500],
            "severity": int(severity) if str(severity).isdigit() else 3,
            "pri": int(pri) if str(pri).isdigit() else 3,
            "steps": _strip_html(str(_pick(raw, "steps", "step", "reproduction", default=""))),
            "module": str(module),
            "assigned_to": str(_pick(raw, "assignedTo", "assigned_to", "assigned", default="")),
            "opened_by": str(_pick(raw, "openedBy", "opened_by", "creator", default="")),
            "opened_build": str(opened_build),
            "zentao_status": str(_pick(raw, "status", "bugStatus", default="")),
            "zentao_resolution": str(_pick(raw, "resolution", default="")),
            "opened_date": str(_pick(raw, "openedDate", "opened_date", "opened", default="")),
            "product_id": int(product_id) if product_id else _pick(raw, "product", "productID", default=0) or 0,
            "raw": raw,
        }
