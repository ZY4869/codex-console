"""
Team 名称构建器。

支持将 Team 名称拆成多个词位，每个词位都可以选择随机词或自定义词。
"""

from __future__ import annotations

import random
import re
from typing import Any, Dict, List, Optional, Sequence

MAX_TEAM_NAME_LENGTH = 64
MAX_TEAM_NAME_WORD_LENGTH = 15

TEAM_NAME_WORD_BANKS: Dict[str, Sequence[str]] = {
    "prefix": (
        "Nova",
        "Atlas",
        "Lumen",
        "Orbit",
        "Signal",
        "Prism",
        "Cedar",
        "Nimbus",
        "Summit",
        "Cobalt",
        "Velvet",
        "Solar",
        "Echo",
        "Turbo",
        "Pixel",
        "Astra",
    ),
    "core": (
        "Forge",
        "Circuit",
        "Harbor",
        "Studio",
        "Beacon",
        "Horizon",
        "Canvas",
        "Bridge",
        "Anchor",
        "Vector",
        "Sprint",
        "Pilot",
        "Nest",
        "Ledger",
        "Matrix",
        "Engine",
    ),
    "suffix": (
        "Lab",
        "Works",
        "Cloud",
        "Hub",
        "Guild",
        "Stack",
        "Flow",
        "Grid",
        "Point",
        "Base",
        "Crew",
        "Field",
        "Scope",
        "Wave",
        "Pulse",
        "Space",
    ),
    "collective": (
        "Prime",
        "One",
        "Core",
        "Deck",
        "Loop",
        "Line",
        "Node",
        "Nest",
        "Dock",
        "Ring",
        "Peak",
        "Sync",
    ),
}

DEFAULT_TEAM_NAME_BUCKETS = ("prefix", "core", "suffix")


def normalize_team_name_word(value: Any) -> str:
    text = str(value or "").strip()
    if not text:
        return ""
    text = re.sub(r"\s+", "-", text)
    text = re.sub(r"[^\w\u4e00-\u9fff-]+", "", text, flags=re.UNICODE)
    text = text.strip("-_")
    return text[:MAX_TEAM_NAME_WORD_LENGTH]


def normalize_team_workspace_name(value: Any) -> str:
    text = str(value or "").strip()
    if not text:
        return "MyTeam"
    text = re.sub(r"\s+", " ", text)
    parts = [normalize_team_name_word(item) for item in text.split(" ")]
    normalized = " ".join(item for item in parts if item).strip()
    return normalized[:MAX_TEAM_NAME_LENGTH].strip() or "MyTeam"


def build_team_workspace_name(
    base_name: Optional[str] = None,
    parts: Optional[Sequence[Dict[str, Any]]] = None,
    *,
    rng: Optional[random.Random] = None,
) -> str:
    if not parts:
        return normalize_team_workspace_name(base_name)

    resolved_words: List[str] = []
    chooser = rng.choice if rng else random.choice

    for index, raw_part in enumerate(parts):
        bucket = str((raw_part or {}).get("bucket") or _get_bucket_for_index(index))
        mode = str((raw_part or {}).get("mode") or "random").strip().lower()
        custom_value = normalize_team_name_word((raw_part or {}).get("value"))
        if mode == "custom" and custom_value:
            resolved_words.append(custom_value)
            continue

        candidates = TEAM_NAME_WORD_BANKS.get(bucket) or TEAM_NAME_WORD_BANKS[_get_bucket_for_index(index)]
        resolved_words.append(str(chooser(list(candidates))))

    name = " ".join(item for item in resolved_words if item).strip()
    return normalize_team_workspace_name(name or base_name)


def build_default_team_name_parts(
    word_count: int = 3,
    *,
    rng: Optional[random.Random] = None,
) -> List[Dict[str, str]]:
    chooser = rng.choice if rng else random.choice
    count = min(max(int(word_count or 3), 2), 4)
    parts: List[Dict[str, str]] = []
    for index in range(count):
        bucket = _get_bucket_for_index(index)
        parts.append(
            {
                "bucket": bucket,
                "mode": "random",
                "value": str(chooser(list(TEAM_NAME_WORD_BANKS[bucket]))),
            }
        )
    return parts


def _get_bucket_for_index(index: int) -> str:
    buckets = list(DEFAULT_TEAM_NAME_BUCKETS) + ["collective"]
    if 0 <= index < len(buckets):
        return buckets[index]
    return buckets[-1]
