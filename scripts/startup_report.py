"""Serve the start-up log as a web page when the app couldn't start.

Used by scripts/replit-start.sh outside production only. Replit's publish
step waits for the app to answer on its port; when the start failed, this
answers instead, so the published address shows what went wrong rather
than "did not become ready in time". Standard library only: it must work
even when the app's packages are missing.

    python3 scripts/startup_report.py LOG_FILE PORT [PORT ...]
"""

from __future__ import annotations

import html
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer


def main() -> None:
    log_file, ports = sys.argv[1], sorted({int(p) for p in sys.argv[2:]})

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self) -> None:  # noqa: N802 (http.server naming)
            try:
                log = open(log_file, encoding="utf-8", errors="replace").read()
            except OSError as exc:
                log = f"(could not read the start-up log: {exc})"
            body = (
                "<!doctype html><meta charset=utf-8><meta name=viewport content='width=device-width'>"
                "<title>App did not start</title>"
                "<body style='font:15px system-ui;margin:24px;max-width:960px'>"
                "<h1 style='font-size:20px'>The app did not start</h1>"
                "<p>This page replaces the app because a start-up step failed. The last "
                "<code>[start]</code> line says which. Fix that, then republish. "
                "(Shown outside production only.)</p>"
                f"<pre style='white-space:pre-wrap;background:#f3f3f6;padding:12px;border-radius:8px'>{html.escape(log[-20000:])}</pre>"
            ).encode()
            # 200 so Replit marks the deployment ready and this page is reachable.
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *args) -> None:
            pass

    servers = [ThreadingHTTPServer(("0.0.0.0", p), Handler) for p in ports]
    for s in servers[1:]:
        threading.Thread(target=s.serve_forever, daemon=True).start()
    print(f"[start] serving the start-up report on port(s) {', '.join(map(str, ports))}", flush=True)
    servers[0].serve_forever()


if __name__ == "__main__":
    main()
