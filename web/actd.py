"""Единственная кнопка панелей: «убрать из ленты» на странице «Контроль».

Крошечный сервис на 127.0.0.1:8077 за nginx (location = /act). Принимает
POST /act {"token": <hex32>, "rid": <int>} и закрывает запись журнала
rop_control резолюцией 'dismissed'. Право есть у токенов kind rop и admin.

Безопасность без параметризации: token обязан пройти ^[0-9a-f]{32}$,
rid приводится к int — в SQL попадают только проверенные значения.
В базу ходим через psql в контейнере postgres, зависимостей на хосте нет.

Systemd: ropbot-actd.service. Лог — journalctl -u ropbot-actd.
"""
import json
import re
import subprocess
from http.server import BaseHTTPRequestHandler, HTTPServer

PSQL = ["docker", "exec", "-i", "ropbot-postgres-1",
        "psql", "-U", "rop", "-d", "rop", "-tAq", "-c"]


def q(sql):
    r = subprocess.run(PSQL + [sql], capture_output=True, text=True, timeout=10)
    if r.returncode != 0:
        raise RuntimeError(r.stderr.strip()[:200])
    return r.stdout.strip()


class H(BaseHTTPRequestHandler):
    def _send(self, code, obj):
        body = json.dumps(obj).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def do_POST(self):
        if self.path != "/act":
            return self._send(404, {"ok": False})
        try:
            raw = self.rfile.read(min(int(self.headers.get("Content-Length", 0)), 4096))
            body = json.loads(raw)
            token = body["token"]
            rid = int(body["rid"])
            if not re.fullmatch(r"[0-9a-f]{32}", token):
                raise ValueError
        except Exception:
            return self._send(400, {"ok": False, "err": "bad request"})
        try:
            kind = q(f"SELECT kind FROM dash_tokens WHERE token = '{token}' AND active")
            if kind not in ("rop", "admin"):
                return self._send(403, {"ok": False, "err": "forbidden"})
            n = q(f"UPDATE rop_control SET resolved_at = now(), "
                  f"resolution = 'dismissed' WHERE id = {rid} "
                  f"AND resolved_at IS NULL RETURNING id")
            self._send(200, {"ok": bool(n)})
        except Exception as exc:
            self._send(500, {"ok": False, "err": str(exc)[:100]})

    def log_message(self, fmt, *args):  # без токенов в логе
        pass


if __name__ == "__main__":
    HTTPServer(("127.0.0.1", 8077), H).serve_forever()
