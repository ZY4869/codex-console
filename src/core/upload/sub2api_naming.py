"""
Sub2API dynamic naming helpers shared by upload, export, and previews.
"""

from __future__ import annotations

import re
from typing import List, Optional, Set

from .sub2api_groups import list_sub2api_group_account_names

SUB2API_IDENTITY_LABELS = ("Free", "Team", "Plus", "Pro")
_GROUP_IDENTITY_PATTERN = re.compile(r"(enterprise|team|plus|pro|free|免费)", re.IGNORECASE)


def normalize_sub2api_identity(value: Optional[str]) -> str:
    normalized = str(value or "").strip().lower()
    if normalized in {"team", "enterprise"}:
        return "Team"
    if normalized == "plus":
        return "Plus"
    if normalized == "pro":
        return "Pro"
    return "Free"


def extract_sub2api_group_identities(group_name: Optional[str]) -> List[str]:
    identities: List[str] = []
    seen = set()
    for token in _GROUP_IDENTITY_PATTERN.findall(str(group_name or "")):
        label = normalize_sub2api_identity(token)
        if label in seen:
            continue
        seen.add(label)
        identities.append(label)
    return identities


def get_sub2api_preview_identities(group_name: Optional[str]) -> List[str]:
    matched = extract_sub2api_group_identities(group_name)
    return matched if matched else list(SUB2API_IDENTITY_LABELS)


def resolve_sub2api_group_naming_identity(group_name: Optional[str], account_identity: Optional[str]) -> str:
    matched = extract_sub2api_group_identities(group_name)
    if len(matched) == 1:
        return matched[0]
    return normalize_sub2api_identity(account_identity)


def build_sub2api_dynamic_name(identity: Optional[str], index: int, digits: int) -> str:
    normalized_identity = normalize_sub2api_identity(identity)
    normalized_digits = max(1, int(digits or 1))
    return f"GPT-{normalized_identity}-{int(index):0{normalized_digits}d}"


def build_sub2api_name_pattern(identity: Optional[str], digits: int) -> re.Pattern[str]:
    normalized_identity = normalize_sub2api_identity(identity)
    normalized_digits = max(1, int(digits or 1))
    return re.compile(rf"^GPT-{re.escape(normalized_identity)}-(\d{{{normalized_digits},}})$")


def parse_sub2api_name_index(name: str, identity: Optional[str], digits: int) -> Optional[int]:
    match = build_sub2api_name_pattern(identity, digits).match(str(name or "").strip())
    if not match:
        return None
    try:
        index = int(match.group(1))
    except (TypeError, ValueError):
        return None
    return index if index > 0 else None


def discover_sub2api_identity_occupied_name_indices(
    api_url: str,
    api_key: str,
    group_id: int,
    identity: Optional[str],
    digits: int,
    platform: str = "openai",
) -> Set[int]:
    occupied: Set[int] = set()
    for name in list_sub2api_group_account_names(api_url, api_key, group_id, platform=platform):
        index = parse_sub2api_name_index(name, identity, digits)
        if index:
            occupied.add(index)
    return occupied


def reserve_smallest_available_indices(occupied_indices: Set[int], count: int) -> List[int]:
    reserved: List[int] = []
    taken = {int(index) for index in occupied_indices if int(index) > 0}
    candidate = 1
    while len(reserved) < max(0, int(count or 0)):
        if candidate not in taken:
            reserved.append(candidate)
            taken.add(candidate)
        candidate += 1
    return reserved


def next_available_index(occupied_indices: Set[int]) -> int:
    candidate = 1
    while candidate in occupied_indices:
        candidate += 1
    return candidate
