from pathlib import Path


def _agents_source() -> str:
    return (Path(__file__).resolve().parents[1] / "frontend" / "src" / "views" / "Agents.vue").read_text(encoding="utf-8")


def test_agent_profile_binding_uses_visible_native_select():
    source = _agents_source()
    marker = "配置 Profile"
    section = source[source.index(marker):source.index(marker) + 900]

    assert "<select" in section
    assert 'class="agent-profile-native-select"' in section
    assert ':value="agent.profile_id || \'default\'"' in section
    assert '@change="handleAgentProfileChange(agent, $event)"' in section
    assert "<el-select" not in section
    assert "<option" in section
    assert "profile.name" in section
    assert ".agent-profile-native-select {" in source
    assert "-webkit-text-fill-color: #30354d;" in source
    assert "color-scheme: light;" in source


def test_agent_profile_native_change_forwards_selected_profile_id():
    source = _agents_source()
    start = source.index("const handleAgentProfileChange")
    handler = source[start:start + 400]

    assert "event.target" in handler
    assert "select instanceof HTMLSelectElement" in handler
    assert "const previousProfileId = agent.profile_id || 'default'" in handler
    assert "await bindAgentProfile(agent, select.value)" in handler
    assert "select.value = previousProfileId" in handler
    assert "agent-profile-select-popper" not in source
