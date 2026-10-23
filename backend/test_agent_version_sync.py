"""The server-side latest Agent version must match the version the Go Agent reports."""
from pathlib import Path
import re

from backend.agents.version import LATEST_AGENT_VERSION, VERSION_CHANGELOG


MIGRATION_GO = Path(__file__).resolve().parent / "agents/go-agent/upgrade/migration.go"


def test_go_agent_version_matches_latest_agent_version():
    # has_update() compares the reported version with LATEST_AGENT_VERSION, so a
    # bump on only one side either hides an update or offers it forever.
    versions = re.findall(r'^const Version = "([^"]+)"$', MIGRATION_GO.read_text(), re.MULTILINE)
    assert versions == [LATEST_AGENT_VERSION]


def test_latest_agent_version_has_changelog_entry():
    entry = VERSION_CHANGELOG[LATEST_AGENT_VERSION]
    assert re.fullmatch(r"\d{4}-\d{2}-\d{2}", entry["date"])
    assert entry["features"] and all(isinstance(item, str) and item for item in entry["features"])
    assert next(iter(VERSION_CHANGELOG)) == LATEST_AGENT_VERSION
