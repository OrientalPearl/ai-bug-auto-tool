"""One-click launcher: init the SQLite file, serve the web console, open browser.

    python run_web.py             # start + open browser
    python run_web.py --no-browser
    python run_web.py --sync      # additionally pull assigned bugs from Zentao first
    python run_web.py --port 8080
"""

from __future__ import annotations

import argparse
import socket
import sys
import threading
import webbrowser

from app import db, sync
from app.config import get_settings
from app.web import app


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="禅道 Bug / 任务 自动修复闭环系统 Web 控制台")
    parser.add_argument("--host", default=None, help="覆盖 WEB_HOST")
    parser.add_argument("--port", type=int, default=None, help="覆盖 WEB_PORT")
    parser.add_argument("--no-browser", action="store_true", help="启动后不自动打开浏览器")
    parser.add_argument("--sync", action="store_true", help="启动前先从禅道同步一次 bug")
    return parser.parse_args(argv)


def port_in_use(host: str, port: int) -> bool:
    """Detect an already running instance so a double click fails with a hint."""
    probe = "127.0.0.1" if host in {"0.0.0.0", ""} else host
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.settimeout(0.5)
        return sock.connect_ex((probe, port)) == 0


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    settings = get_settings()
    host = args.host or settings.host
    port = args.port or settings.port
    url = f"http://{host}:{port}"

    db.init_db()
    print("=" * 62)
    print(" 禅道 Bug / 任务 自动修复闭环系统")
    print(f"   数据库 : {db.db_path()}")
    print(f"   访问地址: {url}")
    print(f"   禅道   : {settings.zentao_base_url or '未配置（请编辑 .env）'}")
    print(f"   SVN    : {settings.svn_repo_url or '未配置（默认不代跑 svn，仅可选方式需要）'}")
    print("=" * 62)

    if args.sync:
        try:
            result = sync.sync_now()
            print(f"[sync] 新增 {result['created']} / 更新 {result['updated']} / 共 {result['total']}")
            for err in result["errors"]:
                print(f"[sync][warn] {err}")
        except Exception as exc:  # launcher must still start the web console
            print(f"[sync][warn] 同步失败，页面仍可用：{exc}")

    if port_in_use(host, port):
        print(f"[warn] 端口 {port} 已被占用，可能系统已经在运行：直接访问 {url}")
        if not args.no_browser:
            webbrowser.open(url)
        return 0

    if not args.no_browser:
        threading.Timer(1.2, webbrowser.open, [url]).start()

    try:
        app.run(host=host, port=port, debug=settings.debug, use_reloader=False)
    except KeyboardInterrupt:
        print("\n[bye] 已停止 Web 控制台")
    return 0


if __name__ == "__main__":
    sys.exit(main())
