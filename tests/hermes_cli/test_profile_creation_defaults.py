"""Operator templates apply before publication on every creation surface."""
import json
from pathlib import Path

import hermes_yaml as yaml
import pytest

from hermes_cli import profiles
from hermes_cli.config import atomic_config_replace, read_user_config_raw


@pytest.fixture
def operator(tmp_path, monkeypatch):
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setattr(profiles, "_notify_multiplexer", lambda name: None)
    monkeypatch.setattr(profiles, "_maybe_register_gateway_service", lambda name: None)
    template = tmp_path / "template"
    template.mkdir()
    (template / "bank.json").write_text(json.dumps({
        "write_bank": "private-{{profile_name}}", "recall_banks": ["shared"],
    }))
    plugin = template / "provider"
    plugin.mkdir()
    (plugin / "__init__.py").write_text("# operator-selected provider code\n")
    manifest = {
        "config": {
            "skills": {"create_dir": str(tmp_path / "shared-skills"),
                       "external_dirs": [str(tmp_path / "shared-skills")],
                       "auto_load": ["working-style", "operating-policy"]},
            "memory": {"provider": "example"},
            "toolsets": ["skills", "memory", "file", "terminal", "session_search"],
        },
        "resources": [
            {"source": "bank.json", "target": "example/config.json", "render": True},
            {"source": "provider", "target": "plugins/example"},
        ],
    }
    (template / "manifest.yaml").write_text(yaml.safe_dump(manifest))
    atomic_config_replace(home / "config.yaml", {
        "profile_creation": {"enabled": True, "template": str(template), "reset_clone": True},
        "model": {"provider": "openai", "default": "chosen-model"},
    })
    return home, template


def config(path):
    return read_user_config_raw(path / "config.yaml")


def test_blank_profiles_get_defaults_and_distinct_final_identity(operator):
    home, _ = operator
    first = profiles.create_profile("Scout", no_alias=True)
    second = profiles.create_profile("helper", no_alias=True)
    for path in (first, second):
        cfg = config(path)
        assert cfg["skills"]["auto_load"] == ["working-style", "operating-policy"]
        assert cfg["memory"]["provider"] == "example"
        assert "skills" in cfg["toolsets"]
        assert (path / "plugins/example/__init__.py").is_file()
        bank = json.loads((path / "example/config.json").read_text())
        assert bank == {"write_bank": f"private-{path.name}", "recall_banks": ["shared"]}
        assert cfg["model"]["default"] == "chosen-model"
    assert config(home)["profile_creation"]["enabled"] is True


@pytest.mark.parametrize("clone_all", [False, True])
@pytest.mark.parametrize("explicit", [False, True])
def test_reset_clones_leave_source_private_state_and_keep_caller_identity(
    operator, monkeypatch, clone_all, explicit,
):
    home, _ = operator
    source = home / "profiles/source"
    source.mkdir(parents=True)
    atomic_config_replace(source / "config.yaml", {
        "model": {"provider": "openai", "default": "source-model"},
        "memory": {"provider": "example", "private_bank": "source-private"},
        "terminal": {"cwd": "/source/project"},
        "mcp_servers": {"project": {"command": "project", "env": {"TOKEN": "fake-secret"}}},
        # A named caller must not replace the operator's policy.
        "profile_creation": {"enabled": False},
    })
    for name, text in {
        "SOUL.md": "source product identity", "profile.yaml": "description: source product\n",
        ".env": "MCP_SECRET=fake-source-secret\n", "auth.json": '{"private": "fake-token"}',
        "mcp-tokens.json": '{"private": "fake-token"}',
        "memories/MEMORY.md": "source project facts", "memories/USER.md": "source user",
        "example/config.json": '{"write_bank": "source-private"}',
        "plugins/unselected/__init__.py": "# source plugin\n",
    }.items():
        path = source / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text)
    external = home.parent / "outside"
    external.mkdir()
    (external / "keep").write_text("untouched")
    (source / "private-link").symlink_to(external, target_is_directory=True)
    before = {p.relative_to(source): p.read_bytes() for p in source.rglob("*") if p.is_file()}
    monkeypatch.setenv("HERMES_HOME", str(source))
    path = profiles.create_profile(
        "New-Agent", clone_from="source", clone_all=clone_all, no_alias=True,
        soul="explicit role" if explicit else None,
        description="explicit project" if explicit else None,
    )
    cfg = config(path)
    assert cfg["model"]["default"] == "source-model"
    assert cfg["memory"].get("private_bank") is None
    assert not cfg.get("mcp_servers")
    assert not cfg.get("terminal", {}).get("cwd")
    if explicit:
        assert (path / "SOUL.md").read_text() == "explicit role"
        assert "explicit project" in (path / "profile.yaml").read_text()
    else:
        assert "source product identity" not in (path / "SOUL.md").read_text()
        assert not (path / "profile.yaml").exists()
    for rel in ("auth.json", "mcp-tokens.json", "memories/MEMORY.md", "memories/USER.md",
                "plugins/unselected", "private-link"):
        assert not (path / rel).exists()
    assert "fake-source-secret" not in (path / ".env").read_text()
    assert json.loads((path / "example/config.json").read_text())["write_bank"] == "private-new-agent"
    assert {p.relative_to(source): p.read_bytes() for p in source.rglob("*") if p.is_file()} == before
    assert (external / "keep").read_text() == "untouched"


def test_no_skills_is_explicit_opt_out(operator):
    _, template = operator
    (template / "skill").write_text("skill")
    manifest = yaml.safe_load((template / "manifest.yaml").read_text())
    manifest["resources"].append({"source": "skill", "target": "skills/team/SKILL.md"})
    (template / "manifest.yaml").write_text(yaml.safe_dump(manifest))
    path = profiles.create_profile("empty", no_skills=True, no_alias=True)
    assert not config(path).get("skills", {}).get("auto_load")
    assert not config(path).get("skills", {}).get("external_dirs")
    assert not (path / "skills/team").exists()
    assert (path / profiles.NO_BUNDLED_SKILLS_MARKER).exists()
    assert (path / "example/config.json").exists()


@pytest.mark.parametrize("target,content,link", [
    ("../escape", "{}", False),
    ("config.yaml", "{}", False),
    ("CONFIG.YAML", "{}", False),
    (".profile-creation.json", "{}", False),
    ("example/config.json", "{{unknown}}", False),
    ("example/config.json", "{}", True),
])
def test_invalid_resources_never_publish(operator, monkeypatch, target, content, link):
    home, template = operator
    notified = []
    monkeypatch.setattr(profiles, "_notify_multiplexer", notified.append)
    manifest_path = template / "manifest.yaml"
    manifest = yaml.safe_load(manifest_path.read_text())
    manifest["resources"][0]["target"] = target
    manifest_path.write_text(yaml.safe_dump(manifest))
    (template / "bank.json").write_text(content)
    if link:
        (template / "bank.json").unlink()
        (template / "bank.json").symlink_to(home / "config.yaml")
    with pytest.raises(ValueError):
        profiles.create_profile("invalid", no_alias=True)
    assert not (home / "profiles/invalid").exists()
    assert not list((home / "profiles").glob(".*.staging-*"))
    assert not notified


@pytest.mark.parametrize("contents", [None, "config: [broken\n", "config: []", "resources: {}"])
def test_missing_or_malformed_manifest_never_publishes(operator, contents):
    home, template = operator
    manifest = template / "manifest.yaml"
    if contents is None:
        manifest.unlink()
    else:
        manifest.write_text(contents)
    with pytest.raises((ValueError, FileNotFoundError)):
        profiles.create_profile("invalid", no_alias=True)
    assert not (home / "profiles/invalid").exists()
    assert not list((home / "profiles").glob(".*.staging-*"))


def test_disabled_policy_preserves_ordinary_clones(operator):
    home, _ = operator
    cfg = config(home)
    cfg["profile_creation"] = {"enabled": False, "template": "/missing", "reset_clone": True}
    atomic_config_replace(home / "config.yaml", cfg)
    (home / "SOUL.md").write_text("keep source soul")
    (home / ".env").write_text("MODEL_API_KEY=fake-key\n")
    path = profiles.create_profile("ordinary", clone_config=True, no_alias=True)
    assert (path / "SOUL.md").read_text() == "keep source soul"
    assert "fake-key" in (path / ".env").read_text()
    assert not (path / "example/config.json").exists()


def test_template_cannot_write_through_cloned_directory_link(operator):
    home, _ = operator
    cfg = config(home)
    cfg["profile_creation"]["reset_clone"] = False
    atomic_config_replace(home / "config.yaml", cfg)
    source = home / "profiles/linked"
    source.mkdir(parents=True)
    atomic_config_replace(source / "config.yaml", {"model": "selected"})
    outside = home.parent / "outside"
    outside.mkdir()
    (outside / "config.json").write_text("private source bank")
    (source / "example").symlink_to(outside, target_is_directory=True)
    with pytest.raises(ValueError, match="link"):
        profiles.create_profile("blocked", clone_from="linked", clone_all=True, no_alias=True)
    assert (outside / "config.json").read_text() == "private source bank"
    assert not (home / "profiles/blocked").exists()


def test_registered_policy_loads_and_template_cannot_replace_selected_model(operator):
    from hermes_cli.config import DEFAULT_CONFIG, _KNOWN_ROOT_KEYS, load_config
    home, template = operator
    assert "profile_creation" in DEFAULT_CONFIG
    assert "profile_creation" in _KNOWN_ROOT_KEYS
    assert load_config()["profile_creation"]["enabled"] is True
    manifest = yaml.safe_load((template / "manifest.yaml").read_text())
    manifest["config"]["model"] = "template-model"
    (template / "manifest.yaml").write_text(yaml.safe_dump(manifest))
    path = profiles.create_profile("model-test", no_alias=True)
    assert config(path)["model"] == config(home)["model"]


@pytest.mark.parametrize("policy", [
    {"enabled": True}, {"enabled": "yes"}, {"enabled": True, "template": 5},
    {"enabled": True, "template": "missing", "reset_clone": "yes"},
    {"enabled": True, "template": "missing", "typo": True},
])
def test_invalid_policy_fails_closed(operator, policy):
    home, _ = operator
    atomic_config_replace(home / "config.yaml", {"profile_creation": policy})
    with pytest.raises(ValueError):
        profiles.create_profile("bad-policy", no_alias=True)
    assert not (home / "profiles/bad-policy").exists()


def test_cli_creation_applies_template_and_explicit_description(operator, monkeypatch):
    from types import SimpleNamespace
    from hermes_cli.profile_cmd import _profile_create
    home, _ = operator
    monkeypatch.setattr(profiles, "seed_profile_skills", lambda *a, **kw: None)
    _profile_create(SimpleNamespace(profile_name="cli-agent", no_alias=True, description="CLI role"))
    path = home / "profiles/cli-agent"
    assert config(path)["skills"]["auto_load"] == ["working-style", "operating-policy"]
    assert "CLI role" in (path / "profile.yaml").read_text()


@pytest.mark.asyncio
async def test_rest_creation_applies_template_and_explicit_identity(operator, monkeypatch):
    from hermes_cli.web_routers.profiles import ProfileCreate, create_profile_endpoint
    home, _ = operator
    monkeypatch.setattr(profiles, "seed_profile_skills", lambda *a, **kw: None)
    monkeypatch.setattr(profiles, "check_alias_collision", lambda name: "skip alias in test")
    result = await create_profile_endpoint(ProfileCreate(
        name="rest-agent", clone_from_default=False, soul="REST role", description="REST project",
    ))
    assert result["ok"] is True
    path = home / "profiles/rest-agent"
    assert config(path)["memory"]["provider"] == "example"
    assert json.loads((path / "example/config.json").read_text())["write_bank"] == "private-rest-agent"
    assert (path / "SOUL.md").read_text() == "REST role"
    assert "REST project" in (path / "profile.yaml").read_text()


@pytest.mark.parametrize("disable_after_publish", [False, True])
def test_cli_reset_skips_legacy_provider_clone(operator, monkeypatch, disable_after_publish):
    from types import SimpleNamespace
    from hermes_cli.profile_cmd import _profile_create
    import plugins.memory
    home, _ = operator
    cloned = []
    provider_cli = SimpleNamespace(ConfigWriteRefused=ValueError,
                                   clone_honcho_for_profile=lambda name: cloned.append(name))
    monkeypatch.setattr(plugins.memory, "import_provider_module", lambda *a: provider_cli)
    def notified(name):
        if disable_after_publish:
            cfg = config(home)
            cfg["profile_creation"]["enabled"] = False
            atomic_config_replace(home / "config.yaml", cfg)
    monkeypatch.setattr(profiles, "_notify_multiplexer", notified)
    _profile_create(SimpleNamespace(profile_name="clone-cli", clone=True, no_alias=True))
    assert config(home / "profiles/clone-cli")["memory"]["provider"] == "example"
    assert not cloned


def test_creation_uuid_changes_on_same_name_recreation_and_is_shared(operator, monkeypatch):
    import shutil
    from uuid import UUID
    home, template = operator
    (template / "bank.json").write_text(json.dumps({
        "write_bank": "private-{{profile_id}}", "profile_name": "{{profile_name}}",
    }))
    (template / "identity.txt").write_text("{{profile_id}}")
    manifest = yaml.safe_load((template / "manifest.yaml").read_text())
    manifest["resources"].append({"source": "identity.txt", "target": "identity.txt", "render": True})
    (template / "manifest.yaml").write_text(yaml.safe_dump(manifest))
    seen = []

    def notified(name):
        path = home / "profiles" / name
        receipt = json.loads((path / ".profile-creation.json").read_text())
        instance = receipt["profile_id"]
        assert UUID(instance).version == 4
        assert receipt["policy_applied"] is True
        assert receipt["reset_identity"] is True
        assert receipt["template"] == str(template)
        assert (path / "identity.txt").read_text() == instance
        assert json.loads((path / "example/config.json").read_text()) == {
            "write_bank": f"private-{instance}", "profile_name": "repeat",
        }
        seen.append(instance)

    monkeypatch.setattr(profiles, "_notify_multiplexer", notified)
    first = profiles.create_profile("Repeat", no_alias=True)
    shutil.rmtree(first)
    profiles.create_profile("Repeat", no_alias=True)
    assert len(seen) == 2 and seen[0] != seen[1]


def test_disabled_clone_gets_own_receipt_without_inheriting_reset(operator):
    from hermes_cli.profiles_creation_defaults import creation_resets_identity
    home, _ = operator
    source = profiles.create_profile("source", no_alias=True)
    source_receipt = (source / ".profile-creation.json").read_bytes()
    (source / "SOUL.md").write_text("source role")
    (source / ".env").write_text("TOOL_API_KEY=fake-source-key\n")
    cfg = config(home)
    cfg["profile_creation"]["enabled"] = False
    atomic_config_replace(home / "config.yaml", cfg)
    path = profiles.create_profile("copy", clone_from="source", clone_all=True, no_alias=True)
    receipt = json.loads((path / ".profile-creation.json").read_text())
    assert receipt["profile_id"] != json.loads(source_receipt)["profile_id"]
    assert receipt["policy_applied"] is False
    assert receipt["reset_identity"] is False
    assert not creation_resets_identity(path)
    assert creation_resets_identity(source)
    assert (source / ".profile-creation.json").read_bytes() == source_receipt
    assert (path / "SOUL.md").read_text() == "source role"
    assert "fake-source-key" in (path / ".env").read_text()


def test_creation_identity_survives_rename_and_later_load(operator, monkeypatch):
    from hermes_cli import profile_identity, profiles_honcho
    from hermes_cli.config_effective import load_user_config_effective
    from hermes_cli.profiles_creation_defaults import creation_resets_identity
    _, template = operator
    (template / "bank.json").write_text('{"write_bank": "private-{{profile_id}}"}')
    source = profiles.create_profile("original", no_alias=True)
    receipt = (source / ".profile-creation.json").read_bytes()
    bank = (source / "example/config.json").read_bytes()
    monkeypatch.setattr(profiles, "_check_gateway_running", lambda *a: False)
    monkeypatch.setattr(profiles, "_cleanup_gateway_service", lambda *a: False)
    monkeypatch.setattr(profiles, "_maybe_unregister_gateway_service", lambda *a: None)
    monkeypatch.setattr(profiles, "_stop_bot_desktop", lambda *a: None)
    monkeypatch.setattr(profiles, "_live_default_multiplexer", lambda: False)
    monkeypatch.setattr(profiles, "check_alias_collision", lambda *a: "skip test alias")
    monkeypatch.setattr(profile_identity, "_migrate_profile_identity", lambda *a: None)
    monkeypatch.setattr(profiles_honcho, "migrate_honcho_profile_host", lambda *a: None)
    renamed = profiles.rename_profile("original", "renamed")
    assert load_user_config_effective(renamed / "config.yaml")["memory"]["provider"] == "example"
    assert creation_resets_identity(renamed)
    assert (renamed / ".profile-creation.json").read_bytes() == receipt
    assert (renamed / "example/config.json").read_bytes() == bank


def test_disabled_clone_replaces_receipt_link_without_writing_outside(operator):
    home, _ = operator
    source = profiles.create_profile("source", no_alias=True)
    marker = source / ".profile-creation.json"
    outside = home.parent / "outside-receipt.json"
    outside.write_bytes(marker.read_bytes())
    original = outside.read_bytes()
    marker.unlink()
    marker.symlink_to(outside)
    cfg = config(home)
    cfg["profile_creation"]["enabled"] = False
    atomic_config_replace(home / "config.yaml", cfg)
    cloned = profiles.create_profile("copy", clone_from="source", clone_all=True, no_alias=True)
    assert outside.read_bytes() == original
    assert marker.is_symlink()
    assert not (cloned / ".profile-creation.json").is_symlink()
    assert json.loads((cloned / ".profile-creation.json").read_text())["profile_id"] != json.loads(original)["profile_id"]
