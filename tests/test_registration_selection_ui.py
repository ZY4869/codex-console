from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def test_registration_page_contains_selection_strategy_controls():
    content = (ROOT / "templates" / "index.html").read_text(encoding="utf-8")
    assert "random-email-service" in content
    assert "service-options-section" in content
    assert "domain-select" in content
    assert "email-address-select" in content
    assert "subdomain-only" in content
    assert "email-stats-table" in content
    assert "refresh-email-stats-btn" in content
    assert "email-domain-stats-table" in content
    assert "refresh-email-domain-stats-btn" in content
    assert "email-domain-stats-overview" in content
    assert "registration-flow-adaptive" in content
    assert "状态纠偏协议注册（测试）" in content


def test_registration_script_contains_selection_strategy_logic():
    content = (ROOT / "static" / "js" / "app.js").read_text(encoding="utf-8")
    assert "loadRegistrationServiceOptions" in content
    assert "loadEmailRegistrationStats" in content
    assert "loadEmailDomainRegistrationStats" in content
    assert "renderEmailDomainStatsOverview" in content
    assert "random_email_service" in content
    assert "subdomain_only" in content
    assert "selected_domains" in content
    assert "selected_email_addresses" in content
    assert "add_phone_count" in content
    assert "email-domain-stats" in content
    assert "registrationFlowAdaptive" in content
    assert "protocol_variant: 'adaptive'" in content
