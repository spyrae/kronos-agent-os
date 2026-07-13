"""Dashboard auth hardening: HttpOnly cookie flow, login rate-limit, logout/me.

Covers the review's dashboard-security findings:
  * the session token is delivered only as an HttpOnly + SameSite=Strict cookie
    (never in the response body, so it can't reach localStorage/JS);
  * login is rate-limited per IP to blunt password brute-forcing;
  * logout invalidates the server-side session, and /me is the SPA's auth probe.
"""

import pytest
from fastapi.testclient import TestClient


@pytest.fixture
def client(monkeypatch):
    import dashboard.auth as auth

    monkeypatch.setattr(auth, "DASHBOARD_USERNAME", "admin")
    monkeypatch.setattr(auth, "DASHBOARD_PASSWORD", "s3cret")
    auth._sessions.clear()
    auth._login_failures.clear()

    from dashboard.server import create_app

    return TestClient(create_app())


def test_login_sets_httponly_samesite_cookie_and_no_token_in_body(client):
    from dashboard.auth import COOKIE_NAME

    res = client.post("/api/auth/login", json={"username": "admin", "password": "s3cret"})

    assert res.status_code == 200
    assert res.json() == {"ok": True}  # token is NOT in the body
    assert res.cookies.get(COOKIE_NAME)  # ...it's a cookie instead

    set_cookie = res.headers.get("set-cookie", "").lower()
    assert "httponly" in set_cookie
    assert "samesite=strict" in set_cookie


def test_me_requires_valid_cookie(client):
    assert client.get("/api/auth/me").status_code == 401

    client.post("/api/auth/login", json={"username": "admin", "password": "s3cret"})
    res = client.get("/api/auth/me")  # cookie carried by the client jar

    assert res.status_code == 200
    assert res.json()["authenticated"] is True


def test_logout_invalidates_session(client):
    client.post("/api/auth/login", json={"username": "admin", "password": "s3cret"})
    assert client.get("/api/auth/me").status_code == 200

    assert client.post("/api/auth/logout").status_code == 200

    from dashboard.auth import _sessions

    assert _sessions == {}  # server-side session dropped
    assert client.get("/api/auth/me").status_code == 401  # stale cookie no longer valid


def test_bad_password_is_rejected_without_cookie(client):
    from dashboard.auth import COOKIE_NAME

    res = client.post("/api/auth/login", json={"username": "admin", "password": "wrong"})

    assert res.status_code == 401
    assert res.cookies.get(COOKIE_NAME) is None


def test_login_rate_limited_after_repeated_failures(client):
    from dashboard.auth import LOGIN_MAX_FAILURES

    for _ in range(LOGIN_MAX_FAILURES):
        res = client.post("/api/auth/login", json={"username": "admin", "password": "wrong"})
        assert res.status_code == 401

    # Further attempts are throttled — even with the CORRECT password.
    res = client.post("/api/auth/login", json={"username": "admin", "password": "s3cret"})
    assert res.status_code == 429
    assert "retry-after" in {k.lower() for k in res.headers}
