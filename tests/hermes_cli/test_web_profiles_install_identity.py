"""FBL-375: HTTP roster identity shares the existing stable-install contract."""

from pathlib import Path

import pytest


@pytest.mark.parametrize("fallback", [False, True])
def test_profiles_body_identifies_its_install_without_a_status_read(tmp_path, monkeypatch, fallback):
    from fastapi.testclient import TestClient

    from hermes_cli import install_identity, profiles, web_server

    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    home = tmp_path / ".hermes"
    home.mkdir()
    (home / "config.yaml").write_text("{}\n")
    monkeypatch.setenv("HERMES_HOME", str(home))

    if fallback:
        def unavailable(**_kwargs):
            raise OSError("temporary inventory failure")
        monkeypatch.setattr(profiles, "list_profiles", unavailable)
        monkeypatch.setattr(profiles, "_check_gateway_running", lambda *_: False)

    expected = install_identity.get_install_id(cache={})
    assert expected
    with TestClient(web_server.app) as client:
        client.headers[web_server._SESSION_HEADER_NAME] = web_server._SESSION_TOKEN
        body = client.get("/api/profiles").json()

    assert body["install_id"] == expected
    assert "default" in [row["name"] for row in body["profiles"]]


def test_identity_failure_does_not_discard_the_profile_roster(tmp_path, monkeypatch):
    from fastapi.testclient import TestClient

    from hermes_cli import install_identity, profiles, web_server

    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    monkeypatch.setenv("HERMES_HOME", str(tmp_path / ".hermes"))
    monkeypatch.setattr(profiles, "list_profiles", lambda **_: [])

    def unavailable():
        raise OSError("identity unavailable")

    monkeypatch.setattr(install_identity, "get_install_id", unavailable)
    with TestClient(web_server.app) as client:
        client.headers[web_server._SESSION_HEADER_NAME] = web_server._SESSION_TOKEN
        response = client.get("/api/profiles")

    assert response.status_code == 200
    assert response.json()["profiles"] == []
    assert not response.json().get("install_id")
