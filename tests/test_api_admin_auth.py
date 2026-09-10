import pytest

from nerya.api import auth, route_scopes
from nerya.core import yaml_io
from nerya.core.config import DEFAULT_CONFIG, load_config


@pytest.mark.smoke
def test_admin_password_issues_jwt_for_remote_requests(tmp_path):
    cfg = load_config(tmp_path)

    auth.set_admin_password(cfg, "correct-horse")
    assert auth.verify_admin_password(cfg, "correct-horse")
    assert not auth.verify_admin_password(cfg, "wrong")

    token = auth.issue_admin_jwt(cfg)["token"]
    result = auth.check_request(
        cfg,
        method="GET",
        path="/workspace",
        client_addr="203.0.113.10",
        headers={"authorization": f"Bearer {token}"},
    )
    assert result.ok
    assert result.actor == "admin:password"
    assert route_scopes.WILDCARD_SCOPE in result.scopes

    saved = yaml_io.load(tmp_path / "nerya.yml", default={})
    runtime_auth = saved["runtime"]["auth"]
    assert runtime_auth["admin_password_hash"].startswith("pbkdf2_sha256$")
    assert "correct-horse" not in yaml_io.dumps(saved)
    assert runtime_auth["jwt_secret"]


@pytest.mark.smoke
def test_remote_without_jwt_is_rejected_after_password_is_configured(tmp_path):
    cfg = load_config(tmp_path)
    auth.set_admin_password(cfg, "correct-horse")

    result = auth.check_request(
        cfg,
        method="GET",
        path="/workspace",
        client_addr="198.51.100.4",
        headers={},
    )

    assert not result.ok
    assert result.status == 401
    assert result.reason == "missing_token"


@pytest.mark.smoke
def test_admin_password_does_not_mutate_default_config(tmp_path):
    cfg = load_config(tmp_path)

    auth.set_admin_password(cfg, "correct-horse")

    assert "auth" not in DEFAULT_CONFIG["runtime"]


@pytest.mark.smoke
def test_forwarded_remote_host_breaks_loopback_trust_lane(tmp_path):
    cfg = load_config(tmp_path)
    auth.set_admin_password(cfg, "correct-horse")

    result = auth.check_request(
        cfg,
        method="GET",
        path="/workspace",
        client_addr="127.0.0.1",
        headers={"x-forwarded-for": "198.51.100.5"},
    )

    assert not result.ok
    assert result.status == 401
    assert result.reason == "missing_token"


@pytest.mark.smoke
def test_spoofed_first_xff_entry_does_not_grant_loopback_trust(tmp_path):
    # A remote caller through the edge proxy can supply its own chain head;
    # only the LAST entry (appended by our edge) may decide, so the spoofed
    # loopback head must not resurrect the local trust lane.
    cfg = load_config(tmp_path)
    auth.set_admin_password(cfg, "correct-horse")

    result = auth.check_request(
        cfg,
        method="GET",
        path="/workspace",
        client_addr="127.0.0.1",
        headers={"x-forwarded-for": "127.0.0.1, 198.51.100.5"},
    )

    assert not result.ok
    assert result.status == 401
    assert result.reason == "missing_token"


@pytest.mark.smoke
def test_x_real_ip_cannot_override_forwarded_remote_verdict(tmp_path):
    # x-real-ip is client-settable and must never be consulted: once the
    # x-forwarded-for chain (edge-appended last entry) says remote, a spoofed
    # loopback x-real-ip cannot resurrect the local trust lane.
    cfg = load_config(tmp_path)
    auth.set_admin_password(cfg, "correct-horse")

    result = auth.check_request(
        cfg,
        method="GET",
        path="/workspace",
        client_addr="127.0.0.1",
        headers={"x-forwarded-for": "198.51.100.5", "x-real-ip": "127.0.0.1"},
    )

    assert not result.ok
    assert result.status == 401
    assert result.reason == "missing_token"


@pytest.mark.smoke
def test_loopback_peer_without_forwarded_headers_keeps_local_lane(tmp_path):
    cfg = load_config(tmp_path)

    result = auth.check_request(
        cfg,
        method="GET",
        path="/workspace",
        client_addr="127.0.0.1",
        headers={},
    )

    assert result.ok
    assert result.actor == "local:loopback"


@pytest.mark.smoke
def test_auth_bootstrap_routes_are_anonymous_but_password_write_is_config_gated():
    assert route_scopes.required_scope("GET", "/auth/status") is None
    assert route_scopes.required_scope("POST", "/auth/login") is None
    assert route_scopes.required_scope("POST", "/auth/admin/password") == "write:config"
