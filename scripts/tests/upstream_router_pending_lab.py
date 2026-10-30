#!/usr/bin/env python3
"""Serve this checkout's backend on a private copy of the IPv6 preview data (test server only).

The upstream router reminder reads the real VM151 RouterOS and the CT152 Agent
through the same report and release preview as the 上游路由 page. Nothing is
written to either: the browser script never confirms a release, and the copy's
own changes (a Mihomo draft, an unreachable extra router) stay in WORK.

CT152 sends its heartbeats to the preview service, not to this copy, so the
copy marks the lab Mihomo online itself; requests to it still go to the
real Agent. VM151 is shared with other labs and is never published to, so
the synchronised case is an extra iKuai router at SYNCED_ENDPOINT served by
an in-process driver (see synced_fixture); everything else is real.

    python3 scripts/tests/upstream_router_pending_lab.py explore   # print the states once
    python3 scripts/tests/upstream_router_pending_lab.py serve     # backend on $CF_LAB_API_PORT
"""
from datetime import datetime
import json
import os
from pathlib import Path
import shutil
import socket
import sys
import threading
import time

SOURCE = Path(__file__).resolve().parents[2]
PREVIEW = Path('/home/sunbroke/previews/configflow-ipv6-readiness')
OWNER = 'configflow-ipv6-readiness-20261030'
WORK = Path(os.environ.get('CF_LAB_WORK') or '/tmp/configflow-upstream-router-pending-lab')
MIHOMO_HOST = '192.0.2.3'


def prepare():
    if sys.platform != 'linux' or socket.gethostname() == 'pve':
        raise RuntimeError('Run on the Linux test server')
    if json.loads((PREVIEW / 'connections.json').read_text())['owner'] != OWNER:
        raise RuntimeError('Preview ownership changed')
    shutil.rmtree(WORK, ignore_errors=True)
    WORK.mkdir(mode=0o700, parents=True)
    shutil.copytree(PREVIEW / 'data', WORK / 'data')
    credentials = json.loads((PREVIEW / 'credentials.json').read_text())
    os.environ.update(DATA_DIR=str(WORK / 'data'), LOG_FILE=str(WORK / 'server.log'), LOG_LEVEL='WARNING',
                      ADMIN_USERNAME=credentials['username'], ADMIN_PASSWORD=credentials['password'],
                      JWT_SECRET_KEY=credentials['jwt_secret'], AGENT_REGISTRATION_KEY=credentials['registration_key'],
                      SUB_STORE_BUILTIN_DIR=str(WORK / 'disabled-sub-store'))
    for name in ('HTTP_PROXY', 'HTTPS_PROXY', 'ALL_PROXY', 'http_proxy', 'https_proxy', 'all_proxy'):
        os.environ.pop(name, None)
    sys.path.insert(0, str(SOURCE))
    from backend.common import config as store
    from backend.common.config_repository import ProfileRepository
    store.set_repository(ProfileRepository(WORK / 'data', default_config_factory=store.get_default_config,
                                           initial_config_factory=store.get_default_config))
    # Automatic jobs would act on the real router from this copy: keep them off.
    import backend.agents.rule_sync as agent_rule_sync
    agent_rule_sync.start_loop = lambda: None
    from backend.common.agent_manager import get_agent_manager
    manager = get_agent_manager()
    mihomo = [agent for agent in manager.get_all_agents()
              if agent.get('service_type') == 'mihomo' and agent.get('host') == MIHOMO_HOST]
    if len(mihomo) != 1:
        raise RuntimeError('Expected exactly one lab Mihomo in the preview data')
    return manager, mihomo[0]['id']


def synced_fixture():
    """An iKuai router at SYNCED_ENDPOINT that already has exactly the desired state.

    VM151 is shared with other labs and must not be published to, so the
    synchronised case uses this in-process driver; the report, release preview,
    API and page are the real ones. Every other endpoint uses the real drivers.
    """
    from backend.routes import routers as views
    from backend.routers.base import NatBypassEntry, RouteEntry, RouterDriver, RouterState
    real = views.DRIVERS['ikuai']

    class Synced(RouterDriver):
        vendor = 'ikuai'
        supports_write = True

        def read_state(self):
            return RouterState(routes=[RouteEntry('1', '198.18.0.0/16', MIHOMO_HOST, True, 'cf:fake-ip')],
                               nat_bypass=[NatBypassEntry('n1', ['198.18.0.0/16'], [], True, 'cf-mihomo')])

    def driver(endpoint, *args, **kwargs):
        return Synced() if endpoint == SYNCED_ENDPOINT else real(endpoint, *args, **kwargs)
    views.DRIVERS['ikuai'] = driver


SYNCED_ENDPOINT = 'http://synced.lab.invalid'


def keep_online(manager, agent_id):
    def beat():
        while True:
            def change(agents):
                for item in agents:
                    if item['id'] == agent_id:
                        item.update(status='online', last_heartbeat=datetime.now().isoformat())
            manager._update_agents(change)
            time.sleep(15)
    threading.Thread(target=beat, daemon=True).start()


def main():
    mode = sys.argv[1] if len(sys.argv) > 1 else 'serve'
    manager, agent_id = prepare()
    keep_online(manager, agent_id)
    synced_fixture()
    if mode == 'explore':
        from backend.routers import pending
        from backend.app import app
        with app.test_request_context('/'):
            data = pending.agent_routers(agent_id, refresh=True)
        print(json.dumps({'agent_id': agent_id, **data}, ensure_ascii=False, indent=2))
        return
    from backend.app import app
    port = int(os.environ.get('CF_LAB_API_PORT') or 15881)
    print(json.dumps({'ready': True, 'agent_id': agent_id, 'port': port}), flush=True)
    from waitress import serve
    serve(app, host='127.0.0.1', port=port, threads=8)


if __name__ == '__main__':
    main()
