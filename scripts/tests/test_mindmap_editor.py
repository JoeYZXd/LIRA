"""Run: python -m unittest discover -s scripts/tests -p test_mindmap_editor.py"""
import importlib.util
import json
from pathlib import Path
import tempfile
import threading
import unittest
from urllib.error import HTTPError
from urllib.request import Request, urlopen

spec = importlib.util.spec_from_file_location("mindmap_editor", Path(__file__).parents[1] / "mindmap_editor.py")
bridge = importlib.util.module_from_spec(spec)
spec.loader.exec_module(bridge)


class LocalRequestsTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.path = Path(self.temp.name) / "map.html"
        self.path.write_text('<details class="node" id="E4"></details><script id="mindmap-bridge" type="application/json">'
                             + json.dumps({"base": "http://127.0.0.1:8768", "token": "test-token"}) + '</script>', encoding="utf-8")
        self.server = bridge.make_server(self.path, port=0)
        self.worker = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.worker.start()
        self.base = f"http://127.0.0.1:{self.server.server_port}"

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        self.worker.join()
        self.temp.cleanup()

    def request(self, body=None, token="test-token", origin="null"):
        headers = {"X-Mindmap-Token": token, "Origin": origin}
        data = None if body is None else json.dumps(body).encode("utf-8")
        req = Request(self.base + "/api/requests", data=data, headers=headers)
        try:
            with urlopen(req) as response:
                return response.status, json.load(response)
        except HTTPError as error:
            return error.code, json.load(error)

    def item(self, kind="modify", **extra):
        return {"id": "test-request-00001", "node": "E4", "kind": kind, "text": "进度条改为暖绿色 <script>alert(1)</script>", **extra}

    def test_three_modes_preserve_raw_input_and_do_not_change_html(self):
        original = self.path.read_bytes()
        for index, kind in enumerate(("modify", "discuss", "idea")):
            status, result = self.request(self.item(kind, id=f"test-request-0000{index}"))
            self.assertEqual(status, 200)
            self.assertEqual(result["request"]["text"], self.item()["text"])
            self.assertTrue(result["request"]["status"].startswith("pending_"))
        self.assertEqual(len(self.request()[1]["requests"]), 3)
        self.assertEqual(self.path.read_bytes(), original)

    def test_retry_is_idempotent_but_changed_payload_conflicts(self):
        self.assertEqual(self.request(self.item())[0], 200)
        self.assertEqual(self.request(self.item())[0], 200)
        self.assertEqual(len(self.request()[1]["requests"]), 1)
        self.assertEqual(self.request(self.item(text="different"))[0], 409)

    def test_auth_origin_and_bad_requests_do_not_create_file(self):
        for code, kwargs in ((403, {"token": "bad"}), (403, {"origin": "https://example.org"})):
            self.assertEqual(self.request(self.item(), **kwargs)[0], code)
        for payload in (self.item(node="../x"), self.item(kind="execute"), self.item(text=" "), self.item(text="x" * 12001)):
            self.assertEqual(self.request(payload)[0], 400)
        self.assertFalse(self.server.queue_path.exists())

    def test_corrupt_queue_is_preserved(self):
        self.server.queue_path.write_text("not json", encoding="utf-8")
        self.assertEqual(self.request(self.item())[0], 400)
        self.assertEqual(self.server.queue_path.read_text(), "not json")

    def test_new_nodes_are_available_without_restarting_service(self):
        with self.path.open("a", encoding="utf-8") as document:
            document.write('<details class="node" id="J9"></details>')
        self.assertEqual(self.request(self.item(node="J9"))[0], 200)

    def test_only_matching_markdown_sibling_is_served(self):
        markdown = self.path.with_suffix(".md")
        markdown.write_text("# Complete plan\n\n## U1. First unit\n", encoding="utf-8")
        with urlopen(self.base + "/map.md") as response:
            self.assertEqual(response.read(), markdown.read_bytes())
            self.assertIn("text/plain", response.headers["Content-Type"])
        with self.assertRaises(HTTPError) as failure:
            urlopen(self.base + "/other.md")
        self.assertEqual(failure.exception.code, 404)


if __name__ == "__main__":
    unittest.main()
