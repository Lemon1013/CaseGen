"""Public registration, runtime registration switch, and self password change."""

from __future__ import annotations

from fastapi.testclient import TestClient
from sqlmodel import Session, select

from app import config
from app.db import get_engine
from app.main import create_app
from app.models.entities import AuthSession, SystemSetting, User
from app.services.auth import hash_password

ADMIN_PASSWORD = "CaseGen-Test1!"
MEMBER_PASSWORD = "CaseGen-Test1!"
NEW_PASSWORD = "CaseGen-Test2!"


def _origin() -> dict[str, str]:
    return {"Origin": "http://testserver"}


def _auth_client(tmp_app_data, monkeypatch) -> TestClient:
    monkeypatch.setattr(config, "AUTH_ENABLED", True)
    monkeypatch.setattr(config, "ALLOW_REGISTRATION", True)
    return TestClient(create_app())


def _setup_admin(client: TestClient) -> dict:
    response = client.post(
        "/api/auth/setup",
        headers=_origin(),
        json={
            "username": "rootadmin",
            "display_name": "Root",
            "password": ADMIN_PASSWORD,
        },
    )
    assert response.status_code == 200, response.text
    return response.json()["user"]


def _register(
    client: TestClient,
    username: str,
    password: str = MEMBER_PASSWORD,
    **extra,
):
    payload = {
        "username": username,
        "display_name": username.title(),
        "password": password,
    }
    payload.update(extra)
    return client.post("/api/auth/register", headers=_origin(), json=payload)


def _login(client: TestClient, username: str, password: str):
    return client.post(
        "/api/auth/login",
        headers=_origin(),
        json={"username": username, "password": password},
    )


def _create_member(admin_client: TestClient, username: str = "member") -> dict:
    created = admin_client.post(
        "/api/users",
        headers=_origin(),
        json={
            "username": username,
            "display_name": username.title(),
            "password": MEMBER_PASSWORD,
            "role": "user",
        },
    )
    assert created.status_code == 201, created.text
    return created.json()


def test_register_creates_ordinary_user_and_logs_in(tmp_app_data, monkeypatch):
    admin_client = _auth_client(tmp_app_data, monkeypatch)
    _setup_admin(admin_client)

    client = TestClient(create_app())
    response = _register(client, "newbie", display_name="New Bie")
    assert response.status_code == 201, response.text
    body = response.json()
    assert body["user"]["username"] == "newbie"
    assert body["user"]["display_name"] == "New Bie"
    assert body["user"]["role"] == "user"
    assert body["expires_at"]
    assert "password_hash" not in response.text

    # Registration behaves like login: both session cookies and an automatic
    # session are returned.
    assert response.cookies.get(config.AUTH_COOKIE_NAME)
    assert response.cookies.get(config.AUTH_CSRF_COOKIE_NAME)
    assert client.get("/api/auth/me").status_code == 200

    with Session(get_engine()) as session:
        row = session.exec(select(User).where(User.username == "newbie")).one()
        assert row.role == "user"
        assert row.is_active is True
        assert row.password_hash.startswith("scrypt$")
        assert MEMBER_PASSWORD not in row.password_hash


def test_register_rejects_duplicate_username(tmp_app_data, monkeypatch):
    admin_client = _auth_client(tmp_app_data, monkeypatch)
    _setup_admin(admin_client)

    assert _register(TestClient(create_app()), "duplicate").status_code == 201
    # A second request with the same normalized username must not create
    # another row even though the first registration already logged in.
    response = _register(TestClient(create_app()), "DUPLICATE")
    assert response.status_code == 409
    assert response.json()["detail"] == "用户名已存在"


def test_register_rejects_weak_password(tmp_app_data, monkeypatch):
    admin_client = _auth_client(tmp_app_data, monkeypatch)
    _setup_admin(admin_client)

    response = _register(TestClient(create_app()), "weakpass", password="short")
    assert response.status_code == 422
    assert "至少 10 位" in response.json()["detail"]


def test_register_enforces_each_password_policy_rule(tmp_app_data, monkeypatch):
    admin_client = _auth_client(tmp_app_data, monkeypatch)
    _setup_admin(admin_client)

    cases = (
        ("lowercaseonly", "abcdefghij"),
        ("missinglower", "ABCDEFGHI!"),
        ("missingspecial", "Abcdefghij"),
    )
    for username, value in cases:
        response = _register(TestClient(create_app()), username, password=value)
        assert response.status_code == 422, response.text
        assert "大写字母" in response.json()["detail"] or "特殊符号" in response.json()["detail"]

    accepted = _register(TestClient(create_app()), "policyok", password="CaseGen-Test1!")
    assert accepted.status_code == 201, accepted.text


def test_setup_rejects_noncompliant_password(tmp_app_data, monkeypatch):
    client = _auth_client(tmp_app_data, monkeypatch)
    response = client.post(
        "/api/auth/setup",
        headers=_origin(),
        json={"username": "admin", "password": "abcdefghij"},
    )
    assert response.status_code == 422
    assert "特殊符号" in response.json()["detail"]


def test_weak_legacy_password_still_logs_in(tmp_app_data, monkeypatch):
    client = _auth_client(tmp_app_data, monkeypatch)
    _setup_admin(client)
    with Session(get_engine()) as session:
        session.add(User(
            username="legacy",
            display_name="Legacy",
            password_hash=hash_password("weakpassword"),
            role="user",
            is_active=True,
        ))
        session.commit()
    assert _login(TestClient(create_app()), "legacy", "weakpassword").status_code == 200


def test_register_rejects_invalid_username(tmp_app_data, monkeypatch):
    admin_client = _auth_client(tmp_app_data, monkeypatch)
    _setup_admin(admin_client)

    for username in ("1starts-with-digit", "ab", "has space"):
        response = _register(TestClient(create_app()), username)
        assert response.status_code == 422, response.text
        assert response.json()["detail"] == (
            "用户名需为 3-32 位，以字母开头，仅限小写字母、数字、.、_、-"
        )


def test_register_cannot_inject_admin_role(tmp_app_data, monkeypatch):
    admin_client = _auth_client(tmp_app_data, monkeypatch)
    _setup_admin(admin_client)

    response = _register(
        TestClient(create_app()),
        "sneaky",
        role="admin",
        is_active=False,
    )
    assert response.status_code == 201, response.text
    assert response.json()["user"]["role"] == "user"
    with Session(get_engine()) as session:
        row = session.exec(select(User).where(User.username == "sneaky")).one()
        assert row.role == "user"
        assert row.is_active is True


def test_registration_setting_toggle_and_bootstrap(tmp_app_data, monkeypatch):
    admin_client = _auth_client(tmp_app_data, monkeypatch)
    _setup_admin(admin_client)

    # The configured default is open, so a user can register before the
    # administrator closes the door.
    member_client = TestClient(create_app())
    assert _register(member_client, "settingsuser").status_code == 201

    assert admin_client.get("/api/auth/registration-setting").json() == {"enabled": True}
    assert TestClient(create_app()).get("/api/auth/bootstrap").json()[
        "registration_enabled"
    ] is True

    disabled = admin_client.put(
        "/api/auth/registration-setting", headers=_origin(), json={"enabled": False}
    )
    assert disabled.status_code == 200
    assert disabled.json() == {"enabled": False}
    assert admin_client.get("/api/auth/registration-setting").json() == {"enabled": False}
    assert TestClient(create_app()).get("/api/auth/bootstrap").json()[
        "registration_enabled"
    ] is False

    blocked = _register(TestClient(create_app()), "blocked")
    assert blocked.status_code == 403
    assert blocked.json()["detail"] == "公开注册已关闭"

    # Only administrators may read or change the switch.
    assert member_client.get("/api/auth/registration-setting").status_code == 403
    assert member_client.put(
        "/api/auth/registration-setting", headers=_origin(), json={"enabled": True}
    ).status_code == 403
    assert TestClient(create_app()).get("/api/auth/registration-setting").status_code == 401

    enabled = admin_client.put(
        "/api/auth/registration-setting", headers=_origin(), json={"enabled": True}
    )
    assert enabled.status_code == 200
    assert enabled.json() == {"enabled": True}
    assert TestClient(create_app()).get("/api/auth/bootstrap").json()[
        "registration_enabled"
    ] is True
    assert admin_client.put(
        "/api/auth/registration-setting", headers=_origin(), json={}
    ).status_code == 422
    with Session(get_engine()) as session:
        row = session.get(SystemSetting, "allow_registration")
        assert row is not None
        assert row.value == "true"


def test_register_before_initial_setup_returns_conflict(tmp_app_data, monkeypatch):
    client = _auth_client(tmp_app_data, monkeypatch)

    bootstrap = client.get("/api/auth/bootstrap").json()
    assert bootstrap["setup_required"] is True
    assert bootstrap["registration_enabled"] is False

    response = _register(client, "earlybird")
    assert response.status_code == 409
    assert response.json()["detail"] == "请先完成初始化"


def test_registration_disabled_by_config_without_setting_row(tmp_app_data, monkeypatch):
    admin_client = _auth_client(tmp_app_data, monkeypatch)
    _setup_admin(admin_client)
    monkeypatch.setattr(config, "ALLOW_REGISTRATION", False)

    with Session(get_engine()) as session:
        assert session.get(SystemSetting, "allow_registration") is None

    bootstrap = TestClient(create_app()).get("/api/auth/bootstrap").json()
    assert bootstrap["setup_required"] is False
    assert bootstrap["registration_enabled"] is False

    response = _register(TestClient(create_app()), "configclosed")
    assert response.status_code == 403
    assert response.json()["detail"] == "公开注册已关闭"


def test_register_without_origin_is_blocked_by_csrf_boundary(tmp_app_data, monkeypatch):
    admin_client = _auth_client(tmp_app_data, monkeypatch)
    _setup_admin(admin_client)

    response = TestClient(create_app()).post(
        "/api/auth/register",
        json={
            "username": "noorigin",
            "display_name": "No Origin",
            "password": MEMBER_PASSWORD,
        },
    )
    assert response.status_code == 403
    assert response.json()["detail"] == "Origin or Referer header is required"


def test_register_rejects_case_insensitive_duplicate(tmp_app_data, monkeypatch):
    admin_client = _auth_client(tmp_app_data, monkeypatch)
    _setup_admin(admin_client)
    created = _create_member(admin_client, "Alice")
    assert created["username"] == "alice"

    response = _register(TestClient(create_app()), "alice")
    assert response.status_code == 409
    assert response.json()["detail"] == "用户名已存在"


def test_register_and_change_password_input_limits(tmp_app_data, monkeypatch):
    admin_client = _auth_client(tmp_app_data, monkeypatch)
    _setup_admin(admin_client)

    assert _register(TestClient(create_app()), "a" * 33).status_code == 422
    assert _register(
        TestClient(create_app()), "longdisplay", display_name="x" * 81
    ).status_code == 422
    assert _register(
        TestClient(create_app()), "longpass", password="x" * 1025
    ).status_code == 422

    member = _create_member(admin_client)
    client = TestClient(create_app())
    assert _login(client, member["username"], MEMBER_PASSWORD).status_code == 200
    response = client.post(
        "/api/auth/change-password",
        headers=_origin(),
        json={
            "current_password": "x" * 1025,
            "new_password": NEW_PASSWORD,
        },
    )
    assert response.status_code == 422


def test_change_password_revokes_every_session(tmp_app_data, monkeypatch):
    admin_client = _auth_client(tmp_app_data, monkeypatch)
    _setup_admin(admin_client)
    member = _create_member(admin_client)

    first = TestClient(create_app())
    second = TestClient(create_app())
    assert _login(first, member["username"], MEMBER_PASSWORD).status_code == 200
    assert _login(second, member["username"], MEMBER_PASSWORD).status_code == 200

    changed = first.post(
        "/api/auth/change-password",
        headers=_origin(),
        json={
            "current_password": MEMBER_PASSWORD,
            "new_password": NEW_PASSWORD,
        },
    )
    assert changed.status_code == 200, changed.text
    assert changed.json() == {"detail": "密码已修改，请重新登录"}

    # The session used for the change is cleared and every other session is
    # revoked server-side, so the old cookie cannot be reused.
    assert first.cookies.get(config.AUTH_COOKIE_NAME) is None
    assert second.get("/api/auth/me").status_code == 401
    with Session(get_engine()) as session:
        assert session.exec(
            select(AuthSession).where(AuthSession.user_id == member["id"])
        ).all() == []
    # Revocation is scoped to the target account: the administrator session
    # used to create the member must remain fully usable.
    assert admin_client.get("/api/users", headers=_origin()).status_code == 200

    fresh = TestClient(create_app())
    assert _login(fresh, member["username"], NEW_PASSWORD).status_code == 200
    assert _login(TestClient(create_app()), member["username"], MEMBER_PASSWORD).status_code == 401


def test_change_password_errors_keep_session_valid(tmp_app_data, monkeypatch):
    admin_client = _auth_client(tmp_app_data, monkeypatch)
    _setup_admin(admin_client)
    member = _create_member(admin_client)

    # Anonymous callers are stopped by the global middleware.
    assert TestClient(create_app()).post(
        "/api/auth/change-password",
        headers=_origin(),
        json={
            "current_password": MEMBER_PASSWORD,
            "new_password": NEW_PASSWORD,
        },
    ).status_code == 401

    client = TestClient(create_app())
    assert _login(client, member["username"], MEMBER_PASSWORD).status_code == 200

    wrong = client.post(
        "/api/auth/change-password",
        headers=_origin(),
        json={
            "current_password": "DefinitelyWrong1",
            "new_password": NEW_PASSWORD,
        },
    )
    assert wrong.status_code == 400
    assert wrong.json()["detail"] == "当前密码不正确"

    short = client.post(
        "/api/auth/change-password",
        headers=_origin(),
        json={
            "current_password": MEMBER_PASSWORD,
            "new_password": "short",
        },
    )
    assert short.status_code == 422

    unchanged = client.post(
        "/api/auth/change-password",
        headers=_origin(),
        json={
            "current_password": MEMBER_PASSWORD,
            "new_password": MEMBER_PASSWORD,
        },
    )
    assert unchanged.status_code == 422
    assert unchanged.json()["detail"] == "新密码不能与当前密码相同"

    # Rejected attempts must not log the user out.
    assert client.get("/api/auth/me").status_code == 200


def test_change_password_rejects_noncompliant_new_password(tmp_app_data, monkeypatch):
    admin_client = _auth_client(tmp_app_data, monkeypatch)
    _setup_admin(admin_client)
    member = _create_member(admin_client)
    client = TestClient(create_app())
    assert _login(client, member["username"], MEMBER_PASSWORD).status_code == 200
    response = client.post(
        "/api/auth/change-password",
        headers=_origin(),
        json={"current_password": MEMBER_PASSWORD, "new_password": "abcdefghij"},
    )
    assert response.status_code == 422
    assert "大写字母" in response.json()["detail"]
