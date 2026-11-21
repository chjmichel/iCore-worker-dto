"""Resolve iCore operations and serialize requests from a pinned OpenAPI file."""

import hashlib
import re
from pathlib import Path
from urllib.parse import quote, urlencode, urlsplit

import yaml

METHODS = {"get", "put", "post", "delete", "patch", "head", "options"}
RESERVED_HEADERS = {
    "authorization", "proxy-authorization", "host", "cookie", "content-length",
    "content-type", "transfer-encoding", "connection", "idempotency-key",
}


class ConfigurationError(ValueError):
    pass


def base_url(value):
    if not value or any(character.isspace() for character in value):
        raise ConfigurationError("Configure an HTTP(S) service URL")
    parts = urlsplit(value)
    if (parts.scheme not in {"http", "https"} or not parts.hostname
            or parts.username is not None or parts.query or parts.fragment):
        raise ConfigurationError("Configure an HTTP(S) service URL without credentials, query or fragment")
    return value.rstrip("/")


def scalar(value):
    if isinstance(value, bool):
        return str(value).lower()
    if isinstance(value, (str, int, float)):
        return str(value)
    raise ConfigurationError("Expected a scalar HTTP parameter")


class Catalog:
    def __init__(self, filename):
        source = Path(filename).read_text(encoding="utf-8-sig")
        self.hash = hashlib.sha256(source.encode()).hexdigest()
        self.document = yaml.safe_load(source)
        if not isinstance(self.document, dict) or not str(self.document.get("openapi", "")).startswith("3.0."):
            raise ConfigurationError("Expected an OpenAPI 3.0 catalog")
        self.operations = {}
        for path, raw_item in self.document["paths"].items():
            item = self.resolve(raw_item)
            for method, operation in item.items():
                if method in METHODS:
                    self.operations[method.upper(), path] = (item, operation)

    def resolve(self, item):
        seen = set()
        while "$ref" in item:
            ref = item["$ref"]
            if not ref.startswith("#/") or ref in seen:
                raise ConfigurationError("Expected a bundled, non-cyclic OpenAPI reference")
            seen.add(ref)
            item = self.document
            for segment in ref[2:].split("/"):
                item = item[segment.replace("~1", "/").replace("~0", "~")]
        return item

    def parameters(self, item, operation):
        parameters = {}
        for raw in item.get("parameters", []) + operation.get("parameters", []):
            parameter = self.resolve(raw)
            parameters[parameter["in"], parameter["name"]] = parameter
        return parameters

    def request_body(self, operation):
        return self.resolve(operation.get("requestBody", {}))

    def exclusion(self, path, item, operation):
        if path.rstrip("/").rsplit("/", 1)[-1].lower() in {"login", "loginwithcode", "logout"}:
            return "Reserved for worker authentication"
        body = self.request_body(operation)
        if body and "application/json" not in body.get("content", {}):
            return "Request body is not JSON"
        for (location, name), parameter in self.parameters(item, operation).items():
            if location not in {"query", "path", "header"}:
                return "Unsupported parameter location"
            if location == "header" and name.lower() in RESERVED_HEADERS:
                return "Reserved HTTP header"
            style = "form" if location == "query" else "simple"
            schema = self.resolve(parameter.get("schema", {}))
            if ("content" in parameter or parameter.get("style", style) != style
                    or schema.get("type") == "object" or parameter.get("allowReserved")):
                return "Unsupported HTTP parameter encoding"
        return None

    def endpoint(self, operation_id):
        matches = [
            (method, path, item, operation)
            for (method, path), (item, operation) in self.operations.items()
            if operation.get("operationId") == operation_id
        ]
        if len(matches) != 1:
            raise ConfigurationError("Expected a unique operationId from the catalog")
        method, path, item, operation = matches[0]
        reason = self.exclusion(path, item, operation)
        if reason:
            raise ConfigurationError(reason)
        return {"method": method, "path": path, "operationId": operation_id, "catalogHash": self.hash}

    def build(self, endpoint, values, body, has_body):
        method, path = endpoint.get("method"), endpoint.get("path")
        if endpoint.get("catalogHash") != self.hash:
            raise ConfigurationError("Catalog hash mismatch")
        if (method, path) not in self.operations:
            raise ConfigurationError("Operation not found in catalog")
        item, operation = self.operations[method, path]
        if not operation.get("operationId") or endpoint.get("operationId") != operation["operationId"]:
            raise ConfigurationError("Operation ID does not match catalog")
        reason = self.exclusion(path, item, operation)
        if reason:
            raise ConfigurationError(reason)
        if (not path.startswith("/") or path.startswith("//") or any(c in path for c in "\\?#%")
                or any(part in {".", ".."} for part in path.split("/"))):
            raise ConfigurationError("Invalid catalog path")

        parameters = self.parameters(item, operation)
        if values.keys() - parameters.keys():
            raise ConfigurationError("Mapped HTTP parameter is absent from the catalog")
        query, headers, segments = [], {}, {}
        for (location, name), parameter in parameters.items():
            if (location, name) not in values:
                if parameter.get("required") or location == "path":
                    raise ConfigurationError(f"Missing parameter {location}.{name}")
                continue
            value = values[location, name]
            schema = self.resolve(parameter.get("schema", {}))
            self.validate_parameter(value, schema, f"{location}.{name}")
            if value is None:
                continue
            entries = [scalar(entry) for entry in value] if isinstance(value, list) else [scalar(value)]
            if location == "query":
                if isinstance(value, list) and parameter.get("explode", True):
                    query.extend((name, entry) for entry in entries)
                else:
                    query.append((name, ",".join(entries)))
            elif location == "path":
                if any(entry in {"", ".", ".."} or "/" in entry or "\\" in entry or "%" in entry
                       for entry in entries):
                    raise ConfigurationError("Invalid path parameter")
                segments[name] = ",".join(quote(entry, safe="") for entry in entries)
            else:
                field = ",".join(entries)
                if not re.fullmatch(r"[!#$%&'*+.^_`|~0-9A-Za-z-]+", name) or "\r" in field or "\n" in field:
                    raise ConfigurationError("Invalid HTTP header")
                headers[name] = field
        try:
            route = re.sub(r"\{([^{}]+)\}", lambda match: segments[match[1]], path)
        except KeyError:
            raise ConfigurationError("Missing catalog path parameter") from None
        if "{" in route or "}" in route:
            raise ConfigurationError("Invalid catalog path template")

        schema = self.request_body(operation)
        if has_body:
            if not schema:
                raise ConfigurationError("Catalog operation has no request body")
            self.validate_parameter(body, schema["content"]["application/json"].get("schema", {}), "body")
        elif schema.get("required"):
            raise ConfigurationError("Missing required JSON request body")
        return method, route + ("?" + urlencode(query) if query else ""), headers

    def validate_parameter(self, value, raw_schema, name):
        schema = self.resolve(raw_schema)
        expected = schema.get("type")
        types = {"integer": (int,), "number": (int, float), "boolean": (bool,),
                 "string": (str,), "array": (list,), "object": (dict,)}
        if value is None and schema.get("nullable"):
            return
        if expected in types and type(value) not in types[expected]:
            raise ConfigurationError(f"{name} must be {expected}")
        if "enum" in schema and value not in schema["enum"]:
            raise ConfigurationError(f"{name} has a value outside its enum")
        if isinstance(value, list) and "items" in schema:
            for entry in value:
                self.validate_parameter(entry, schema["items"], name)
        if isinstance(value, dict):
            properties = schema.get("properties", {})
            missing = {
                key for key in schema.get("required", [])
                if not self.resolve(properties.get(key, {})).get("readOnly")
            } - value.keys()
            if missing:
                raise ConfigurationError(f"{name} is missing required fields")
            for key in value.keys() & properties.keys():
                child = self.resolve(properties[key])
                if not child.get("readOnly"):
                    self.validate_parameter(value[key], child, name + "." + key)
        for part in schema.get("allOf", []):
            self.validate_parameter(value, part, name)
