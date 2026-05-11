import json
import re
import sys
import tempfile
import unittest
from pathlib import Path

from fastapi.testclient import TestClient


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import api  # noqa: E402


DOCS = ROOT / "API.md"


def _fenced_block(name: str) -> str:
    pattern = rf"```{re.escape(name)}\n(.*?)\n```"
    match = re.search(pattern, DOCS.read_text(), flags=re.DOTALL)
    if not match:
        raise AssertionError(f"missing fenced block: {name}")
    return match.group(1).strip()


def _documented_endpoints() -> set[tuple[str, str]]:
    endpoints = set()
    for line in _fenced_block("api-endpoints").splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        method, path = line.split(maxsplit=1)
        endpoints.add((method.upper(), path))
    return endpoints


def _openapi_endpoints() -> set[tuple[str, str]]:
    http_methods = {"get", "post", "put", "patch", "delete"}
    schema = api.app.openapi()
    endpoints = set()
    for path, item in schema["paths"].items():
        for method in item:
            if method in http_methods:
                endpoints.add((method.upper(), path))
    return endpoints


def _resolve_ref(value: str, context: dict) -> object:
    if not value.startswith("$"):
        return value
    ref = value[1:]
    name, *parts = ref.split(".")
    current = context[name]
    for part in parts:
        current = current[part]
    return current


def _resolve(value, context: dict):
    if isinstance(value, str):
        if value.startswith("$") and value.count("$") == 1:
            return _resolve_ref(value, context)
        for match in re.findall(r"\$[A-Za-z_][A-Za-z0-9_]*(?:\.[A-Za-z_][A-Za-z0-9_]*)*", value):
            value = value.replace(match, str(_resolve_ref(match, context)))
        return value
    if isinstance(value, list):
        return [_resolve(item, context) for item in value]
    if isinstance(value, dict):
        return {key: _resolve(item, context) for key, item in value.items()}
    return value


class ApiDocsTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        api.configure_storage(Path(self.tmp.name))
        self.client = TestClient(api.app)

    def tearDown(self):
        api.configure_storage()
        self.tmp.cleanup()

    def test_documented_endpoint_inventory_matches_openapi(self):
        self.assertEqual(_documented_endpoints(), _openapi_endpoints())

    def test_documented_smoke_flow_executes(self):
        steps = json.loads(_fenced_block("api-smoke-test"))
        context = {}

        for step in steps:
            method = step["method"].upper()
            path = _resolve(step["path"], context)
            body = _resolve(step.get("body"), context)
            response = self.client.request(method, path, json=body)
            self.assertEqual(response.status_code, 200, (step["name"], response.text))

            data = response.json()
            for key, expected in step.get("expect", {}).items():
                self.assertEqual(data[key], expected, step["name"])

            if "name" in step:
                context[step["name"]] = data


if __name__ == "__main__":
    unittest.main()
