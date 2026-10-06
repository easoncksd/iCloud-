"""Regression tests for API hardening and stale UI protection.

These tests intentionally avoid real iCloud/network access.  They are kept
separate from the older regression suite so security-contract changes can be
reviewed and run independently.
"""

from pathlib import Path


def test_api_responses_are_uncached_and_have_request_id():
    import web_ui

    response = web_ui.app.test_client().get("/api/state")
    assert response.status_code == 200
    assert response.headers["Cache-Control"].startswith("no-store")
    assert response.headers["Referrer-Policy"] == "no-referrer"
    assert response.headers.get("X-Content-Type-Options") == "nosniff"
    assert response.headers.get("X-Request-ID")


def test_mail_watch_status_exposes_cycle_state_without_credentials():
    import web_ui

    response = web_ui.app.test_client().get("/api/mail-watch/status")
    payload = response.get_json()
    assert response.status_code == 200
    assert payload["ok"] is True
    assert 1 <= payload["interval_hours"] <= 24
    assert payload["status"]["state"] in {"starting", "waiting", "running", "idle"}
    assert "app_password" not in payload


def test_unknown_api_route_has_sanitized_json_error():
    import web_ui

    response = web_ui.app.test_client().get("/api/not-a-real-route")
    payload = response.get_json()
    assert response.status_code == 404
    assert payload["ok"] is False
    assert payload.get("request_id")
    assert "Not Found" not in payload.get("error", "")


def test_pickup_list_does_not_return_bearer_token(monkeypatch):
    import web_ui

    class Store:
        def list_all(self):
            return [{
                "account_id": "acc-1",
                "alias_email": "one@icloud.com",
                "token": "secret-token",
                "created_at": "2026-01-01T00:00:00+00:00",
            }]

    monkeypatch.setattr(web_ui, "_pickup_store", Store())
    payload = web_ui.app.test_client().get("/api/pickup-links").get_json()
    link = payload["links"][0]
    assert "token" not in link
    assert link["url"].endswith("/pickup/secret-token")


def test_pickup_creation_requires_account_alias_ownership(monkeypatch):
    import web_ui

    class Manager:
        _latest_emails_lock = __import__("threading").RLock()

        def get_account(self, account_id):
            return {"id": account_id} if account_id == "acc-1" else None

    monkeypatch.setattr(web_ui, "_account_mgr", Manager())
    response = web_ui.app.test_client().post(
        "/api/pickup-links/acc-1/not-owned@icloud.com"
    )
    assert response.status_code == 404
    assert response.get_json()["ok"] is False


def test_alias_mail_endpoint_requires_account_alias_ownership(monkeypatch, tmp_path):
    import threading
    import web_ui

    class Manager:
        _latest_emails_lock = threading.RLock()

        def get_account(self, account_id):
            return {"id": account_id} if account_id == "acc-1" else None

    monkeypatch.setattr(web_ui, "_account_mgr", Manager())
    monkeypatch.setattr(web_ui, "RESULTS_DIR", tmp_path)
    response = web_ui.app.test_client().get(
        "/api/accounts/acc-1/mail/not-owned@icloud.com"
    )
    assert response.status_code == 404
    assert response.get_json()["ok"] is False


def test_inbox_limit_is_bounded(monkeypatch):
    import web_ui

    seen = {}

    class Manager:
        class Cache:
            @staticmethod
            def get_stats(account_id):
                return {}

        _cache = Cache()

        def check_inbox(self, account_id, limit=20, **kwargs):
            seen["limit"] = limit
            return []

    monkeypatch.setattr(web_ui, "_account_mgr", Manager())
    monkeypatch.setattr(web_ui, "_apply_mail_watch_result", lambda *args, **kwargs: None)
    response = web_ui.app.test_client().get("/api/accounts/acc-1/inbox?limit=999999")
    assert response.status_code == 200
    assert seen["limit"] == 100


def test_ui_api_keeps_existing_data_on_failed_refresh_and_handles_status():
    import web_ui

    html = web_ui.UI_HTML
    assert "if(d.ok===false||!Array.isArray(d.emails))" in html
    assert "r.status===401||r.status===403" in html
    assert "r.status===404" in html
    assert "_refreshQueued" in html
    assert "_batchGeneration" in html

