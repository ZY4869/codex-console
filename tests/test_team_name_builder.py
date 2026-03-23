from src.core.team_name_builder import (
    build_default_team_name_parts,
    build_team_workspace_name,
    normalize_team_name_word,
    normalize_team_workspace_name,
)


class FakeRng:
    def __init__(self, picks):
        self._picks = list(picks)

    def choice(self, items):
        value = self._picks.pop(0)
        assert value in items
        return value


def test_normalize_team_name_word_collapses_whitespace_and_symbols():
    assert normalize_team_name_word("  Alpha Team!!!  ") == "Alpha-Team"


def test_normalize_team_workspace_name_defaults_when_empty():
    assert normalize_team_workspace_name("   ") == "MyTeam"


def test_build_team_workspace_name_supports_custom_and_random_parts():
    rng = FakeRng(["Nova", "Lab"])
    result = build_team_workspace_name(
        "Fallback",
        parts=[
            {"bucket": "prefix", "mode": "random"},
            {"bucket": "core", "mode": "custom", "value": "Signal"},
            {"bucket": "suffix", "mode": "random"},
        ],
        rng=rng,
    )
    assert result == "Nova Signal Lab"


def test_build_default_team_name_parts_uses_requested_word_count():
    rng = FakeRng(["Atlas", "Forge", "Hub", "Prime"])
    parts = build_default_team_name_parts(4, rng=rng)
    assert [item["bucket"] for item in parts] == ["prefix", "core", "suffix", "collective"]
    assert [item["value"] for item in parts] == ["Atlas", "Forge", "Hub", "Prime"]
