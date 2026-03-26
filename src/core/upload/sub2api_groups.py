"""
Sub2API group and post-import binding helpers.
"""

from __future__ import annotations

import re
from typing import Any, Dict, Iterable, List, Set

from curl_cffi import requests as cffi_requests


def _headers(api_key: str) -> Dict[str, str]:
    return {
        "Content-Type": "application/json",
        "x-api-key": api_key,
    }


def _parse_response(response) -> Any:
    status_code = getattr(response, "status_code", None)
    if status_code not in (200, 201):
        detail = f"HTTP {status_code}"
        try:
            payload = response.json()
            if isinstance(payload, dict) and payload.get("message"):
                detail = payload["message"]
        except Exception:
            text = getattr(response, "text", "") or ""
            if text:
                detail = f"{detail} - {text[:200]}"
        raise RuntimeError(detail)

    try:
        payload = response.json()
    except Exception as exc:
        raise RuntimeError(f"Sub2API response is not valid JSON: {exc}") from exc

    if not isinstance(payload, dict):
        return payload

    code = payload.get("code", 0)
    if code not in (0, 200):
        raise RuntimeError(payload.get("message") or f"Sub2API returned error code {code}")

    return payload.get("data")


def _coerce_int(value: Any) -> int | None:
    try:
        number = int(value)
    except (TypeError, ValueError):
        return None
    return number if number > 0 else None


def fetch_sub2api_groups(api_url: str, api_key: str, platform: str = "openai") -> List[Dict[str, Any]]:
    response = cffi_requests.get(
        api_url.rstrip("/") + "/api/v1/admin/groups/all",
        headers=_headers(api_key),
        params={"platform": platform},
        proxies=None,
        timeout=15,
        impersonate="chrome110",
    )
    data = _parse_response(response)
    groups: List[Dict[str, Any]] = []
    for item in data or []:
        if not isinstance(item, dict):
            continue
        group_id = _coerce_int(item.get("id"))
        if not group_id:
            continue
        groups.append(
            {
                "id": group_id,
                "name": str(item.get("name") or f"Group {group_id}"),
                "platform": item.get("platform") or platform,
                "status": item.get("status") or "active",
                "subscription_type": item.get("subscription_type"),
                "rate_multiplier": item.get("rate_multiplier"),
                "account_count": item.get("account_count"),
                "active_account_count": item.get("active_account_count"),
                "rate_limited_account_count": item.get("rate_limited_account_count"),
            }
        )
    return groups


def find_sub2api_account_ids_by_names(
    api_url: str,
    api_key: str,
    names: Iterable[str],
    platform: str = "openai",
) -> Dict[str, int]:
    results: Dict[str, int] = {}
    for raw_name in names:
        name = str(raw_name or "").strip()
        if not name:
            continue

        response = cffi_requests.get(
            api_url.rstrip("/") + "/api/v1/admin/accounts",
            headers=_headers(api_key),
            params={
                "page": 1,
                "page_size": 100,
                "platform": platform,
                "search": name,
            },
            proxies=None,
            timeout=15,
            impersonate="chrome110",
        )
        data = _parse_response(response)
        items = data.get("items") if isinstance(data, dict) else []

        best_id = None
        for item in items or []:
            if not isinstance(item, dict):
                continue
            if str(item.get("name") or "") != name:
                continue
            candidate = _coerce_int(item.get("id"))
            if candidate and (best_id is None or candidate > best_id):
                best_id = candidate

        if best_id is not None:
            results[name] = best_id

    return results


def search_sub2api_accounts(
    api_url: str,
    api_key: str,
    search: str,
    platform: str = "openai",
    page_size: int = 100,
) -> List[Dict[str, Any]]:
    term = str(search or "").strip()
    if not term:
        return []

    response = cffi_requests.get(
        api_url.rstrip("/") + "/api/v1/admin/accounts",
        headers=_headers(api_key),
        params={
            "page": 1,
            "page_size": page_size,
            "platform": platform,
            "search": term,
        },
        proxies=None,
        timeout=15,
        impersonate="chrome110",
    )
    data = _parse_response(response)
    if not isinstance(data, dict):
        return []
    return [item for item in (data.get("items") or []) if isinstance(item, dict)]


def bind_sub2api_accounts_to_groups(
    api_url: str,
    api_key: str,
    account_ids: Iterable[int],
    group_ids: Iterable[int],
) -> Any:
    response = cffi_requests.post(
        api_url.rstrip("/") + "/api/v1/admin/accounts/bulk-update",
        headers=_headers(api_key),
        json={
            "account_ids": [int(account_id) for account_id in account_ids],
            "group_ids": [int(group_id) for group_id in group_ids],
        },
        proxies=None,
        timeout=30,
        impersonate="chrome110",
    )
    return _parse_response(response)


def list_sub2api_group_account_names(
    api_url: str,
    api_key: str,
    group_id: int,
    platform: str = "openai",
    page_size: int = 200,
) -> List[str]:
    names: List[str] = []
    page = 1
    total_pages = 1

    while page <= total_pages:
        response = cffi_requests.get(
            api_url.rstrip("/") + "/api/v1/admin/accounts",
            headers=_headers(api_key),
            params={
                "page": page,
                "page_size": page_size,
                "platform": platform,
                "group_id": int(group_id),
            },
            proxies=None,
            timeout=15,
            impersonate="chrome110",
        )
        data = _parse_response(response)
        if not isinstance(data, dict):
            break

        items = data.get("items") or []
        for item in items:
            if not isinstance(item, dict):
                continue
            name = str(item.get("name") or "").strip()
            if name:
                names.append(name)

        try:
            total_pages = max(1, int(data.get("pages") or 1))
        except (TypeError, ValueError):
            total_pages = page if len(items) < page_size else page + 1
        if len(items) < page_size and "pages" not in data:
            break
        page += 1

    return names


def discover_sub2api_occupied_name_indices(
    api_url: str,
    api_key: str,
    group_id: int,
    template_config: Dict[str, Any],
    platform: str = "openai",
) -> Set[int]:
    prefix = str(template_config.get("name_prefix") or "")
    min_digits = max(1, int(template_config.get("name_digits") or 1))
    pattern = re.compile(rf"^{re.escape(prefix)}(\d{{{min_digits},}})$")
    occupied: Set[int] = set()

    for name in list_sub2api_group_account_names(api_url, api_key, group_id, platform=platform):
        match = pattern.match(name)
        if not match:
            continue
        try:
            index = int(match.group(1))
        except (TypeError, ValueError):
            continue
        if index > 0:
            occupied.add(index)

    return occupied


def discover_sub2api_next_name_index(
    api_url: str,
    api_key: str,
    group_id: int,
    template_config: Dict[str, Any],
    platform: str = "openai",
) -> int | None:
    max_index = 0
    for index in discover_sub2api_occupied_name_indices(
        api_url,
        api_key,
        group_id,
        template_config,
        platform=platform,
    ):
        max_index = max(max_index, index)
    return max_index + 1 if max_index > 0 else None
