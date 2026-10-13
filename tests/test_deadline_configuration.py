import pytest
from aos_bridge.settings import Settings as BridgeSettings
from aos_mcp.settings import Settings as McpSettings


def test_device_deadlines_cover_the_cec_command_budget() -> None:
    assert 18 < BridgeSettings().cec_timeout_s < McpSettings().timeout_s <= 30


def test_device_deadlines_are_runtime_configurable(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("AOS_CEC_TIMEOUT_S", "24")
    monkeypatch.setenv("AOS_MCP_TIMEOUT_S", "28")
    assert BridgeSettings.from_env().cec_timeout_s == 24
    assert McpSettings.from_env().timeout_s == 28


@pytest.mark.parametrize("value", ["0", "-1", "31", "nan", "inf", "invalid"])
def test_device_deadlines_reject_invalid_values(
    monkeypatch: pytest.MonkeyPatch, value: str
) -> None:
    monkeypatch.setenv("AOS_CEC_TIMEOUT_S", value)
    monkeypatch.setenv("AOS_MCP_TIMEOUT_S", value)
    with pytest.raises(ValueError):
        BridgeSettings.from_env()
    with pytest.raises(ValueError):
        McpSettings.from_env()
