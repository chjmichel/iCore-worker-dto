import json
import os
import tempfile
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs

from catalog import Catalog, ConfigurationError
from generate import build_model
from worker import ICore, RemoteError, State, Worker, task_config

FIXTURE = """
openapi: 3.0.1
info: {title: iCore test API, version: '1'}
paths:
  /api/v2/icore/records/{id}:
    parameters:
      - {name: id, in: path, required: true, schema: {type: integer}}
    get:
      operationId: readRecord
      parameters:
        - name: includes
          in: query
          schema: {type: array, items: {type: string}}
      responses: {'200': {description: OK}}
  /api/v2/icore/records:
    post:
      operationId: createRecord
      requestBody:
        required: true
        content:
          application/json:
            schema: {$ref: '#/components/schemas/RecordDTO'}
      responses: {'201': {description: Created}}
components:
  schemas:
    RecordDTO:
      type: object
      required: [label]
      properties:
        label: {type: string}
"""


class WorkerIntegrationTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        spec = Path(self.directory.name) / "api.yml"
        spec.write_text(FIXTURE)
        self.catalog = Catalog(spec)
        self.init_server()
        self.state = State(Path(self.directory.name) / "state.sqlite")
        self.addCleanup(self.state.db.close)
        self.worker = Worker(self.catalog, ICore(self.url, username="worker", password="secret"),
                             self.url + "/engine-rest", self.state)
        self.task = {
            "id": "task-1", "activityId": "ICoreTask", "processDefinitionId": "definition:1:17",
            "variables": {"requestBody": {"type": "Json", "value": '{"label":"test"}'}},
        }

    def init_server(self):
        owner = self
        self.calls = []
        self.complete_error = False
        self.icore_status = 201

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *_):
                pass

            def respond(self, code, body=None):
                self.send_response(code)
                self.send_header("Content-Type", "application/json")
                self.end_headers()
                if body is not None:
                    self.wfile.write(json.dumps(body).encode())

            def do_GET(self):
                owner.calls.append((self.path, self.headers.get("Authorization")))
                if self.path.endswith("/xml"):
                    self.respond(200, {"bpmn20Xml": owner.xml.decode()})
                else:
                    self.respond(200, {"id": 7})

            def do_POST(self):
                raw = self.rfile.read(int(self.headers.get("Content-Length", "0")))
                if self.path.endswith("/login"):
                    owner.calls.append(("login", parse_qs(raw.decode())))
                    self.respond(200, {"access_token": "worker-token", "expires_in": 120})
                elif self.path.endswith("/fetchAndLock"):
                    owner.calls.append(("poll", json.loads(raw)))
                    self.respond(200, [owner.task])
                elif self.path.startswith("/engine-rest"):
                    owner.calls.append((self.path.rsplit("/", 1)[-1], json.loads(raw)))
                    self.respond(503 if self.path.endswith("/complete") and owner.complete_error else 204)
                else:
                    owner.calls.append(("icore", json.loads(raw), self.headers["Authorization"],
                                        self.headers["Idempotency-Key"]))
                    self.respond(owner.icore_status, {"id": 7})

        server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.url = f"http://127.0.0.1:{server.server_port}"
        self.addCleanup(server.server_close)
        self.addCleanup(server.shutdown)
        threading.Thread(target=server.serve_forever, daemon=True).start()

    def model(self, operation_id):
        self.xml, _ = build_model(self.catalog, operation_id, "icore_test", "icore-service")

    def test_fetch_login_write_and_recover_failed_completion(self):
        self.model("createRecord")
        (task,) = self.worker.poll()
        self.assertEqual(self.calls[0][1]["topics"][0]["topicName"], "icore-service")
        self.complete_error = True
        with self.assertRaises(RemoteError):
            self.worker.handle(task)
        self.complete_error = False
        self.worker.handle(task)
        writes = [call for call in self.calls if call[0] == "icore"]
        self.assertEqual(writes, [("icore", {"label": "test"}, "Bearer worker-token", "task-1")])
        self.assertIn(("login", {"username": ["worker"], "password": ["secret"]}), self.calls)
        result = json.loads(self.calls[-1][1]["variables"]["icore_result"]["value"])
        self.assertEqual((result["status"], result["response"]), (201, {"id": 7}))

    def test_write_with_uncertain_outcome_is_not_replayed(self):
        self.model("createRecord")
        self.icore_status = 503
        self.worker.handle(self.task)
        self.worker.handle(self.task)
        self.assertEqual(sum(call[0] == "icore" for call in self.calls), 1)
        self.assertEqual(self.calls[-1][0], "failure")
        self.assertIn("uncertain", self.calls[-1][1]["errorMessage"])

    def test_get_and_yaml_type_check(self):
        self.model("readRecord")
        self.task["variables"] = {"id": {"type": "Long", "value": 7}}
        self.worker.handle(self.task)
        self.assertIn(("/api/v2/icore/records/7", "Bearer worker-token"), self.calls)
        self.assertEqual(self.calls[-1][0], "complete")
        endpoint = self.catalog.endpoint("readRecord")
        method, path, _ = self.catalog.build(endpoint, {("path", "id"): 7,
                                    ("query", "includes"): ["name", "group id"]}, None, False)
        self.assertEqual((method, path), ("GET", "/api/v2/icore/records/7?includes=name&includes=group+id"))
        with self.assertRaises(ConfigurationError):
            self.catalog.build(endpoint, {("path", "id"): "7"}, None, False)

    def test_catalog_hash_mismatch_reports_incident_before_login(self):
        self.model("createRecord")
        self.xml = self.xml.replace(self.catalog.hash.encode(), b"wrong-catalog")
        self.worker.handle(self.task)
        self.assertEqual([call[0] for call in self.calls if not call[0].endswith("/xml")], ["failure"])
        self.assertEqual(self.calls[-1][1]["retries"], 0)

    @unittest.skipUnless(os.getenv("TEST_ICORE_CATALOG"), "private Swagger fixture is optional")
    def test_real_catalog_product_dto(self):
        self.catalog = self.worker.catalog = Catalog(os.environ["TEST_ICORE_CATALOG"])
        self.model("detail_33")
        config, _, _ = task_config(self.xml, "ICoreTask", "icore-service")
        self.assertEqual(config["endpoint"]["path"], "/api/v2/icore/products/{id}")
        self.task["variables"] = {"id": {"type": "Long", "value": 7}}
        self.worker.handle(self.task)
        self.assertIn(("/api/v2/icore/products/7", "Bearer worker-token"), self.calls)
        self.assertEqual(self.calls[-1][0], "complete")
