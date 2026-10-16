"""`workitem_relation` against a Community Edition shaped server.

CE serves built-in dependencies on `relations/` and has none of the optional custom
relation endpoints. The real tool runs against the real SDK; only the HTTP server is
faked, so no network or credentials are involved.
"""

from __future__ import annotations

import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest
from plane import PlaneClient
from plane.errors.errors import HttpError

from plane_mcp.tools import workitem_relation

PROJECT = "11111111-1111-1111-1111-111111111111"
ITEM_A = "aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa"
ITEM_B = "bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbb"
GROUPS = ("blocking", "blocked_by", "start_before", "start_after", "finish_before", "finish_after")
ITEM_BASE = f"/api/v1/workspaces/acme/projects/{PROJECT}/work-items/"


class FakeCE:
    def __init__(self) -> None:
        self.rows: list[tuple[str, str]] = []  # (blocked item, blocker), as CE stores them
        self.requests: list[tuple[str, str]] = []
        self.forced: dict[str, int] = {}  # route -> status for every method

    def handle(self, method: str, path: str, body):
        route = path.removeprefix("/api/v1/workspaces/acme/").strip("/")
        self.requests.append((method, route))
        if route.rsplit("/", 1)[-1] in self.forced:
            return self.forced[route.rsplit("/", 1)[-1]], {"detail": "forced"}
        if path.startswith(ITEM_BASE) and route.endswith("/relations"):
            item = route.split("/")[-2]
            return self._relations(method, item, body)
        return 404, {"error": "not found"}  # dependencies/, work-item-relations/, definitions

    def _relations(self, method: str, item: str, body):
        if method == "GET":
            groups: dict[str, list] = {name: [] for name in GROUPS}
            for blocked, blocker in self.rows:
                if blocker == item:
                    groups["blocking"].append({"project_id": PROJECT, "issue_id": blocked})
                if blocked == item:
                    groups["blocked_by"].append({"project_id": PROJECT, "issue_id": blocker})
            return 200, groups
        reverse = body["relation_type"] == "blocking"
        for other in body["issues"]:
            row = (other, item) if reverse else (item, other)
            if row not in self.rows:
                self.rows.append(row)
        return 201, [{"id": o, "project_id": PROJECT, "relation_type": "blocked_by"} for o in body["issues"]]


@pytest.fixture
def ce(monkeypatch):
    fake = FakeCE()

    class Handler(BaseHTTPRequestHandler):
        def _answer(self):
            length = int(self.headers.get("Content-Length") or 0)
            body = json.loads(self.rfile.read(length)) if length else None
            status, payload = fake.handle(self.command, self.path.split("?")[0], body)
            data = json.dumps(payload).encode()
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        do_GET = do_POST = do_PATCH = do_DELETE = _answer  # noqa: N815

        def log_message(self, format, *args):  # noqa: A002
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    client = PlaneClient(base_url=f"http://127.0.0.1:{server.server_port}", api_key="k")
    monkeypatch.setattr(workitem_relation, "get_plane_client_context", lambda: (client, "acme"))
    yield fake
    server.shutdown()
    server.server_close()


def _call(registered, **kwargs):
    kwargs.setdefault("project_id", PROJECT)
    return registered["workitem_relation"].fn(**kwargs)


def _ids(items):
    return [item["id"] if isinstance(item, dict) else item.id for item in items]


def test_list_returns_built_in_dependencies_when_custom_relations_are_missing(registered, ce):
    ce.rows.append((ITEM_A, ITEM_B))  # A is blocked by B

    result = _call(registered, action="list", workitem_id=ITEM_A)

    assert _ids(result["dependencies"]["blocked_by"]) == [ITEM_B]
    assert result["dependencies"]["blocking"] == []
    assert result["custom"] == {}
    assert "custom_relations" in result["unsupported"]


def test_list_definitions_marks_custom_definitions_unsupported_alongside_the_empty_list(registered, ce):
    result = _call(registered, action="list_definitions")

    assert result["built_in_dependencies"] == list(workitem_relation.DEPENDENCY_TYPES)
    assert result["custom_definitions"] == []
    assert "custom_definitions" in result["unsupported"]


@pytest.mark.parametrize(
    ("relation_type", "blocker", "blocked"),
    [("blocking", ITEM_A, ITEM_B), ("blocked_by", ITEM_B, ITEM_A)],
)
def test_create_then_list_keeps_the_blocking_direction(registered, ce, relation_type, blocker, blocked):
    created = _call(registered, action="create", workitem_id=ITEM_A, workitem_ids=[ITEM_B], relation_type=relation_type)

    assert _ids(created) == [ITEM_B]
    assert _ids(_call(registered, action="list", workitem_id=blocker)["dependencies"]["blocking"]) == [blocked]
    assert _ids(_call(registered, action="list", workitem_id=blocked)["dependencies"]["blocked_by"]) == [blocker]


def test_a_retried_create_reconciles_with_the_existing_relation(registered, ce):
    kwargs = {"action": "create", "workitem_id": ITEM_A, "workitem_ids": [ITEM_B], "relation_type": "blocking"}
    _call(registered, **kwargs)

    again = _call(registered, **kwargs)

    assert _ids(again) == [ITEM_B]
    assert ce.rows == [(ITEM_B, ITEM_A)]


def test_creating_a_custom_relation_is_refused_explicitly_and_writes_nothing(registered, ce):
    result = _call(
        registered,
        action="create",
        workitem_id=ITEM_A,
        workitem_ids=[ITEM_B],
        relation_definition_id="def-1",
        relation_definition_label="implements",
    )

    assert isinstance(result, str)
    assert result.startswith("Error:")
    assert "custom relations" in result
    assert ce.rows == []


@pytest.mark.parametrize("status", [401, 403, 500])
@pytest.mark.parametrize("route", ["work-item-relations", "work-item-relation-definitions"])
def test_auth_and_server_errors_on_the_custom_endpoints_are_not_swallowed(registered, ce, route, status):
    ce.forced[route] = status
    action = "list" if route == "work-item-relations" else "list_definitions"

    with pytest.raises(HttpError) as raised:
        _call(registered, action=action, workitem_id=ITEM_A)

    assert raised.value.status_code == status


@pytest.mark.parametrize("status", [401, 403, 500])
def test_a_failing_relations_route_is_not_reported_as_an_empty_result(registered, ce, status):
    ce.forced["relations"] = status

    with pytest.raises(HttpError) as raised:
        _call(registered, action="list", workitem_id=ITEM_A)

    assert raised.value.status_code == status


@pytest.mark.parametrize(
    "arguments",
    [
        {"action": "create_definition", "name": "implements"},
        {"action": "update_definition", "definition_id": "def-1", "name": "implements"},
        {"action": "delete_definition", "definition_id": "def-1"},
    ],
)
def test_definition_writes_are_refused_explicitly_when_the_endpoint_is_absent(registered, ce, arguments):
    result = _call(registered, **arguments)

    assert isinstance(result, str)
    assert result.startswith("Error:")
    assert "custom relation definitions" in result


@pytest.mark.parametrize("status", [401, 403, 500])
def test_definition_write_errors_are_not_swallowed(registered, ce, status):
    ce.forced["work-item-relation-definitions"] = status

    with pytest.raises(HttpError) as raised:
        _call(registered, action="create_definition", name="implements")

    assert raised.value.status_code == status
