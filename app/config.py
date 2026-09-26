"""Runtime configuration loaded from environment variables / .env file.

Secrets (Zentao account & password, SVN credentials) are NEVER hard coded.
They are read from a `.env` file located at the project root, which is
ignored by git/svn.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path

from dotenv import load_dotenv

# Project root = the folder that contains the `app` package.
PROJECT_ROOT = Path(__file__).resolve().parent.parent

# Load .env once at import time; existing OS env variables always win.
load_dotenv(PROJECT_ROOT / ".env", override=False)


def _env_str(name: str, default: str = "") -> str:
    value = os.environ.get(name)
    if value is None:
        return default
    return value.strip()


def _env_int(name: str, default: int) -> int:
    raw = _env_str(name)
    if not raw:
        return default
    try:
        return int(raw)
    except ValueError:
        return default


def _env_bool(name: str, default: bool = False) -> bool:
    raw = _env_str(name).lower()
    if not raw:
        return default
    return raw in {"1", "true", "yes", "y", "on"}


def _split_list(raw: str) -> list[str]:
    return [item.strip() for item in raw.split(",") if item.strip()]


@dataclass
class Settings:
    """All tunables of the system, resolved from environment variables."""

    # --- SQLite ---
    db_path: Path = field(
        default_factory=lambda: Path(
            _env_str("DB_PATH", str(PROJECT_ROOT / "bug_system.db"))
        )
    )

    # --- Zentao ---
    zentao_base_url: str = field(default_factory=lambda: _env_str("ZENTAO_BASE_URL"))
    zentao_account: str = field(default_factory=lambda: _env_str("ZENTAO_ACCOUNT"))
    zentao_password: str = field(default_factory=lambda: _env_str("ZENTAO_PASSWORD"))
    # Optional explicit product ids; when empty the client discovers them.
    zentao_product_ids: list[int] = field(
        default_factory=lambda: [
            int(x) for x in _split_list(_env_str("ZENTAO_PRODUCT_IDS")) if x.isdigit()
        ]
    )
    zentao_bug_status: str = field(default_factory=lambda: _env_str("ZENTAO_BUG_STATUS", "active"))
    zentao_browse_type: str = field(default_factory=lambda: _env_str("ZENTAO_BROWSE_TYPE", "assigntome"))
    zentao_verify_ssl: bool = field(default_factory=lambda: _env_bool("ZENTAO_VERIFY_SSL", False))
    zentao_timeout: int = field(default_factory=lambda: _env_int("ZENTAO_TIMEOUT", 20))
    # Override when a Zentao build exposes comment endpoints on another path.
    zentao_comment_path: str = field(default_factory=lambda: _env_str("ZENTAO_COMMENT_PATH"))
    zentao_http_proxy: str = field(default_factory=lambda: _env_str("ZENTAO_HTTP_PROXY"))
    # Manual token: pasted from the browser, skips password login (REST mode).
    zentao_token: str = field(default_factory=lambda: _env_str("ZENTAO_TOKEN"))
    # Override the login endpoint when it is not `/api.php/v1/tokens`.
    zentao_tokens_path: str = field(
        default_factory=lambda: _env_str("ZENTAO_TOKENS_PATH", "/api.php/v1/tokens")
    )
    # Auth channel:
    #   rest    -> POST /api.php/v1/tokens + `Token:` header (needs api.php reachable)
    #   session -> web session via /user-login.html (account/password)
    #   cookie  -> reuse a `zentaosid` copied from the browser, no password at all
    #   auto    -> cookie if ZENTAO_COOKIE set, else rest if api.php answers, else session
    zentao_auth_mode: str = field(
        default_factory=lambda: _env_str("ZENTAO_AUTH_MODE", "auto").lower()
    )
    # Session id copied from the browser (value of the `zentaosid` cookie),
    # optionally the whole `key=value; key2=value2` cookie header.
    zentao_cookie: str = field(default_factory=lambda: _env_str("ZENTAO_COOKIE"))

    # --- SVN ---
    svn_repo_url: str = field(default_factory=lambda: _env_str("SVN_REPO_URL"))
    svn_working_copy: Path = field(
        default_factory=lambda: Path(
            _env_str("SVN_WORKING_COPY", str(PROJECT_ROOT / "svn_workspace"))
        )
    )
    svn_trunk_path: str = field(default_factory=lambda: _env_str("SVN_TRUNK_PATH", "/trunk"))
    svn_branch_root: str = field(default_factory=lambda: _env_str("SVN_BRANCH_ROOT", "/branches"))
    svn_username: str = field(default_factory=lambda: _env_str("SVN_USERNAME"))
    svn_password: str = field(default_factory=lambda: _env_str("SVN_PASSWORD"))
    svn_bin: str = field(default_factory=lambda: _env_str("SVN_BIN", "svn"))
    # Safety switch: the AI must never touch trunk, this is only for manual use.
    svn_allow_trunk_write: bool = field(
        default_factory=lambda: _env_bool("SVN_ALLOW_TRUNK_WRITE", False)
    )

    # --- Attachments (Zentao screenshots / uploaded files) ---
    attachment_dir: Path = field(
        default_factory=lambda: Path(
            _env_str("ATTACHMENT_DIR", str(PROJECT_ROOT / "attachments"))
        )
    )
    download_attachments: bool = field(
        default_factory=lambda: _env_bool("DOWNLOAD_ATTACHMENTS", True)
    )

    # --- Branch naming ---
    branch_prefix: str = field(default_factory=lambda: _env_str("BRANCH_PREFIX", "bugfix/zentao-"))

    # --- Web ---
    host: str = field(default_factory=lambda: _env_str("WEB_HOST", "127.0.0.1"))
    port: int = field(default_factory=lambda: _env_int("WEB_PORT", 5000))
    debug: bool = field(default_factory=lambda: _env_bool("WEB_DEBUG", False))

    # --- AI executor ---
    batch_size: int = field(default_factory=lambda: _env_int("AI_BATCH_SIZE", 5))
    # Force the executor to write a structured analysis before commit/block can land.
    require_analysis: bool = field(default_factory=lambda: _env_bool("REQUIRE_ANALYSIS", True))

    # ------------------------------------------------------------------
    def require_zentao(self) -> None:
        """Raise when Zentao credentials are missing (called before syncing)."""
        missing = [
            name
            for name, value in (
                ("ZENTAO_BASE_URL", self.zentao_base_url),
                ("ZENTAO_ACCOUNT", self.zentao_account),
            )
            if not value
        ]
        if not self.zentao_password and not self.zentao_token:
            missing.append("ZENTAO_PASSWORD（或手工 ZENTAO_TOKEN）")
        if missing:
            raise RuntimeError(
                "禅道未配置：请在 " + str(PROJECT_ROOT / ".env") + " 中填写 "
                + ", ".join(missing)
            )

    @property
    def zentao_ready(self) -> bool:
        return bool(
            self.zentao_base_url
            and self.zentao_account
            and (self.zentao_password or self.zentao_token)
        )

    @property
    def svn_ready(self) -> bool:
        return bool(self.svn_repo_url)

    def describe(self) -> dict[str, str]:
        """Safe summary for display (secrets masked)."""
        return {
            "db_path": str(self.db_path),
            "zentao_base_url": self.zentao_base_url or "(未配置)",
            "zentao_account": self.zentao_account or "(未配置)",
            "zentao_password": "******" if self.zentao_password else "(未配置)",
            "zentao_product_ids": ", ".join(map(str, self.zentao_product_ids)) or "(自动发现)",
            "svn_repo_url": self.svn_repo_url or "(未配置)",
            "svn_working_copy": str(self.svn_working_copy),
            "branch_prefix": self.branch_prefix,
            "batch_size": str(self.batch_size),
            "require_analysis": "true" if self.require_analysis else "false",
        }


_settings: Settings | None = None


def get_settings() -> Settings:
    """Return the process-wide settings singleton."""
    global _settings
    if _settings is None:
        _settings = Settings()
    return _settings


def reload_settings() -> Settings:
    """Force re-reading .env (used by tests / after editing config)."""
    global _settings
    load_dotenv(PROJECT_ROOT / ".env", override=True)
    _settings = Settings()
    return _settings
