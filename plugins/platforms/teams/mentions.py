"""Fail-closed native mentions, resolved against the destination's Bot Framework roster."""
from __future__ import annotations

import json
import re
from html import escape

_AAD_ID = re.compile(r"[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}")


def mention_ids(values: list[str]) -> list[str]:
    """Accept exact hyphenated AAD object IDs, not display names or repaired identifiers."""
    ids = []
    for value in values:
        if not isinstance(value, str) or not _AAD_ID.fullmatch(value) or int(value.replace("-", ""), 16) == 0:
            raise ValueError("Teams mentions require nonzero hyphenated AAD object IDs")
        if value.lower() not in ids:
            ids.append(value.lower())
    return ids


def mention_activity(message: str, ids: list[str], members: list[dict]) -> dict:
    """Build matching <at> text and entities; never guess a member from a display name."""
    if not isinstance(members, list):
        raise ValueError("Teams member response is not a complete member list")
    entities = []
    for aad_id in ids:
        matches = [m for m in members if isinstance(m, dict) and str(m.get("aadObjectId", "")).lower() == aad_id]
        if len(matches) != 1:
            raise ValueError("Teams mention target is missing or ambiguous in the destination roster")
        member = matches[0]
        member_id, name = member.get("id"), member.get("name")
        if not isinstance(member_id, str) or not member_id.startswith("29:") or not isinstance(name, str) or not name.strip():
            raise ValueError("Teams mention target lacks a usable human member ID or display name")
        tag = f"<at>{escape(name, quote=False)}</at>"
        entities.append({"type": "mention", "text": tag, "mentioned": {"id": member_id, "name": name}})
    prefix = " ".join(e["text"] for e in entities) + " "
    activity = {"type": "message", "text": prefix, "textFormat": "markdown", "entities": entities}
    if len(json.dumps(activity).encode("utf-8")) >= 27000:
        raise ValueError("Teams mention names exceed the message limit")
    activity["text"] += message
    return activity


def activity_chunks(activity: dict) -> list[dict]:
    """Keep UTF-8 characters intact and attach notification entities to the first chunk only."""
    remaining = activity["text"]
    chunks = []
    while remaining:
        item = {"type": "message", "text": "", "textFormat": "markdown"}
        if not chunks and activity.get("entities"):
            item["entities"] = activity["entities"]
        # Account for the full default JSON serialization (including escaped Unicode,
        # mention entities and duplicated display names), with 1 KB of headroom.
        low, high = 0, min(len(remaining), 28000)
        while low < high:
            middle = (low + high + 1) // 2
            item["text"] = remaining[:middle]
            if len(json.dumps(item).encode("utf-8")) <= 27000:
                low = middle
            else:
                high = middle - 1
        if not low:
            raise ValueError("Teams mention metadata exceeds the message limit")
        item["text"] = remaining[:low]
        chunks.append(item)
        remaining = remaining[low:]
    return chunks or [activity]
