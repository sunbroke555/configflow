import sys, json
sys.path.insert(0, sys.argv[1])
from backend.agents import deployment_messages as d, upgrade_messages as u
def items(name, mapping):
    out = ["var %s = []messagePair{" % name]
    for k, v in mapping.items():
        out.append("\t{%s, %s}," % (json.dumps(k, ensure_ascii=False), json.dumps(v, ensure_ascii=False)))
    out.append("}\n")
    return "\n".join(out)
print("// Code generated from backend/agents/deployment_messages.py and upgrade_messages.py by a one-off script; DO NOT EDIT.\n\npackage agents\n")
print("type messagePair struct{ key, value string }\n")
print(items("deploymentMessages", d._MESSAGES))
print(items("deploymentPrefixes", d._PREFIXES))
print(items("upgradeMessages", u._MESSAGES))
print(items("upgradeCausePrefixes", u._CAUSE_PREFIXES))
print(items("upgradePathPrefixes", u._PATH_PREFIXES))
print(items("upgradeSystemErrors", u._SYSTEM_ERRORS))
