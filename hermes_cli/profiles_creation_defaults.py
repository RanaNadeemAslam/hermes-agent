"""Trusted operator templates for new profiles, applied only in hidden staging trees."""
import json
from pathlib import Path
from uuid import uuid4
import shutil
import stat

import hermes_yaml as yaml

CREATION_MARKER = ".profile-creation.json"


def load_creation_policy() -> dict | None:
    """Read the operator's policy, never the named caller's project configuration."""
    from hermes_cli.config_effective import load_user_config_effective
    from hermes_cli.profiles import _get_default_hermes_home

    root = _get_default_hermes_home()
    policy = load_user_config_effective(root / "config.yaml", fail_closed=True).get("profile_creation", {})
    if not isinstance(policy, dict):
        raise ValueError("profile_creation must be a mapping")
    enabled = policy.get("enabled", False)
    if not isinstance(enabled, bool):
        raise ValueError("profile_creation.enabled must be boolean")
    if not enabled:
        return None
    if set(policy) - {"enabled", "template", "reset_clone"}:
        raise ValueError("Unknown profile_creation setting")
    template = policy.get("template")
    if not isinstance(template, str) or not template.strip():
        raise ValueError("Enabled profile_creation requires a template directory")
    if not isinstance(policy.get("reset_clone", False), bool):
        raise ValueError("profile_creation.reset_clone must be boolean")
    template = Path(template).expanduser()
    if not template.is_absolute():
        template = root / template
    return {**policy, "template": template}


def creation_resets_identity(profile_dir: Path) -> bool:
    """Read the applied decision, independent of operator policy changes after publication."""
    try:
        receipt = json.loads((profile_dir / CREATION_MARKER).read_text(encoding="utf-8-sig"))
    except FileNotFoundError:
        return False  # Legacy profiles have no creation policy receipt.
    if not isinstance(receipt, dict) or not isinstance(receipt.get("reset_identity"), bool):
        raise ValueError("Invalid profile creation receipt")
    return receipt["reset_identity"]


def _write_creation_receipt(staging: Path, policy: dict | None, profile_id: str) -> None:
    from utils import atomic_json_write

    marker = staging / CREATION_MARKER
    # A full clone may carry the source receipt, even a link. Replace the entry itself:
    # atomic_json_write otherwise preserves symlink destinations for ordinary user files.
    marker.unlink(missing_ok=True)
    atomic_json_write(marker, {
        "profile_id": profile_id,
        "policy_applied": policy is not None,
        "template": str(policy["template"]) if policy else None,
        "reset_identity": bool(policy and policy.get("reset_clone", False)),
    }, mode=0o600)


def _relative_path(value) -> Path:
    if not isinstance(value, str) or not value or "\\" in value or ":" in value:
        raise ValueError("Template resource paths must be relative paths")
    path = Path(value)
    if path.is_absolute() or any(part in {"", ".", ".."} for part in value.split("/")):
        raise ValueError("Template resource paths cannot escape their root")
    return path


def _checked_path(root: Path, relative: Path) -> Path:
    """Reject links even inside the root: a clone must never write through to its source."""
    from hermes_cli.profiles import _junction_target

    if root.is_symlink() or _junction_target(str(root)) is not None:
        raise ValueError("Template resource root cannot be a link")
    current = root
    for part in relative.parts:
        current = current / part
        if current.is_symlink() or _junction_target(str(current)) is not None:
            raise ValueError(f"Template resource path contains a link: {relative}")
    if not current.resolve().is_relative_to(root.resolve()):
        raise ValueError(f"Template resource path escapes its root: {relative}")
    return current


def _render(text: str, name: str, profile_id: str) -> str:
    rendered = text.replace("{{profile_name}}", name).replace("{{profile_id}}", profile_id)
    if "{{" in rendered or "}}" in rendered:
        raise ValueError("Unknown template variable; only {{profile_name}} and {{profile_id}} are supported")
    return rendered


def _copy_resource(source_root: Path, source_rel: Path, staging: Path, target_rel: Path,
                   *, render: bool, name: str, profile_id: str) -> None:
    source = _checked_path(source_root, source_rel)
    target = _checked_path(staging, target_rel)
    if target_rel.parts[0].casefold() in {"config.yaml", CREATION_MARKER}:
        raise ValueError("Use the config mapping for config.yaml; the creation marker is Hermes-owned")
    if source.is_dir():
        target.mkdir(parents=True, exist_ok=True, mode=0o700)
        for child in sorted(source.iterdir()):
            _copy_resource(source_root, source_rel / child.name, staging, target_rel / child.name,
                           render=render, name=name, profile_id=profile_id)
        return
    if not source.is_file():
        raise FileNotFoundError(f"Template resource missing or not a regular file: {source_rel}")
    target.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    if render:
        text = _render(source.read_text(encoding="utf-8-sig"), name, profile_id)
        target.write_text(text, encoding="utf-8")
    else:
        shutil.copyfile(source, target)
    target.chmod(stat.S_IMODE(source.stat().st_mode) & 0o700)


def _read_manifest(template: Path) -> dict:
    manifest_path = _checked_path(template, Path("manifest.yaml"))
    try:
        manifest = yaml.safe_load(manifest_path.read_text(encoding="utf-8-sig"))
    except yaml.YAMLError as exc:
        raise ValueError("Malformed profile creation manifest") from exc
    if not isinstance(manifest, dict) or set(manifest) - {"config", "resources"}:
        raise ValueError("Template manifest must contain only config and resources")
    if not isinstance(manifest.get("config", {}), dict):
        raise ValueError("Template config must be a mapping")
    if not isinstance(manifest.get("resources", []), list):
        raise ValueError("Template resources must be a list")
    return manifest


def _apply_template(staging: Path, policy: dict, *, name: str, profile_id: str, no_skills: bool) -> None:
    from hermes_cli.config import _deep_merge, atomic_config_replace, read_user_config_raw
    from hermes_cli.profile_model_seed import launch_model_seed, merge_endpoint_seed

    template = policy["template"]
    manifest = _read_manifest(template)
    config_path = _checked_path(staging, Path("config.yaml"))
    current = read_user_config_raw(config_path) if config_path.exists() else {}
    overlay = manifest.get("config", {}).copy()
    if no_skills:
        overlay.pop("skills", None)
    # A template's defaults must not change the caller's selected model/endpoint.
    seed = launch_model_seed(current)
    merged = merge_endpoint_seed(_deep_merge(current, overlay), seed)
    if "model" in seed:
        merged["model"] = seed["model"]
    atomic_config_replace(config_path, merged)
    for resource in manifest.get("resources", []):
        if not isinstance(resource, dict) or set(resource) - {"source", "target", "render"}:
            raise ValueError("Template resource must contain source, target and optional render")
        source = _relative_path(resource.get("source"))
        target = _relative_path(resource.get("target"))
        render = resource.get("render", False)
        if not isinstance(render, bool):
            raise ValueError("Template resource render must be boolean")
        if no_skills and target.parts[0] == "skills":
            continue
        _copy_resource(template, source, staging, target, render=render, name=name, profile_id=profile_id)


def _prepare_layout(staging: Path, source_dir: Path | None, *, name: str,
                    clone_all: bool, sync_imports: bool, clone_channels: bool,
                    reset: bool) -> None:
    from hermes_cli import profiles

    if reset:
        # Start fresh: never copy source credentials, private banks or symlinked state.
        profiles._bootstrap_profile_dir(staging, None)
    elif clone_all and source_dir:
        profiles._clone_all_into(source_dir, staging, name)
    else:
        profiles._bootstrap_profile_dir(staging, source_dir, sync_imports=sync_imports)
    if source_dir is not None and not clone_channels and not reset:
        from hermes_cli.profile_channels import strip_channel_settings
        stripped = strip_channel_settings(staging, include_state=clone_all, source_dir=source_dir)
        if stripped:
            profiles.logger.info("profile %s: cloned without messaging channels %s", name, stripped)


def initialize_profile_staging(staging: Path, source_dir: Path | None, *, name: str,
                               clone_all: bool, sync_imports: bool, clone_channels: bool,
                               no_skills: bool, description: str | None, soul: str | None) -> None:
    """Prepare the complete private tree; the caller owns atomic publication and cleanup."""
    from hermes_cli.config import atomic_config_replace, read_user_config_raw
    from hermes_cli.profile_model_seed import launch_model_seed
    from hermes_cli.profiles_initialization import finish_profile_layout
    from hermes_constants import (
        get_hermes_home, set_hermes_home_override, reset_hermes_home_override,
    )

    policy = load_creation_policy()
    profile_id = str(uuid4())
    reset = bool(policy and policy.get("reset_clone", False) and source_dir is not None)
    model_source = source_dir or get_hermes_home()
    # Model resolution may consult config and maintain a last-good backup. Enabled creation
    # runs in staging so those incidental reads/writes can never touch the source profile.
    token = set_hermes_home_override(staging) if policy else None
    try:
        _prepare_layout(staging, source_dir, name=name, clone_all=clone_all,
                        sync_imports=sync_imports, clone_channels=clone_channels, reset=reset)
        if policy:
            if reset or source_dir is None:
                atomic_config_replace(staging / "config.yaml",
                                      launch_model_seed(read_user_config_raw(model_source / "config.yaml")))
            _apply_template(staging, policy, name=name, profile_id=profile_id, no_skills=no_skills)
        finish_profile_layout(staging, no_skills=no_skills, clone_all=clone_all,
                              description=description, soul=soul)
        _write_creation_receipt(staging, policy, profile_id)
    finally:
        if token is not None:
            reset_hermes_home_override(token)
