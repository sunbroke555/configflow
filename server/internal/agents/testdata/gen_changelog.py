import sys, json
sys.path.insert(0, sys.argv[1])
from backend.agents.version import VERSION_CHANGELOG, LATEST_AGENT_VERSION
keys = set()
for v in VERSION_CHANGELOG.values():
    keys |= set(v)
print(keys, len(VERSION_CHANGELOG), LATEST_AGENT_VERSION, file=sys.stderr)
out = []
out.append("// Code generated from backend/agents/version.py by a one-off script; DO NOT EDIT.\n")
out.append("package agents\n")
out.append("// LatestAgentVersion is LATEST_AGENT_VERSION.")
out.append("const LatestAgentVersion = %s\n" % json.dumps(LATEST_AGENT_VERSION))
out.append("// versionChangelog is VERSION_CHANGELOG in its declaration order (newest first).")
out.append("var versionChangelog = []ChangelogEntry{")
for version, entry in VERSION_CHANGELOG.items():
    assert set(entry) <= {'date', 'features'}, entry
    feats = ", ".join(json.dumps(f, ensure_ascii=False) for f in entry['features'])
    out.append("\t{Version: %s, Date: %s, Features: []string{%s}}," % (json.dumps(version), json.dumps(entry['date']), feats))
out.append("}")
print("\n".join(out))
