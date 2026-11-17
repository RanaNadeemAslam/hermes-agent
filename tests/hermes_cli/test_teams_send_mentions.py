"""Native Teams mentions through the real CLI, plugin registry and Bot Framework sender.

The transport is a local fake: no real login, roster request or message post occurs.
"""
import argparse
import asyncio
import json
from types import SimpleNamespace

import aiohttp
import pytest

from gateway.config import Platform, PlatformConfig
from hermes_cli import send_cmd
from hermes_cli.plugins import discover_plugins

USER = "11111111-2222-4333-8444-555555555555"
CHAT = "19:mention-test@thread.v2"


@pytest.fixture
def teams_transport(monkeypatch):
    state = SimpleNamespace(
        calls=[], members=[{"id": "29:alice", "aadObjectId": USER, "name": "Alice & Bob"}],
        roster_status=200, post_status=200, fail_roster=False, fail_post=False, fail_at=None,
        fail_token_at=None, activity_response=None,
    )

    class Response:
        def __init__(self, status, payload):
            self.status, self.payload = status, payload

        async def __aenter__(self):
            return self

        async def __aexit__(self, *_args):
            return False

        async def json(self):
            return self.payload

        async def text(self):
            return "transport failure"

    class Session:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *_args):
            return False

        def post(self, url, **kwargs):
            state.calls.append(("POST", url, kwargs))
            if "oauth2" in url:
                number = sum("oauth2" in u for _, u, _ in state.calls)
                if state.fail_token_at == number:
                    return Response(500, {})
                return Response(200, {"access_token": "fake-bot-token"})
            number = sum(u.endswith("/activities") for _, u, _ in state.calls)
            if state.fail_post or state.fail_at == number:
                raise TimeoutError("activity outcome unknown")
            payload = state.activity_response if state.activity_response is not None else {"id": f"posted-{number}"}
            return Response(state.post_status, payload)

        def get(self, url, **kwargs):
            state.calls.append(("GET", url, kwargs))
            if state.fail_roster:
                raise TimeoutError("roster timed out")
            return Response(state.roster_status, state.members)

    from hermes_cli.plugins import PluginContext, PluginManager, PluginManifest
    from plugins.platforms.teams.adapter import register

    discover_plugins()
    register(PluginContext(PluginManifest(name="teams", source="bundled"), PluginManager()))
    cfg = SimpleNamespace(
        platforms={Platform("teams"): PlatformConfig(enabled=True, extra={
            "client_id": "bot-app-id", "client_secret": "fake-secret", "tenant_id": "tenant",
        })}, get_home_channel=lambda _platform: None,
    )
    monkeypatch.setattr(send_cmd, "_load_hermes_env", lambda: None)
    monkeypatch.setattr("gateway.config.load_gateway_config", lambda: cfg)
    monkeypatch.setattr("tools.interrupt.is_interrupted", lambda: False)
    monkeypatch.setattr("model_tools._run_async", lambda coro: asyncio.run(coro))
    monkeypatch.setattr("gateway.relay.egress._live_relay_fronted", lambda: set())
    monkeypatch.setattr("gateway.outbound.send._mirror_sent_message", lambda *_args: False)
    monkeypatch.setattr("gateway.channel_directory.load_directory", lambda: {
        "platforms": {"teams": [{"id": CHAT, "name": "Test room", "type": "group"}]},
    })
    monkeypatch.setattr(aiohttp, "ClientSession", lambda **_kwargs: Session())
    return state


def send(argv):
    parser = argparse.ArgumentParser()
    send_cmd.register_send_subparser(parser.add_subparsers())
    with pytest.raises(SystemExit) as exc:
        send_cmd.cmd_send(parser.parse_args(["send", *argv]))
    return exc.value.code


def test_cli_posts_native_mention_resolved_from_exact_room(teams_transport):
    assert send(["--to", f"teams:{CHAT}", "--mention", USER, "Approved reply."]) == 0
    calls = teams_transport.calls
    assert [method for method, _, _ in calls] == ["POST", "GET", "POST"]
    assert calls[1][1].endswith(f"/v3/conversations/{CHAT}/members")
    assert calls[2][1].endswith(f"/v3/conversations/{CHAT}/activities")
    assert calls[2][2]["headers"]["Authorization"] == "Bearer fake-bot-token"
    activity = calls[2][2]["json"]
    assert activity["text"] == "<at>Alice &amp; Bob</at> Approved reply."
    assert activity["entities"] == [{"type": "mention", "text": "<at>Alice &amp; Bob</at>",
                                      "mentioned": {"id": "29:alice", "name": "Alice & Bob"}}]
    assert activity["textFormat"] == "markdown"


def test_mentions_with_media_are_rejected_before_network(teams_transport, tmp_path):
    image = tmp_path / "photo.png"
    image.write_bytes(b"\x89PNG\r\n\x1a\n")
    assert send(["--to", f"teams:{CHAT}", "--mention", USER, f"Approved reply. MEDIA:{image}"]) != 0
    assert teams_transport.calls == []


@pytest.mark.parametrize("target", ["Alice", "{" + USER + "}", USER.replace("-", ""),
                                    "urn:uuid:" + USER, "00000000-0000-0000-0000-000000000000"])
def test_malformed_ids_never_reach_network(teams_transport, target):
    assert send(["--to", f"teams:{CHAT}", "--mention", target, "Reply."]) != 0
    assert teams_transport.calls == []


@pytest.mark.parametrize("members", [[], [{"aadObjectId": USER, "name": "Alice"}],
    [{"aadObjectId": USER, "id": "28:bot", "name": "Bot"}],
    [{"aadObjectId": USER, "id": "29:alice", "name": ""}],
    [{"aadObjectId": USER, "id": "29:a", "name": "Alice"}, {"aadObjectId": USER, "id": "29:b", "name": "Alice"}],
    {"error": "not a roster"},
])
def test_missing_ambiguous_or_unusable_members_never_post(teams_transport, members):
    teams_transport.members = members
    assert send(["--to", f"teams:{CHAT}", "--mention", USER, "Reply."]) != 0
    assert [method for method, _, _ in teams_transport.calls] == ["POST", "GET"]


@pytest.mark.parametrize("status", [401, 403, 404, 500])
def test_failed_roster_lookup_never_posts(teams_transport, status):
    teams_transport.roster_status = status
    assert send(["--to", f"teams:{CHAT}", "--mention", USER, "Reply."]) != 0
    assert [method for method, _, _ in teams_transport.calls] == ["POST", "GET"]


def test_mention_entities_are_only_on_first_chunk(teams_transport):
    assert send(["--to", f"teams:{CHAT}", "--mention", USER, "word " * 7000]) == 0
    activities = [kw["json"] for _, url, kw in teams_transport.calls if url.endswith("/activities")]
    assert len(activities) > 1
    assert activities[0]["entities"][0]["mentioned"]["id"] == "29:alice"
    assert all("entities" not in a for a in activities[1:])
    assert all(len(a["text"].encode("utf-8")) <= 28000 for a in activities)


def test_uncertain_post_is_not_retried(teams_transport, capsys):
    teams_transport.fail_post = True
    assert send(["--to", f"teams:{CHAT}", "--mention", USER, "Reply."]) != 0
    assert sum(url.endswith("/activities") for _, url, _ in teams_transport.calls) == 1


def test_duplicate_targets_are_deduplicated(teams_transport):
    assert send(["--to", f"teams:{CHAT}", "--mention", USER, "--mention", USER.upper(), "Reply."]) == 0
    activity = teams_transport.calls[-1][2]["json"]
    assert len(activity["entities"]) == 1


def test_all_targets_are_checked_before_posting(teams_transport):
    missing = "aaaaaaaa-bbbb-4ccc-8ddd-eeeeeeeeeeee"
    assert send(["--to", f"teams:{CHAT}", "--mention", USER, "--mention", missing, "Reply."]) != 0
    assert not any(url.endswith("/activities") for _, url, _ in teams_transport.calls)


def test_missing_message_id_is_not_success(teams_transport):
    teams_transport.activity_response = {}
    assert send(["--to", f"teams:{CHAT}", "--mention", USER, "Reply."]) != 0
    assert sum(url.endswith("/activities") for _, url, _ in teams_transport.calls) == 1


def test_roster_timeout_is_known_not_sent(teams_transport, capsys):
    teams_transport.fail_roster = True
    assert send(["--json", "--to", f"teams:{CHAT}", "--mention", USER, "Reply."]) != 0
    result = json.loads(capsys.readouterr().out)
    assert result["retryable"] is True
    assert not any(url.endswith("/activities") for _, url, _ in teams_transport.calls)


def test_untagged_send_keeps_plain_activity(teams_transport):
    assert send(["--to", f"teams:{CHAT}", "Reply."]) == 0
    assert [method for method, _, _ in teams_transport.calls] == ["POST", "POST"]
    assert teams_transport.calls[-1][2]["json"] == {"type": "message", "text": "Reply.", "textFormat": "markdown"}


def test_multichunk_result_identifies_the_tagged_message(teams_transport, capsys):
    assert send(["--json", "--to", f"teams:{CHAT}", "--mention", USER, "word " * 7000]) == 0
    result = json.loads(capsys.readouterr().out)
    assert result["mention_message_id"] == "posted-1"
    activities = [kw["json"] for _, url, kw in teams_transport.calls if url.endswith("/activities")]
    assert result["message_ids"] == [f"posted-{i}" for i in range(1, len(activities) + 1)]
    assert all(len(json.dumps(a).encode("utf-8")) <= 27000 for a in activities)


def test_partial_delivery_retains_tagged_id_and_forbids_retry(teams_transport, capsys):
    teams_transport.fail_at = 2
    assert send(["--json", "--to", f"teams:{CHAT}", "--mention", USER, "word " * 5500]) != 0
    result = json.loads(capsys.readouterr().out)
    assert result["partial"] is True
    assert result["mention_message_id"] == "posted-1"
    assert result["message_ids"] == ["posted-1"]
    assert not result.get("retryable")
    assert "do not retry" in result["error"]
    assert sum(url.endswith("/activities") for _, url, _ in teams_transport.calls) == 2


def test_unicode_chunks_preserve_text_and_full_serialized_budget():
    from plugins.platforms.teams.mentions import activity_chunks, mention_activity
    members = [{"id": "29:alice", "aadObjectId": USER, "name": "<_*Alice&>"}]
    activity = mention_activity("é漢🙂" * 10000, [USER], members)
    chunks = activity_chunks(activity)
    assert "".join(c["text"] for c in chunks) == activity["text"]
    assert all(len(json.dumps(c).encode("utf-8")) <= 27000 for c in chunks)
    assert chunks[0]["entities"] == activity["entities"]
    assert all("entities" not in c for c in chunks[1:])


def test_second_outer_chunk_token_failure_is_partial_not_retryable(teams_transport, capsys):
    teams_transport.fail_token_at = 2
    assert send(["--json", "--to", f"teams:{CHAT}", "--mention", USER, "word " * 12000]) != 0
    result = json.loads(capsys.readouterr().out)
    assert result["delivery"] == "partial"
    assert result["mention_message_id"] == "posted-1"
    assert not result.get("retryable")
    assert sum("oauth2" in url for _, url, _ in teams_transport.calls) == 2


@pytest.mark.parametrize("allowed", [True, False])
def test_relay_attestation_is_preserved_for_tagged_sends(teams_transport, monkeypatch, allowed):
    monkeypatch.setattr("gateway.relay.egress._live_relay_fronted", lambda: {"teams"})
    monkeypatch.setattr("gateway.relay.egress.attested_relay_targets", lambda _name: {CHAT} if allowed else set())
    result = send(["--to", f"teams:{CHAT}", "--mention", USER, "Reply."])
    assert (result == 0) == allowed
    if allowed:
        assert teams_transport.calls[-1][2]["json"]["entities"][0]["mentioned"]["id"] == "29:alice"
    else:
        assert teams_transport.calls == []
