#!/bin/sh
# Real Linux network lab. This entrypoint must NEVER run on a real host.
set -eu
umask 077
STATE=/var/lib/configflow-gateway-lab
SCRIPT_DIR=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd -P)
HELPER="$SCRIPT_DIR/linux_gateway_lab_support.py"
NAMESPACES='cfgl-gateway cfgl-router cfgl-client cfgl-switch cfgl-origin'
UNITS='configflow-agent-mihomo configflow-agent-oxidns mihomo oxidns'
fail() { printf '%s\n' "LAB ERROR: $*" >&2; exit 1; }
usage() {
    cat <<'EOF'
Run ONLY in an explicitly marked, disposable QEMU/Apple Linux VM:
  linux_gateway_lab_network.sh setup
  linux_gateway_lab_network.sh snapshot LABEL
  linux_gateway_lab_network.sh compare BEFORE AFTER [gateway|root|both]
  linux_gateway_lab_network.sh probe active|ordinary|removed|direct [--controller-token-file FILE]
  linux_gateway_lab_network.sh cleanup

VM prerequisites: root; hostname configflow-gateway-lab; systemd; Python 3;
iproute2, nftables, curl, slirp4netns, util-linux. Explicit marker (0600, root):
  CONFIGFLOW_DISPOSABLE_LINUX_VM=1
  MACHINE_ID=<this VM's /etc/machine-id>
Marker path: /root/.configflow-disposable-lab. The script NEVER creates it.

Topology: gateway lan0 192.0.2.2/24 -> router 192.0.2.1; client
192.0.2.100 uses gateway; three LAN ports share an isolated bridge namespace.
Router origin0 203.0.113.1 -> origin 203.0.113.2:1080 SOCKS5 and
.10/.20/.30:18080 HTTP endpoints. Router tap0 uses slirp4netns user-mode
networking for downloads without changing the VM initial namespace.

Start ConfigFlow explicitly in cfgl-router, bound to 192.0.2.1:8000.
Four installed systemd units use a lab-owned network namespace drop-in.
All initial-namespace routes/sysctls/nft must compare equal after cleanup.
EOF
}
case "${1:---help}" in -h|--help|help) usage; exit 0 ;; esac
[ "$(uname -s)" = Linux ] || fail 'Linux VM only; no host networking was inspected or changed.'
[ "$(id -u)" -eq 0 ] || fail 'Run as root inside the disposable VM.'
command -v python3 >/dev/null 2>&1 || fail 'Python 3 is required.'
# The shared guard verifies VM identity, hostname, exact per-machine marker,
# ownership and mode BEFORE any network command in this script can run.
python3 - "$HELPER" <<'PY'
import importlib.util, sys
spec=importlib.util.spec_from_file_location('cfgl_guard',sys.argv[1]);mod=importlib.util.module_from_spec(spec);spec.loader.exec_module(mod)
mod.guard(require_setup=False)
PY
initial_namespace() {
    [ "$(readlink /proc/self/ns/net)" = "$(readlink /proc/1/ns/net)" ] || fail 'setup/cleanup require the VM initial network namespace.'
}
ready() {
    [ -d "$STATE" ] && [ ! -L "$STATE" ] && [ -f "$STATE/owner" ] || fail 'Lab setup is missing.'
    [ "$(cat "$STATE/machine-id")" = "$(cat /etc/machine-id)" ] || fail 'Lab state belongs to a different VM.'
}
valid_label() {
    case "$1" in ''|*[!A-Za-z0-9_-]*) fail 'Snapshot labels use letters, numbers, hyphens and underscores only.' ;; esac
}
snapshot() {
    label=$1
    valid_label "$label"
    mkdir -p "$STATE/snapshots"
    ip netns exec cfgl-gateway python3 "$HELPER" snapshot > "$STATE/snapshots/$label.gateway.json"
    nsenter --net=/proc/1/ns/net -- python3 "$HELPER" snapshot > "$STATE/snapshots/$label.root.json"
    printf '{"success":true,"gateway":"%s","root":"%s"}\n' "$STATE/snapshots/$label.gateway.json" "$STATE/snapshots/$label.root.json"
}
compare() {
    before=$1; after=$2; scope=${3:-both}
    valid_label "$before"; valid_label "$after"
    case "$scope" in both) scopes='gateway root' ;; gateway|root) scopes=$scope ;; *) fail 'Scope must be gateway, root or both.' ;; esac
    for namespace in $scopes; do
        first="$STATE/snapshots/$before.$namespace.json"
        second="$STATE/snapshots/$after.$namespace.json"
        [ -f "$first" ] && [ -f "$second" ] || fail 'Snapshot pair is missing.'
        if ! cmp -s "$first" "$second"; then
            diff -u "$first" "$second" >&2 || true
            fail "$namespace network state changed between $before and $after."
        fi
    done
    printf '{"success":true,"before":"%s","after":"%s","scope":"%s"}\n' "$before" "$after" "$scope"
}
setup() {
    initial_namespace
    for dependency in ip nft curl slirp4netns nsenter systemctl cmp sha256sum; do
        command -v "$dependency" >/dev/null 2>&1 || fail "Install prerequisite $dependency inside this VM first."
    done
    [ -c /dev/net/tun ] || fail 'The disposable VM needs /dev/net/tun.'
    [ ! -e "$STATE" ] && [ ! -L "$STATE" ] || fail "Existing $STATE; inspect it and run cleanup before another setup."
    for namespace in $NAMESPACES; do
        [ ! -e "/run/netns/$namespace" ] || fail "Namespace $namespace already exists; refusing to claim it."
        [ ! -e "/etc/netns/$namespace" ] || fail "DNS directory /etc/netns/$namespace already exists."
    done
    for unit in $UNITS; do
        [ ! -e "/etc/systemd/system/$unit.service.d/90-configflow-gateway-lab.conf" ] || fail "Existing lab drop-in for $unit."
        if systemctl is-active --quiet "$unit"; then fail "Service $unit is already running before lab setup; refusing to move it."; fi
    done
    mkdir -m 700 "$STATE"
    cat /proc/sys/kernel/random/uuid > "$STATE/owner"
    cp /etc/machine-id "$STATE/machine-id"
    python3 "$HELPER" snapshot > "$STATE/root-before.json"
    # Keep file evidence separate from network snapshots so older saved labels
    # retain their format. Namespace NSS overrides must never alter this file.
    sha256sum /etc/nsswitch.conf > "$STATE/root-nsswitch.sha256"
    : > "$STATE/namespaces"
    : > "$STATE/dropins.sha256"
    for namespace in $NAMESPACES; do
        ip netns add "$namespace"
        printf '%s %s\n' "$namespace" "$(stat -Lc %i "/run/netns/$namespace")" >> "$STATE/namespaces"
        ip -n "$namespace" link set lo up
        mkdir -m 755 -p "/etc/netns/$namespace"
        printf 'nameserver 10.0.2.3\noptions timeout:2 attempts:2\n' > "/etc/netns/$namespace/resolv.conf"
        # libnss_resolve uses a global AF_UNIX socket, bypassing netns DNS and
        # potentially returning the VM host's unrelated fake-IP answers. Keep
        # all other NSS databases intact and confine name lookup to this netns.
        python3 - "$namespace" <<'PY'
from pathlib import Path
import re, sys
source = Path('/etc/nsswitch.conf').read_text()
result, changed = re.subn(r'^hosts:.*$', 'hosts: files dns', source, flags=re.MULTILINE)
if not changed:
    result += '\nhosts: files dns\n'
Path('/etc/netns', sys.argv[1], 'nsswitch.conf').write_text(result)
PY
    done
    ip -n cfgl-switch link add br0 type bridge
    ip -n cfgl-switch link set br0 up
    for peer in gateway router client; do
        ip -n cfgl-switch link add "sw-$peer" type veth peer name lan0 netns "cfgl-$peer"
        ip -n cfgl-switch link set "sw-$peer" master br0
        ip -n cfgl-switch link set "sw-$peer" up
        ip -n "cfgl-$peer" link set lan0 up
    done
    ip -n cfgl-gateway address add 192.0.2.2/24 dev lan0
    ip -n cfgl-router address add 192.0.2.1/24 dev lan0
    ip -n cfgl-client address add 192.0.2.100/24 dev lan0
    ip -n cfgl-gateway route add default via 192.0.2.1 dev lan0
    ip -n cfgl-client route add default via 192.0.2.2 dev lan0
    ip -n cfgl-router link add origin0 type veth peer name wan0 netns cfgl-origin
    ip -n cfgl-router link set origin0 up
    ip -n cfgl-origin link set wan0 up
    ip -n cfgl-router address add 203.0.113.1/24 dev origin0
    ip -n cfgl-origin address add 203.0.113.2/24 dev wan0
    for target in 10 20 30; do ip -n cfgl-origin address add "203.0.113.$target/32" dev lo; done
    ip -n cfgl-origin route add default via 203.0.113.1 dev wan0
    # Only NEW lab namespaces are configured. The VM root namespace is untouched.
    ip netns exec cfgl-gateway sysctl -qw net.ipv4.ip_forward=0
    ip netns exec cfgl-gateway sysctl -qw net.ipv4.conf.all.send_redirects=1 net.ipv4.conf.default.send_redirects=1 net.ipv4.conf.lan0.send_redirects=1
    ip netns exec cfgl-client sysctl -qw net.ipv4.conf.all.accept_redirects=0 net.ipv4.conf.lan0.accept_redirects=0
    ip netns exec cfgl-router sysctl -qw net.ipv4.ip_forward=1 net.ipv4.conf.all.send_redirects=0 net.ipv4.conf.default.send_redirects=0 net.ipv4.conf.lan0.send_redirects=0
    ip netns exec cfgl-router sleep infinity > "$STATE/router-keeper.log" 2>&1 &
    keeper_pid=$!
    printf '%s\n' "$keeper_pid" > "$STATE/router-keeper.pid"
    slirp4netns --configure --disable-host-loopback --mtu=1500 "$keeper_pid" tap0 > "$STATE/slirp.log" 2>&1 &
    printf '%s\n' "$!" > "$STATE/slirp.pid"
    count=0
    until ip -n cfgl-router link show tap0 >/dev/null 2>&1 && ip -n cfgl-router route show default | grep -q 'via 10.0.2.2'; do
        count=$((count + 1)); [ "$count" -lt 50 ] || fail "slirp4netns did not become ready; inspect $STATE/slirp.log."
        sleep .1
    done
    ip netns exec cfgl-router nft -f - <<'NFT'
table ip cfgl_router_egress {
  chain postrouting {
    type nat hook postrouting priority 100; policy accept;
    oifname "tap0" masquerade
  }
}
NFT
    for unit in $UNITS; do
        directory="/etc/systemd/system/$unit.service.d"
        mkdir -p "$directory"
        cat > "$directory/90-configflow-gateway-lab.conf" <<'UNIT'
# ConfigFlow disposable VM lab. Not a production configuration.
[Service]
NetworkNamespacePath=/run/netns/cfgl-gateway
BindReadOnlyPaths=/etc/netns/cfgl-gateway/resolv.conf:/etc/resolv.conf
BindReadOnlyPaths=/etc/netns/cfgl-gateway/nsswitch.conf:/etc/nsswitch.conf
UNIT
        sha256sum "$directory/90-configflow-gateway-lab.conf" >> "$STATE/dropins.sha256"
    done
    systemctl daemon-reload
    ip netns exec cfgl-origin python3 "$HELPER" serve > "$STATE/fixtures.log" 2>&1 &
    printf '%s\n' "$!" > "$STATE/fixtures.pid"
    count=0
    until [ -f "$STATE/fixtures.ready" ]; do
        count=$((count + 1)); [ "$count" -lt 50 ] || fail "Fixtures failed to start; inspect $STATE/fixtures.log."
        sleep .1
    done
    cat > "$STATE/topology.json" <<'JSON'
{"server_url":"http://192.0.2.1:8000","server_namespace":"cfgl-router","gateway_namespace":"cfgl-gateway","client_namespace":"cfgl-client","origin_namespace":"cfgl-origin","network":{"interface":"lan0","address":"192.0.2.2","lan_cidr":"192.0.2.0/24","upstream_gateway":"192.0.2.1"},"socks":{"name":"LAB-SOCKS","group":"LAB-PROXY","address":"203.0.113.2","port":1080},"http_port":18080,"direct_ip":"203.0.113.10","selected_ip":"203.0.113.20","fake_domain":"proxy.lab.invalid","fake_origin_ip":"203.0.113.30","fake_ip_range":"198.18.0.0/16","controller_url":"http://192.0.2.2:9090","state_directory":"/var/lib/configflow-gateway-lab"}
JSON
    snapshot setup >/dev/null
    python3 "$HELPER" snapshot > "$STATE/root-after-setup.json"
    cmp -s "$STATE/root-before.json" "$STATE/root-after-setup.json" || fail 'Setup changed the initial namespace; preserve evidence and investigate.'
    cat "$STATE/topology.json"
}
cleanup() {
    initial_namespace
    ready
    # Verify ownership before stopping services or removing any lab resource.
    [ ! -s "$STATE/dropins.sha256" ] || sha256sum -c "$STATE/dropins.sha256" >/dev/null || fail 'A lab drop-in was externally changed; refusing automatic cleanup.'
    while read -r namespace inode; do
        case "$namespace" in cfgl-gateway|cfgl-router|cfgl-client|cfgl-switch|cfgl-origin) ;; *) fail 'Invalid recorded namespace.' ;; esac
        if [ -e "/run/netns/$namespace" ]; then
            [ "$(stat -Lc %i "/run/netns/$namespace")" = "$inode" ] || fail "Namespace identity changed: $namespace"
        fi
    done < "$STATE/namespaces"
    for unit in $UNITS; do
        dropin="/etc/systemd/system/$unit.service.d/90-configflow-gateway-lab.conf"
        [ -f "$dropin" ] || continue
        systemctl stop "$unit" 2>/dev/null || true
        # Lab-installed cores must not restart in the initial namespace after the
        # lab namespace/drop-in is removed. Files remain available for inspection.
        systemctl disable "$unit" 2>/dev/null || true
        rm "$dropin"
        rmdir "/etc/systemd/system/$unit.service.d" 2>/dev/null || true
    done
    if [ -f "$STATE/slirp.pid" ]; then
        pid=$(cat "$STATE/slirp.pid")
        if [ -e "/proc/$pid/cmdline" ]; then
            tr '\0' ' ' < "/proc/$pid/cmdline" | grep -q 'slirp4netns' || fail 'Recorded slirp PID was reused.'
            kill "$pid" 2>/dev/null || true
        fi
    fi
    while read -r namespace inode; do
        [ -e "/run/netns/$namespace" ] || continue
        for pid in $(ip netns pids "$namespace"); do kill "$pid" 2>/dev/null || true; done
    done < "$STATE/namespaces"
    sleep .2
    while read -r namespace inode; do
        [ ! -e "/run/netns/$namespace" ] || ip netns delete "$namespace"
        if [ -d "/etc/netns/$namespace" ]; then
            rm -f "/etc/netns/$namespace/resolv.conf"
            rm -f "/etc/netns/$namespace/nsswitch.conf"
            rmdir "/etc/netns/$namespace"
        fi
    done < "$STATE/namespaces"
    systemctl daemon-reload
    python3 "$HELPER" snapshot > "$STATE/root-after-cleanup.json"
    cmp -s "$STATE/root-before.json" "$STATE/root-after-cleanup.json" || fail 'Initial-namespace network changed; inspect the saved root snapshots.'
    initial_nsswitch_unchanged=null
    if [ -f "$STATE/root-nsswitch.sha256" ]; then
        sha256sum -c "$STATE/root-nsswitch.sha256" > "$STATE/root-nsswitch-after-cleanup.log" || fail 'Initial-namespace nsswitch.conf changed; preserve the lab evidence.'
        initial_nsswitch_unchanged=true
    fi
    printf '{"success":true,"initial_namespace_unchanged":true,"initial_nsswitch_unchanged":%s,"artifacts":"%s"}\n' "$initial_nsswitch_unchanged" "$STATE"
    # Keep all reports; the owning VM is meant to be discarded after review.
    mv "$STATE/owner" "$STATE/owner.cleaned"
}
case "$1" in
    setup) [ "$#" -eq 1 ] || fail 'setup takes no arguments'; setup ;;
    snapshot) [ "$#" -eq 2 ] || fail 'snapshot needs a label'; ready; snapshot "$2" ;;
    compare) [ "$#" -ge 3 ] && [ "$#" -le 4 ] || fail 'compare needs two labels and optional scope'; ready; compare "$2" "$3" "${4:-both}" ;;
    probe)
        ready
        [ "$#" -ge 2 ] || fail 'probe needs a mode'
        mode=$2; shift 2
        python3 "$HELPER" probe --mode "$mode" "$@"
        ;;
    cleanup) [ "$#" -eq 1 ] || fail 'cleanup takes no arguments'; cleanup ;;
    *) usage >&2; exit 2 ;;
esac
