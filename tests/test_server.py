import http.client
import json
import pathlib
import socket
import sys
import tempfile
import threading
import unittest
from http.server import ThreadingHTTPServer

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))

from kernel import Kernel  # noqa: E402
from server import make_handler  # noqa: E402
from test_kernel import LEAD, TODAY, fixture  # noqa: E402

JSON = {"Content-Type": "application/json"}


class ServerTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        base = pathlib.Path(self.tmp.name)
        (base / "objects.json").write_text(json.dumps(fixture()))
        kernel = Kernel(ROOT / "ontology" / "ontology.json", base / "objects.json", base / "edits.jsonl", today=TODAY)
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), make_handler(kernel))
        self.server.RequestHandlerClass.log_message = lambda *args: None
        threading.Thread(target=self.server.serve_forever, daemon=True).start()

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        self.tmp.cleanup()

    def request(self, method, path, body=b"", headers=None):
        connection = http.client.HTTPConnection("127.0.0.1", self.server.server_address[1], timeout=5)
        connection.request(method, path, body=body, headers=headers or {})
        response = connection.getresponse()
        data = response.read()
        connection.close()
        return response.status, json.loads(data) if data else None

    def test_a_valid_action_is_recorded(self):
        body = json.dumps({"params": {"vehicle": "VEH-001", "reason": "Open critical recall"}, "actor": LEAD})
        status, payload = self.request("POST", "/api/actions/groundVehicle", body.encode(), JSON)
        self.assertEqual(status, 200)
        self.assertEqual(payload["edit"]["id"], "E-0001")

    def test_malformed_requests_get_a_json_error_instead_of_a_dropped_connection(self):
        good_actor = json.dumps(LEAD)
        bodies = [
            b"[1, 2]", b'"hi"', b"5", b"null", b"not json", b"\xff\xff",
            b'{"params": [1], "actor": ' + good_actor.encode() + b"}",
            b'{"params": {}, "actor": "safety_lead"}',
            b'{"params": {"vehicle": ["VEH-001"], "reason": "Open critical recall"}, "actor": '
            + good_actor.encode() + b"}",
        ]
        for body in bodies:
            with self.subTest(body=body):
                status, payload = self.request("POST", "/api/actions/groundVehicle", body, JSON)
                self.assertIn(status, (400, 422))
                self.assertIn("error", payload)

    def test_bad_headers_are_rejected_up_front(self):
        cases = [
            ({"Content-Type": "text/plain"}, 415),
            ({**JSON, "Content-Length": "abc"}, 400),
            ({**JSON, "Content-Length": "-1"}, 400),
            ({**JSON, "Content-Length": str(10**7)}, 413),
            ({**JSON, "Host": "attacker.example"}, 403),
        ]
        for headers, expected in cases:
            with self.subTest(headers=headers):
                status, payload = self.request("POST", "/api/actions/groundVehicle", b"{}", headers)
                self.assertEqual(status, expected)
                self.assertIn("error", payload)

    def test_a_garbage_request_line_still_gets_an_answer(self):
        # A line with no HTTP version is treated as HTTP/0.9, which has no status
        # line, so the answer is the bare JSON body. It used to be nothing at all.
        with socket.create_connection(("127.0.0.1", self.server.server_address[1]), timeout=5) as sock:
            sock.sendall(b"GARBAGE\r\n\r\n")
            reply = sock.recv(4096)
        self.assertIn(b'"error"', reply)

    def test_unsupported_methods_answer_in_json(self):
        status, payload = self.request("PUT", "/api/actions/groundVehicle", b"{}", JSON)
        self.assertEqual(status, 501)
        self.assertIn("error", payload)


if __name__ == "__main__":
    unittest.main()
