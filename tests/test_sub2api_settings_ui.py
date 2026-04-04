from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def test_settings_page_contains_sub2api_group_fields():
    content = (ROOT / "templates" / "settings.html").read_text(encoding="utf-8")
    assert "same-email-retry-limit" in content
    assert "email-prefix-alnum-only" in content
    assert "sub2api-service-default-group-ids" in content
    assert "sub2api-service-load-groups-btn" in content
    assert "sub2api-service-groups-list" in content


def test_settings_script_contains_sub2api_group_logic():
    content = (ROOT / "static" / "js" / "settings.js").read_text(encoding="utf-8")
    assert "same_email_retry_limit" in content
    assert "email_prefix_alnum_only" in content
    assert "default_group_ids" in content
    assert "/sub2api-services/fetch-groups" in content
    assert "loadSub2ApiGroupsForModal" in content
