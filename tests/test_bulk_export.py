"""Bulk export behavior, using temporary stores instead of real account data."""
from concurrent.futures import ThreadPoolExecutor
import ast
from pathlib import Path
from types import SimpleNamespace

from flask import Flask, jsonify, request
from export_history import ExportHistoryStore


def test_bulk_export_account_scope_and_idempotence(tmp_path):
    # Load the real endpoint without starting unrelated account/mail services.
    source = Path(__file__).resolve().parents[1] / "web_ui.py"
    tree = ast.parse(source.read_text(encoding="utf-8"))
    endpoint = next(node for node in tree.body if isinstance(node, ast.FunctionDef)
                    and node.name == "api_export_pickup_links")
    app = Flask(__name__)
    namespace = {"app": app, "jsonify": jsonify, "request": request}
    exec(compile(ast.Module(body=[endpoint], type_ignores=[]), str(source), "exec"), namespace)

    links = [
        {"alias_email": f"mail{i}@icloud.com", "account_id": "a1", "token": f"t{i}"}
        for i in range(5002)
    ] + [
        {"alias_email": "other@icloud.com", "account_id": "a2", "token": "other"},
        {"alias_email": "deleted@icloud.com", "account_id": "gone", "token": "gone"},
    ]
    store = ExportHistoryStore(tmp_path / "exports.json")
    store.claim([{"email": "mail0@icloud.com", "account_id": "a1"}])
    namespace.update(
        _pickup_store=SimpleNamespace(list_all=lambda: links),
        _export_store=store,
        _account_mgr=SimpleNamespace(accounts={"a1": {}, "a2": {}}),
        PICKUP_BASE_URL="https://mail.example.test",
    )

    def export(payload):
        with app.test_client() as client:
            response = client.post("/api/pickup-links/export", json=payload)
            return response.status_code, response.get_json()

    payload = {"unexported": True, "account_id": "a1"}
    assert export({"unexported": True, "account_id": ""})[0] == 400
    assert export({"unexported": True})[0] == 400
    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(export, [payload, payload]))
    assert all(status == 200 and data["ok"] for status, data in results)
    lines = [line for _, data in results for line in data["lines"]]
    assert len(lines) == len(set(lines)) == 5001
    assert "mail1@icloud.com----https://mail.example.test/pickup/t1" in lines
    assert not any(line.startswith(("mail0@", "other@", "deleted@")) for line in lines)
    assert export(payload)[1]["count"] == 0

    status, data = export({"unexported": True, "account_id": "all"})
    assert status == 200 and data["count"] == 1
    assert data["lines"] == ["other@icloud.com----https://mail.example.test/pickup/other"]
    assert export({"unexported": True, "account_id": "gone"})[0] == 400
    assert "deleted@icloud.com" not in store.status_map(["deleted@icloud.com"])
    assert export({"emails": ["other@icloud.com"]})[1]["count"] == 0
