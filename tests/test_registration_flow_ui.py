from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def test_registration_page_contains_flow_selector():
    content = (ROOT / "templates" / "index.html").read_text(encoding="utf-8")
    assert "registration-flow-group" in content
    assert "registration-flow-legacy" in content
    assert "registration-flow-enhanced" in content
    assert "registration-flow-adaptive" in content
    assert "registration-flow-browser" in content
    assert "registration-flow-outlook-note" in content
    assert "状态纠偏协议注册（测试）" in content


def test_registration_script_contains_flow_selector_logic():
    content = (ROOT / "static" / "js" / "app.js").read_text(encoding="utf-8")
    assert "protocol_variant" in content
    assert "registration_mode" in content
    assert "getRegistrationFlowDisplayName" in content
    assert "setRegistrationFlowSelection" in content
    assert "syncRegistrationFlowVisibility" in content
    assert "registrationFlowAdaptive" in content
    assert "protocol_variant: 'adaptive'" in content
    assert "状态纠偏协议注册（测试）" in content
