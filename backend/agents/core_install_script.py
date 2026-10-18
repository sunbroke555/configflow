"""Standalone, fixed-layout Mihomo + OxiDNS core installer."""
from pathlib import Path


def generate_core_install_script() -> str:
    """Return the core-only installer; no Agent identity or access token is used."""
    return (Path(__file__).parent / "scripts" / "install-cores.sh").read_text(encoding="utf-8")
