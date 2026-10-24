"""Agent heartbeat state kept beside the configuration document.

Every heartbeat used to rewrite and fully revalidate config.json. The fields a
heartbeat reports now live in one small SQLite table instead and are laid over
the stored Agent on read. The configuration document keeps what registration
wrote, so a backup, a restore or older code still sees a complete Agent.

Whichever side holds the newer heartbeat wins: registration writes the JSON
side, so a re-registered Agent shows its new version until its next heartbeat.
config_version is a publication baseline, not heartbeat state, and stays in
the configuration document.
"""
from contextlib import contextmanager
from datetime import datetime
import json
import logging
import os
from pathlib import Path
import sqlite3
import time

logger = logging.getLogger(__name__)

RUNTIME_FIELDS = ('version', 'service_status', 'system_metrics', 'cpu', 'memory', 'disk', 'network')
_METRIC_FIELDS = ('cpu', 'memory', 'disk', 'network')


def _timestamp(value):
    try:
        return datetime.fromisoformat(value).timestamp()
    except (TypeError, ValueError):
        return float('-inf')


class AgentRuntimeStore:
    def __init__(self, data_dir):
        self.path = Path(data_dir) / 'runtime.sqlite3'

    @contextmanager
    def _database(self):
        # Create privately before sqlite opens it, even with a permissive umask.
        os.close(os.open(self.path, os.O_CREAT | os.O_RDWR, 0o600))
        connection = sqlite3.connect(self.path, timeout=5)
        try:
            connection.execute('CREATE TABLE IF NOT EXISTS agent_runtime (agent_id TEXT PRIMARY KEY, '
                               'heartbeat_at REAL NOT NULL, last_heartbeat TEXT NOT NULL, fields TEXT NOT NULL)')
            connection.execute('BEGIN IMMEDIATE')
            yield connection
            connection.commit()
        finally:
            connection.close()

    def _write(self, action):
        try:
            with self._database() as connection:
                return action(connection)
        except sqlite3.DatabaseError as error:
            if type(error) is not sqlite3.DatabaseError:
                raise
            # Not a database any more; heartbeat state is safe to lose, so start over.
            corrupt = self.path.with_name(f'{self.path.name}.corrupt-{int(time.time())}')
            logger.warning('Agent 运行状态库已损坏（%s），已移到 %s 并重建', error, corrupt.name)
            os.replace(self.path, corrupt)
            with self._database() as connection:
                return action(connection)

    def _rows(self):
        if not self.path.is_file():
            return {}
        try:
            connection = sqlite3.connect(self.path, timeout=5)
            try:
                rows = connection.execute('SELECT agent_id, heartbeat_at, last_heartbeat, fields FROM agent_runtime').fetchall()
            finally:
                connection.close()
        except sqlite3.Error as error:
            # Agents then show their registration state; they are never hidden.
            logger.warning('Agent 运行状态读取失败，暂按注册信息显示：%s', error)
            return {}
        return {agent_id: (heartbeat_at, last_heartbeat, json.loads(fields))
                for agent_id, heartbeat_at, last_heartbeat, fields in rows}

    def overlay(self, agents):
        """Lay newer heartbeat state over the given (copied) Agents in place."""
        rows = self._rows()
        for agent in agents:
            row = rows.get(agent.get('id'))
            if row and row[0] > _timestamp(agent.get('last_heartbeat')):
                agent.update(row[2])
                agent['last_heartbeat'] = row[1]
                agent['status'] = 'online'
        return agents

    def record_heartbeat(self, agent, payload):
        """Merge one heartbeat into the newer of the stored and registered state."""
        now = time.time()

        def record(connection):
            row = connection.execute('SELECT heartbeat_at, fields FROM agent_runtime WHERE agent_id = ?',
                                     (agent['id'],)).fetchone()
            if row and row[0] > _timestamp(agent.get('last_heartbeat')):
                fields = json.loads(row[1])
            else:
                fields = {key: agent[key] for key in RUNTIME_FIELDS if key in agent}
            # Fields a heartbeat leaves out keep their last value.
            for key in ('version', 'service_status'):
                if key in payload:
                    fields[key] = payload[key]
            if 'system_metrics' in payload:
                metrics = payload['system_metrics']
                fields['system_metrics'] = metrics
                fields.update({key: metrics[key] for key in _METRIC_FIELDS if key in metrics})
            connection.execute('INSERT OR REPLACE INTO agent_runtime VALUES (?, ?, ?, ?)',
                               (agent['id'], now, datetime.fromtimestamp(now).isoformat(),
                                json.dumps(fields, ensure_ascii=False)))
            return fields

        return self._write(record)

    def delete(self, agent_id):
        if self.path.is_file():
            self._write(lambda connection: connection.execute(
                'DELETE FROM agent_runtime WHERE agent_id = ?', (agent_id,)))
