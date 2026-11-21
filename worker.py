"""Run iCore JSON service tasks fetched from the Operaton REST API."""

import base64
import json
import logging
import math
import os
import re
import signal
import sqlite3
import threading
import time
import uuid
import xml.etree.ElementTree as ET
from http.client import HTTPException
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.parse import quote, urlencode, urlsplit
from urllib.request import HTTPRedirectHandler, Request, build_opener

from catalog import Catalog, ConfigurationError, base_url

LOG = logging.getLogger("icore-worker")
BPMN = "{http://www.omg.org/spec/BPMN/20100524/MODEL}"
EXTENSIONS = ("{http://camunda.org/schema/1.0/bpmn}", "{http://operaton.org/schema/1.0/bpmn}")
READ_METHODS = {"GET", "HEAD", "OPTIONS"}
MAX_BODY = 10_000_000
MISSING = object()


class RemoteError(Exception):
    def __init__(self, status=None):
        self.status = status

    @property
    def transient(self):
        return self.status is None or self.status in {408, 429} or self.status >= 500


class NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def json_text(value):
    return json.dumps(value, allow_nan=False, ensure_ascii=False)


def http(url, method="GET", data=MISSING, headers=None, timeout=20, form=False):
    request_headers = dict(headers or {})
    request_headers["Accept"] = "application/json"
    payload = None
    if data is not MISSING:
        payload = (urlencode(data) if form else json_text(data)).encode()
        request_headers["Content-Type"] = "application/x-www-form-urlencoded" if form else "application/json"
    try:
        with build_opener(NoRedirect).open(
            Request(url, payload, request_headers, method=method), timeout=timeout
        ) as response:
            content = response.read(MAX_BODY + 1)
            status = response.status
    except HTTPError as exc:
        exc.close()
        raise RemoteError(exc.code) from None
    except (URLError, OSError, HTTPException):
        raise RemoteError() from None
    if len(content) > MAX_BODY:
        raise ConfigurationError("HTTP response exceeds the size limit")
    try:
        return status, json.loads(content) if content else None
    except (UnicodeError, ValueError):
        raise ConfigurationError("HTTP response is not JSON") from None


def bpmn_properties(element):
    properties = {}
    for namespace in EXTENSIONS:
        for item in element.findall(f"{BPMN}extensionElements/{namespace}properties/{namespace}property"):
            name = item.get("name", "")
            if name in {"icore-config", "icore-start-variables", "icore-process-outputs"}:
                if name in properties:
                    raise ConfigurationError("Duplicate iCore BPMN property")
                properties[name] = json.loads(item.get("value", ""))
    return properties


def task_config(xml, activity_id, topic):
    root = ET.fromstring(xml)
    for process in root.findall(f"{BPMN}process"):
        for task in process.iter(f"{BPMN}serviceTask"):
            if task.get("id") != activity_id:
                continue
            if not any(task.get(ns + "type") == "external" and task.get(ns + "topic") == topic
                       for ns in EXTENSIONS):
                raise ConfigurationError("BPMN task has the wrong implementation or topic")
            config = bpmn_properties(task).get("icore-config")
            if not isinstance(config, dict) or type(config.get("version")) is not int or config["version"] not in {1, 2}:
                raise ConfigurationError("Expected iCore mapping version 1 or 2")
            return config, bpmn_properties(process), root
    raise ConfigurationError("External service task is missing from the deployed BPMN")


def read_path(root, path):
    if not isinstance(path, str) or not path or any(not part for part in path.split(".")):
        raise ConfigurationError("Invalid source path")
    value = root
    for part in path.split("."):
        if isinstance(value, dict) and part in value:
            value = value[part]
        elif isinstance(value, list) and part.isascii() and part.isdigit() and len(part) <= 9 and int(part) < len(value):
            value = value[int(part)]
        else:
            return MISSING
    return value


def binding_value(source, variables):
    kind = source.get("kind")
    if kind == "constant":
        value = source["value"]
    elif kind == "variable":
        value = read_path(variables, source.get("path"))
        if value is MISSING:
            if "fallback" in source:
                return source["fallback"], True
            raise ConfigurationError("Mapped process value is missing")
    else:
        raise ConfigurationError("Expected a constant or a process variable")
    transform = source.get("transform")
    if not transform:
        return value, False
    if transform == "string":
        return (value if isinstance(value, str) else json_text(value)), False
    if transform == "boolean" and (type(value) is bool or value in ("true", "false")):
        return value is True or value == "true", False
    if transform == "number" and type(value) in {str, int, float}:
        number = float(value)
        if math.isfinite(number):
            return (int(number) if number.is_integer() else number), False
    raise ConfigurationError("Invalid mapping conversion")


def bindings(config, variables):
    parameters, trace = {}, []
    body = MISSING
    targets = []
    for entry in config.get("inputs", []):
        target = entry["target"]
        if (not isinstance(target, str) or not target
                or any(target == old or target.startswith(old + ".") or old.startswith(target + ".")
                       for old in targets)):
            raise ConfigurationError("Invalid or overlapping mapping targets")
        targets.append(target)
        source = entry["source"]
        value, fallback = binding_value(source, variables)
        location, _, name = target.partition(".")
        if target == "body":
            body = value
        elif location == "body" and name and all(name.split(".")):
            if body is MISSING:
                body = {}
            leaf = body
            *parents, key = name.split(".")
            for parent in parents:
                leaf = leaf.setdefault(parent, {})
            leaf[key] = value
        elif location in {"path", "query", "header"} and name:
            parameters[location, name] = value
        else:
            raise ConfigurationError("Unknown mapping target")
        trace.append({"target": target, "source": source.get("path", "constant"),
                      "value": None if location == "header" else value, "usedFallback": fallback})
    return parameters, body, trace


def encoded(value, force_json=False):
    if force_json or isinstance(value, (dict, list)):
        return {"type": "Json", "value": json_text(value)}
    if isinstance(value, int) and not isinstance(value, bool) and not -2**63 <= value < 2**63:
        raise ConfigurationError("Output integer exceeds Operaton Long range")
    variable_type = {type(None): "Null", str: "String", bool: "Boolean", int: "Long", float: "Double"}
    return {"type": variable_type[type(value)], "value": value}


def variable_name(value):
    if not isinstance(value, str) or not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", value):
        raise ConfigurationError("Invalid Operaton variable name")
    return value


class ICore:
    def __init__(self, origin, bearer="", username="", password="", login_format="form", login_origin=""):
        self.url = base_url(origin)
        if urlsplit(self.url).path:
            raise ConfigurationError("ICORE_BASE_URL must be an origin")
        if bool(username) != bool(password) or bool(bearer) == bool(username):
            raise ConfigurationError("Configure either an iCore bearer token or worker login credentials")
        parsed = urlsplit(self.url)
        if parsed.scheme != "https" and parsed.hostname not in {"localhost", "127.0.0.1", "::1"}:
            raise ConfigurationError("iCore authentication requires HTTPS outside loopback")
        if login_format not in {"form", "json"}:
            raise ConfigurationError("ICORE_LOGIN_FORMAT must be form or json")
        self.static = bearer
        self.username = username
        self.password = password
        self.form = login_format == "form"
        self.login_headers = {"Origin": login_origin, "Referer": login_origin.rstrip("/") + "/"} if login_origin else {}
        self.token = ""
        self.expires_at = 0.0
        self.retry_login_at = 0.0

    def authorization(self):
        if self.static:
            return "Bearer " + self.static
        now = time.monotonic()
        if now < self.expires_at:
            return "Bearer " + self.token
        if now < self.retry_login_at:
            raise ConfigurationError("iCore worker login is cooling down after a failure")
        self.retry_login_at = now + 30
        try:
            _, response = http(self.url + "/api/v2/icore/login", "POST",
                               {"username": self.username, "password": self.password},
                               headers=self.login_headers, timeout=10, form=self.form)
        except RemoteError as exc:
            raise ConfigurationError(f"iCore worker login failed (HTTP {exc.status or 'unavailable'})") from None
        if not isinstance(response, dict) or response.get("status", "OK") != "OK":
            raise ConfigurationError("iCore worker login did not complete")
        token = response.get("access_token")
        if not isinstance(token, str) or not token or any(character.isspace() for character in token):
            raise ConfigurationError("iCore worker login returned no valid access token")
        if str(response.get("token_type", "bearer")).lower() != "bearer":
            raise ConfigurationError("iCore worker login returned an unsupported token type")
        lifetime = float(response.get("expires_in", 300))
        parts = token.split(".")
        if len(parts) == 3:
            try:
                claims = json.loads(base64.urlsafe_b64decode(parts[1] + "=" * (-len(parts[1]) % 4)))
                if "exp" in claims:
                    lifetime = min(lifetime, float(claims["exp"]) - time.time())
            except (ValueError, TypeError):
                raise ConfigurationError("iCore returned an invalid JWT expiry") from None
        if not math.isfinite(lifetime) or lifetime <= 0:
            raise ConfigurationError("iCore worker login returned an expired token")
        self.token = token
        self.expires_at = time.monotonic() + lifetime - min(30, lifetime / 10)
        self.retry_login_at = 0
        return "Bearer " + token


class State:
    def __init__(self, path):
        self.db = sqlite3.connect(path)
        self.db.execute("CREATE TABLE IF NOT EXISTS results (task TEXT PRIMARY KEY, result TEXT)")
        self.db.commit()

    def get(self, task):
        row = self.db.execute("SELECT result FROM results WHERE task=?", (task,)).fetchone()
        return MISSING if row is None else json.loads(row[0]) if row[0] is not None else None

    def mark_dispatched(self, task):
        with self.db:
            self.db.execute("INSERT INTO results VALUES (?, NULL)", (task,))

    def save(self, task, result):
        with self.db:
            self.db.execute("INSERT OR REPLACE INTO results VALUES (?, ?)", (task, json_text(result)))

    def remove(self, task):
        with self.db:
            self.db.execute("DELETE FROM results WHERE task=?", (task,))


class Worker:
    def __init__(self, catalog, icore, engine_url, state, engine_headers=None, topic="icore-service"):
        self.catalog = catalog
        self.icore = icore
        self.engine_url = base_url(engine_url)
        self.state = state
        self.engine_headers = engine_headers or {}
        self.topic = topic
        self.worker_id = "icore-dto-" + uuid.uuid4().hex
        self.stop = threading.Event()

    def engine(self, path, data=MISSING, timeout=20):
        method = "GET" if data is MISSING else "POST"
        return http(self.engine_url + path, method, data, self.engine_headers, timeout)[1]

    def poll(self):
        return self.engine("/external-task/fetchAndLock", {
            "workerId": self.worker_id, "maxTasks": 1, "asyncResponseTimeout": 20000,
            "topics": [{"topicName": self.topic, "lockDuration": 180000, "deserializeValues": False}],
        }, timeout=30)

    def execute(self, task):
        definition = self.engine("/process-definition/" + quote(task["processDefinitionId"], safe="") + "/xml")
        config, contract, root = task_config(definition["bpmn20Xml"], task["activityId"], self.topic)
        kind = config.get("kind", "icore")
        variables = {}
        for name, wrapped in task.get("variables", {}).items():
            value = wrapped.get("value")
            if isinstance(value, str) and (wrapped.get("type", "").lower() == "json"
                    or (wrapped.get("type", "").lower() == "object"
                        and wrapped.get("valueInfo", {}).get("serializationDataFormat") == "application/json")):
                value = json.loads(value)
            variables[name] = value
        for item in contract.get("icore-start-variables", []):
            name = variable_name(item["name"])
            if name not in variables and "default" in item:
                variables[name] = item["default"]
            if name in variables:
                self.catalog.validate_parameter(variables[name], {**item.get("schema", {}), "type": item["type"]}, name)
            elif item.get("required", True):
                raise ConfigurationError("Required process input is missing")
        if kind == "process-outputs":
            result = {
                variable_name(output["name"]): binding_value(output["source"], variables)[0]
                for output in contract.get("icore-process-outputs", [])
            }
            return {"icore_process_result": encoded(result, force_json=True)}
        if kind not in {"icore", "api", "service"} or config.get("postprocess") or config.get("script"):
            raise ConfigurationError("This worker executes catalog API service tasks")
        if config.get("connection", "default") != "default":
            raise ConfigurationError("This worker provides the default iCore connection")
        result_name = variable_name(config["resultVariable"])
        names = {"icore_process_result", result_name}
        if result_name == "icore_process_result":
            raise ConfigurationError("Reserved result variable")
        for output in config.get("outputs", []):
            name = variable_name(output["target"])
            if name in names or (name in variables and not output.get("overwrite", False)):
                raise ConfigurationError("Output variable already exists or is reserved")
            if output["type"] not in {"string", "integer", "number", "boolean", "object", "array", "unknown"}:
                raise ConfigurationError("Unknown output variable type")
            read_path({}, output["source"])
            if output["source"].split(".")[0] != "result":
                raise ConfigurationError("Output source must use the result envelope")
            names.add(name)

        policy = self.policy(config, root, task["activityId"])
        parameters, body, trace = bindings(config, variables)
        method, route, headers = self.catalog.build(config["endpoint"], parameters, body, body is not MISSING)
        task_id = task["id"]
        result = self.state.get(task_id)
        if result is None:
            raise ConfigurationError("Previous iCore write outcome is uncertain. Reconcile it before replay.")
        if result is MISSING:
            try:
                result = self.call_service(task_id, method, route, body, headers, policy["timeout"], trace)
            except RemoteError as exc:
                if exc.status in policy["business_errors"]:
                    return {"$bpmnError": policy["business_errors"][exc.status]}
                if method in READ_METHODS and exc.transient:
                    prior = task.get("retries")
                    retries = policy["retries"] if prior is None else max(0, prior - 1)
                    raise TaskFailure("iCore read failed temporarily", retries, policy["delay"] * 1000) from None
                raise ConfigurationError(f"iCore request failed (HTTP {exc.status or 'unavailable'})") from None

        outputs = {result_name: encoded(result, force_json=True)}
        for output in config.get("outputs", []):
            value = read_path({"result": result}, output["source"])
            if value is MISSING:
                raise ConfigurationError("Mapped response field is missing")
            if output["type"] != "unknown" and value is not None:
                self.catalog.validate_parameter(value, {"type": output["type"]}, output["target"])
            outputs[output["target"]] = encoded(value)
        return outputs

    def policy(self, config, root, activity_id):
        raw = config.get("policy") or {}
        policy = {"timeout": raw.get("timeout", 20), "retries": raw.get("retries", 2),
                  "delay": raw.get("retryDelay", 10), "business_errors": {}}
        for key, minimum, maximum in (("timeout", 1, 120), ("retries", 0, 5), ("delay", 1, 120)):
            if type(policy[key]) is not int or not minimum <= policy[key] <= maximum:
                raise ConfigurationError("Invalid task timeout or retry policy")
        codes = {item.get("id"): item.get("errorCode") for item in root.findall(f"{BPMN}error")}
        definitions = [item for event in root.iter(f"{BPMN}boundaryEvent") if event.get("attachedToRef") == activity_id
                       for item in event.findall(f"{BPMN}errorEventDefinition")]
        for item in raw.get("businessErrors", []):
            status, code = item["status"], item["code"]
            if (type(status) is not int or not 400 <= status <= 499 or status in {401, 403, 408, 429}
                    or status in policy["business_errors"] or not isinstance(code, str) or not code
                    or not any(not d.get("errorRef") or codes.get(d.get("errorRef")) == code for d in definitions)):
                raise ConfigurationError("Invalid HTTP business error or missing BPMN boundary event")
            policy["business_errors"][status] = code
        return policy

    def call_service(self, task_id, method, route, body, headers, timeout, trace):
        headers["Authorization"] = self.icore.authorization()
        headers["Idempotency-Key"] = task_id
        write = method not in READ_METHODS
        if write:
            self.state.mark_dispatched(task_id)
        try:
            status, response = http(self.icore.url + route, method, body, headers, timeout)
        except RemoteError as exc:
            if exc.status == 401:
                self.icore.expires_at = 0
            if exc.status == 401 and not write and not self.icore.static:
                headers["Authorization"] = self.icore.authorization()
                status, response = http(self.icore.url + route, method, body, headers, min(10, timeout))
            else:
                if write and exc.status in {400, 401, 403, 404, 405, 415, 422, 429}:
                    self.state.remove(task_id)
                raise
        result = {"response": response, "status": status, "trace": trace}
        self.state.save(task_id, result)
        return result

    def handle(self, task):
        task_id = task["id"]
        path = "/external-task/" + quote(task_id, safe="")
        payload = {"workerId": self.worker_id}
        try:
            variables = self.execute(task)
        except TaskFailure as exc:
            self.report_failure(path, payload, str(exc), exc.retries, exc.delay)
            return
        except ConfigurationError as exc:
            self.report_failure(path, payload, str(exc))
            return
        except (KeyError, TypeError, AttributeError, ValueError, ET.ParseError):
            self.report_failure(path, payload, "Invalid BPMN configuration, catalog data or process variable")
            return
        except RemoteError:
            LOG.warning("Unable to read deployed BPMN for task %s. Lock will expire.", task_id)
            return

        if "$bpmnError" in variables:
            self.engine(path + "/bpmnError", {**payload, "errorCode": variables["$bpmnError"]})
        else:
            self.engine(path + "/complete", {**payload, "variables": variables})
            self.state.remove(task_id)
        LOG.info("Finished external task %s", task_id)

    def report_failure(self, path, payload, message, retries=0, delay=0):
        self.engine(path + "/failure", {**payload, "errorMessage": message[:666],
                                       "retries": retries, "retryTimeout": delay})
        LOG.warning("Reported external task failure, retries=%s", retries)

    def run(self):
        LOG.info("Worker ready, topic=%s catalog=%s", self.topic, self.catalog.hash[:12])
        while not self.stop.is_set():
            try:
                for task in self.poll():
                    self.handle(task)
            except (RemoteError, ConfigurationError, sqlite3.Error) as exc:
                LOG.warning("Worker poll or task acknowledgement failed (%s)", type(exc).__name__)
                self.stop.wait(5)


class TaskFailure(Exception):
    def __init__(self, message, retries, delay):
        super().__init__(message)
        self.retries, self.delay = retries, delay


def main():
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    settings = os.environ
    password = settings.get("ICORE_PASSWORD", "")
    if settings.get("ICORE_PASSWORD_FILE"):
        if password:
            raise ConfigurationError("Set one iCore password source")
        password = Path(settings["ICORE_PASSWORD_FILE"]).read_text().rstrip("\r\n")
    icore = ICore(settings.get("ICORE_BASE_URL", ""), settings.get("ICORE_BEARER_TOKEN", ""),
                  settings.get("ICORE_USERNAME", ""), password, settings.get("ICORE_LOGIN_FORMAT", "form"),
                  settings.get("ICORE_LOGIN_ORIGIN", ""))
    engine_url = base_url(settings.get("OPERATON_ENGINE_URL", ""))
    user, secret = settings.get("OPERATON_AUTH_USER", ""), settings.get("OPERATON_AUTH_PASSWORD", "")
    if bool(user) != bool(secret):
        raise ConfigurationError("Provide both Operaton basic authentication settings")
    auth = base64.b64encode(f"{user}:{secret}".encode()).decode() if user else ""
    headers = {"Authorization": "Basic " + auth} if auth else {}
    catalog = Catalog(settings.get("ICORE_CATALOG", "catalog.yaml"))
    state = State(settings.get("WORKER_STATE", "worker-state.sqlite3"))
    worker = Worker(catalog, icore, engine_url, state, headers, settings.get("WORKER_TOPIC", "icore-service"))
    for signum in (signal.SIGTERM, signal.SIGINT):
        signal.signal(signum, lambda *_: worker.stop.set())
    try:
        worker.run()
    finally:
        state.db.close()


if __name__ == "__main__":
    try:
        main()
    except (ConfigurationError, OSError, sqlite3.Error, ValueError, KeyError) as exc:
        if isinstance(exc, ConfigurationError):
            raise SystemExit(str(exc)) from None
        raise SystemExit(f"Worker configuration failed ({type(exc).__name__})") from None
