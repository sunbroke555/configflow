"""Standalone, fixed-layout Mihomo + OxiDNS core installer."""
from pathlib import Path
from backend.agents.install_output import with_install_output


def generate_core_install_script() -> str:
    """Return the core-only installer; no Agent identity or access token is used."""
    return with_install_output((Path(__file__).parent / "scripts" / "install-cores.sh").read_text(encoding="utf-8"))
