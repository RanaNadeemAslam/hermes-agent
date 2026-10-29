"""Desktop/TUI creation uses operator defaults without reintroducing launch credentials."""
from pathlib import Path

from hermes_cli import profiles
from hermes_cli.config import atomic_config_replace, read_user_config_raw
from tui_gateway import server


def test_tui_creation_does_not_mirror_credentials_over_operator_reset(tmp_path, monkeypatch):
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    def notified(name):
        assert (home / "profiles" / name / "SOUL.md").read_text() == "caller role"
    monkeypatch.setattr(profiles, "_notify_multiplexer", notified)
    monkeypatch.setattr(profiles, "_maybe_register_gateway_service", lambda name: None)
    monkeypatch.setattr(profiles, "seed_profile_skills", lambda *a, **kw: None)
    template = tmp_path / "template"
    template.mkdir()
    (template / "manifest.yaml").write_text("config:\n  skills:\n    auto_load: [working-style]\n")
    atomic_config_replace(home / "config.yaml", {
        "profile_creation": {"enabled": True, "template": str(template), "reset_clone": True},
        "mcp_servers": {"project": {"command": "project", "env": {"TOKEN": "fake-source-key"}}},
    })
    (home / ".env").write_text("MCP_SECRET=fake-source-key\n")
    (home / "auth.json").write_text('{"private": "fake-source-token"}')
    response = server._methods["profiles.create"](1, {
        "name": "scout", "no_alias": True, "soul": "caller role",
        "description": "caller description", "clone_from": "default",
    })
    assert "error" not in response, response
    path = home / "profiles/scout"
    cfg = read_user_config_raw(path / "config.yaml")
    assert cfg["skills"]["auto_load"] == ["working-style"]
    assert not cfg.get("mcp_servers")
    assert "fake-source-key" not in (path / ".env").read_text()
    assert not (path / "auth.json").exists()
    assert (path / "SOUL.md").read_text() == "caller role"
    assert "caller description" in (path / "profile.yaml").read_text()
    assert response["result"]["mirrored"]["env"] is False
