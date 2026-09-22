"""HTTP front end for the kernel: a small JSON API plus the single-page app.

  GET  /                      the app (web/index.html)
  GET  /api/ontology          the ontology definition
  GET  /api/state             objects with derived properties, KPIs, metadata
  GET  /api/edits             the full action log
  POST /api/actions/<name>    submit an action: {"params": {...}, "actor": {...}}

Standard library only. Binds to localhost by default. There is no
authentication and the actor's role is whatever the client says it is, so
the server takes two cheap precautions against other websites open in the
same browser. POSTs must be application/json, which forces a CORS preflight
this server never approves. And the Host header must be a loopback name,
which blocks DNS rebinding. Every error, including malformed requests and
unsupported methods, comes back as JSON.
"""

import argparse
import json
import pathlib
import sys
import traceback
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
from kernel import ActionError, Kernel, LogCorrupted  # noqa: E402
from views import fleet_view  # noqa: E402

ROOT = pathlib.Path(__file__).resolve().parents[1]
INDEX = ROOT / "web" / "index.html"
OBJECTS = ROOT / "state" / "objects.json"
ACTION_PREFIX = "/api/actions/"
MAX_BODY = 64 * 1024
LOOPBACK_NAMES = {"localhost", "127.0.0.1", "[::1]"}


def host_name(header):
    """'localhost:8765' -> 'localhost', '[::1]:8765' -> '[::1]'."""
    host = (header or "").strip().lower()
    if host.startswith("["):
        return host.split("]", 1)[0] + "]"
    return host.rsplit(":", 1)[0]


def make_handler(kernel, check_host=True):
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, fmt, *args):
            # A malformed request line gets here before command and path are set.
            command, path = getattr(self, "command", "") or "", getattr(self, "path", "") or ""
            sys.stderr.write(f"  {command} {path} -> {args[1] if len(args) > 1 else fmt % args}\n")

        def send_error(self, code, message=None, explain=None):
            # http.server answers bad requests and unsupported methods with an
            # HTML page. Keep every response JSON.
            try:
                phrase = HTTPStatus(code).phrase
            except ValueError:
                phrase = "Error"
            self.close_connection = True
            self._send(code, {"error": message or phrase})

        def _send(self, status, body, content_type="application/json"):
            data = body if isinstance(body, bytes) else json.dumps(body).encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(data)))
            self.send_header("Cache-Control", "no-store")
            self.send_header("X-Content-Type-Options", "nosniff")
            self.end_headers()
            if self.command != "HEAD":
                self.wfile.write(data)

        def _guarded(self, handler):
            if check_host and host_name(self.headers.get("Host")) not in LOOPBACK_NAMES:
                return self._send(403, {"error": "Host not allowed."})
            try:
                handler()
            except Exception:  # noqa: BLE001 - last line of defense: answer instead of dropping the socket
                traceback.print_exc()
                self._send(500, {"error": "Internal error. See the server log."})

        def do_GET(self):
            self._guarded(self._get)

        def do_POST(self):
            self._guarded(self._post)

        def _get(self):
            path = self.path.split("?", 1)[0]
            if path in ("/", "/index.html"):
                return self._send(200, INDEX.read_bytes(), "text/html; charset=utf-8")
            if path == "/api/ontology":
                return self._send(200, kernel.ontology)
            if path == "/api/state":
                return self._send(200, fleet_view(kernel.snapshot()))
            if path == "/api/edits":
                return self._send(200, kernel.edit_log())
            return self._send(404, {"error": "Not found."})

        def _post(self):
            path = self.path.split("?", 1)[0]
            if not path.startswith(ACTION_PREFIX):
                return self._send(404, {"error": "Not found."})

            content_type = (self.headers.get("Content-Type") or "").split(";", 1)[0].strip().lower()
            if content_type != "application/json":
                return self._send(415, {"error": "Content-Type must be application/json."})

            raw_length = self.headers.get("Content-Length")
            if raw_length is None:
                return self._send(411, {"error": "Content-Length is required."})
            try:
                length = int(raw_length)
            except ValueError:
                return self._send(400, {"error": "Content-Length must be a number."})
            if length < 0:
                return self._send(400, {"error": "Content-Length can't be negative."})
            if length > MAX_BODY:
                return self._send(413, {"error": "Request body is too large."})

            try:
                body = json.loads(self.rfile.read(length).decode("utf-8") or "{}")
            except ValueError:  # covers both JSONDecodeError and UnicodeDecodeError
                return self._send(400, {"error": "Request body must be UTF-8 JSON."})
            if not isinstance(body, dict):
                return self._send(400, {"error": "Request body must be a JSON object."})
            params = body.get("params")
            actor = body.get("actor")
            if params is not None and not isinstance(params, dict):
                return self._send(400, {"error": "params must be an object."})
            if not isinstance(actor, dict):
                return self._send(400, {"error": "actor must be an object."})

            try:
                edit = kernel.submit(path[len(ACTION_PREFIX):], params, actor)
            except ActionError as error:
                return self._send(422, {"error": error.message, "field": error.field})
            return self._send(200, {"edit": edit, "state": fleet_view(kernel.snapshot())})

    return Handler


def main():
    parser = argparse.ArgumentParser(description="Serve the Recall Ops app.")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8765)
    args = parser.parse_args()

    if not OBJECTS.exists():
        print("No data yet. Build it first: python3 src/pipeline.py", file=sys.stderr)
        return 1
    try:
        kernel = Kernel()
    except LogCorrupted as error:
        print(error, file=sys.stderr)
        return 1

    loopback = args.host in ("127.0.0.1", "localhost", "::1")
    server = ThreadingHTTPServer((args.host, args.port), make_handler(kernel, check_host=loopback))
    counts = {kind: len(rows) for kind, rows in kernel.objects.items()}
    print(f"Recall Ops on http://{args.host}:{args.port}")
    print(f"  {counts.get('Vehicle', 0)} vehicles, {counts.get('RecallCampaign', 0)} campaigns, "
          f"{counts.get('WorkOrder', 0)} work orders, {len(kernel.edits)} edits replayed")
    if not loopback:
        print("  Not bound to loopback: the Host check is off and anyone who can reach this port can act.")
    if kernel.load_warning:
        print(f"  Warning: {kernel.load_warning}")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    return 0


if __name__ == "__main__":
    sys.exit(main())
