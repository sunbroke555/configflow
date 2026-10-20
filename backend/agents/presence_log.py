"""Log Agent online/offline transitions.

Offline is derived from the heartbeat age when Agents are listed, never stored,
so transitions are detected by comparing successive scans in this process.
The first scan only records a baseline; a restart does not report every Agent.
"""
import logging
import threading

from backend.utils.logger import log_text

logger = logging.getLogger(__name__)
_lock = threading.Lock()
_known = {}


def observe(agents):
    with _lock:
        first = not _known
        current = {}
        for agent in agents:
            status = 'online' if agent.get('status') == 'online' else 'offline'
            current[agent['id']] = (status, agent.get('name') or agent['id'], agent.get('last_heartbeat') or '')
        for agent_id, (status, name, heartbeat) in current.items():
            previous = _known.get(agent_id)
            if first or previous is None or previous[0] == status:
                continue
            if status == 'online':
                logger.info('Agent %s 已恢复在线', log_text(name))
            else:
                logger.warning('Agent %s 已离线：超过 2 分钟没有心跳（最后心跳 %s）', log_text(name), log_text(heartbeat, 32))
        _known.clear()
        _known.update(current)
