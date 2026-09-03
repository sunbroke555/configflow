"""Agent 管理器"""
import secrets
import hashlib
import hmac
from datetime import datetime, timedelta
from typing import Dict, Any, List, Optional
import requests
from .metrics_history import MetricsHistory


def _constant_time_ascii_equal(provided: Any, expected: Any) -> bool:
    """Compare credentials without timing leaks; non-ASCII is simply invalid."""
    if not isinstance(provided, str) or not isinstance(expected, str):
        return False
    try:
        return hmac.compare_digest(provided.encode('ascii'), expected.encode('ascii'))
    except UnicodeEncodeError:
        return False


class AgentManager:
    """Agent 管理器，负责 Agent 的注册、心跳、状态管理等"""

    def __init__(self, repository):
        """
        初始化 Agent 管理器

        Args:
            repository: system/profile 配置仓库
        """
        self.repository = repository

        # 初始化监控历史管理器
        self.metrics_history = MetricsHistory()

    def _agents(self) -> List[Dict[str, Any]]:
        return self.repository.get_system().get('agents', [])

    def _update_agents(self, updater, profile_id=None):
        result = {}

        def update(system):
            if profile_id:
                self.repository._profile_metadata(profile_id, system)
            result['value'] = updater(system.setdefault('agents', []))

        self.repository.update_system_transaction(update)
        return result.get('value')

    def register_agent(
        self,
        agent_data: Dict[str, Any],
        existing_token: Optional[str] = None,
    ) -> Dict[str, Any]:
        """
        注册新的 Agent

        如果已存在相同名称和地址的 Agent，则更新该记录（保留 ID 和 token）
        否则创建新记录

        Args:
            agent_data: Agent 信息，包含 name, host, port, service_type

        Returns:
            Dict: 新 Agent 包含一次性返回的 token；已有 Agent 只返回非敏感状态
        """
        agent_name = agent_data.get('name', 'Unnamed Agent')
        agent_host = agent_data.get('host', '')
        requested_profile_id = agent_data.get('profile_id')
        if requested_profile_id:
            from backend.common.config import get_repository
            get_repository().validate_profile_id(requested_profile_id)

        def register(agents):
            existing_agent = next(
                (agent for agent in agents if agent.get('name') == agent_name and agent.get('host') == agent_host),
                None,
            )
            if existing_agent:
                if not _constant_time_ascii_equal(existing_token, existing_agent.get('token')):
                    raise PermissionError('Unauthorized')
                agent_id = existing_agent['id']
                token = existing_agent['token']
                updated_agent = {
                    'id': agent_id,
                    'name': agent_name,
                    'host': agent_host,
                    'port': agent_data.get('port', 8080),
                    'token': token,
                    'service_type': agent_data.get('service_type', 'mihomo'),
                    'profile_id': requested_profile_id or existing_agent.get('profile_id', 'default'),
                    'deployment_method': agent_data.get('deployment_method', existing_agent.get('deployment_method', 'unknown')),
                    'status': 'online',
                    'last_heartbeat': datetime.now().isoformat(),
                    'version': agent_data.get('version', '1.0.0'),
                    'config_version': existing_agent.get('config_version', '0'),
                    'enabled': True,
                    'created_at': existing_agent.get('created_at', datetime.now().isoformat()),
                    'updated_at': datetime.now().isoformat(),
                }
                agents[agents.index(existing_agent)] = updated_agent
                return {'id': agent_id, 'status': 'online', 'is_new': False}

            agent_id = f"agent_{int(datetime.now().timestamp() * 1000)}_{secrets.token_hex(3)}"
            token = secrets.token_urlsafe(24)
            agent = {
                'id': agent_id,
                'name': agent_name,
                'host': agent_host,
                'port': agent_data.get('port', 8080),
                'token': token,
                'service_type': agent_data.get('service_type', 'mihomo'),
                'profile_id': requested_profile_id or 'default',
                'deployment_method': agent_data.get('deployment_method', 'unknown'),
                'status': 'online',
                'last_heartbeat': datetime.now().isoformat(),
                'version': agent_data.get('version', '1.0.0'),
                'config_version': '0',
                'enabled': True,
                'created_at': datetime.now().isoformat(),
            }
            agents.append(agent)
            return {
                'id': agent_id,
                'status': 'online',
                'is_new': True,
                'token': token,
            }

        return self._update_agents(register, requested_profile_id)

    def get_all_agents(self) -> List[Dict[str, Any]]:
        """获取所有 Agent 列表"""
        agents = self._agents()

        # 更新在线状态（超过 2 分钟未心跳视为离线）
        now = datetime.now()
        for agent in agents:
            try:
                last_heartbeat = datetime.fromisoformat(agent.get('last_heartbeat', ''))
                if now - last_heartbeat > timedelta(minutes=2):
                    agent['status'] = 'offline'
            except (ValueError, TypeError):
                agent['status'] = 'offline'

        return agents

    def get_agent_by_id(self, agent_id: str) -> Optional[Dict[str, Any]]:
        """根据 ID 获取 Agent"""
        agents = self._agents()
        return next((a for a in agents if a['id'] == agent_id), None)

    def get_agent_by_token(self, token: str) -> Optional[Dict[str, Any]]:
        """根据 token 获取 Agent（用于认证）"""
        agents = self._agents()
        return next((a for a in agents if a.get('token') == token), None)

    def update_agent(self, agent_id: str, updates: Dict[str, Any]) -> bool:
        """
        更新 Agent 信息

        Args:
            agent_id: Agent ID
            updates: 要更新的字段

        Returns:
            bool: 是否更新成功
        """
        if 'profile_id' in updates:
            self.repository.validate_profile_id(updates['profile_id'])

        def update(agents):
            for agent in agents:
                if agent['id'] != agent_id:
                    continue
                # 更新允许的字段
                allowed_fields = ['name', 'host', 'port', 'enabled', 'service_type', 'profile_id']
                for field in allowed_fields:
                    if field in updates:
                        agent[field] = updates[field]

                agent['updated_at'] = datetime.now().isoformat()
                return True
            return False

        return self._update_agents(update, updates.get('profile_id'))

    def delete_agent(self, agent_id: str) -> bool:
        """删除 Agent"""
        def delete(agents):
            initial_len = len(agents)
            agents[:] = [agent for agent in agents if agent['id'] != agent_id]
            return len(agents) < initial_len

        return self._update_agents(delete)

    def update_heartbeat(self, agent_id: str, heartbeat_data: Dict[str, Any] = None) -> bool:
        """
        更新 Agent 心跳

        Args:
            agent_id: Agent ID
            heartbeat_data: 心跳数据，包含 version, service_status, config_version, system_metrics 等

        Returns:
            bool: 是否更新成功
        """
        def update(agents):
            for agent in agents:
                if agent['id'] != agent_id:
                    continue
                agent['last_heartbeat'] = datetime.now().isoformat()
                agent['status'] = 'online'  # Agent 在线状态

                # 更新其他信息
                if heartbeat_data:
                    if 'version' in heartbeat_data:
                        agent['version'] = heartbeat_data['version']
                    if 'service_status' in heartbeat_data:
                        agent['service_status'] = heartbeat_data['service_status']  # 服务运行状态
                    if 'config_version' in heartbeat_data:
                        agent['config_version'] = heartbeat_data['config_version']

                    # 处理系统监控数据
                    if 'system_metrics' in heartbeat_data:
                        system_metrics = heartbeat_data['system_metrics']
                        agent['system_metrics'] = system_metrics

                        # 同时保存到顶层字段以保持向后兼容
                        if 'cpu' in system_metrics:
                            agent['cpu'] = system_metrics['cpu']
                        if 'memory' in system_metrics:
                            agent['memory'] = system_metrics['memory']
                        if 'disk' in system_metrics:
                            agent['disk'] = system_metrics['disk']
                        if 'network' in system_metrics:
                            agent['network'] = system_metrics['network']

                        # 保存监控数据到历史记录
                        try:
                            self.metrics_history.add_metrics(agent_id, system_metrics)
                        except Exception as e:
                            print(f"Warning: Failed to save metrics history: {e}")

                    # 兼容旧版本的直接字段
                    elif 'cpu' in heartbeat_data:
                        agent['cpu'] = heartbeat_data['cpu']
                    elif 'memory' in heartbeat_data:
                        agent['memory'] = heartbeat_data['memory']

                return True
            return False

        return self._update_agents(update)

    def push_config_to_agent(self, agent_id: str, config_content: str, extra_data: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
        """
        主动推送配置到 Agent

        Args:
            agent_id: Agent ID
            config_content: 配置文件内容
            extra_data: 额外数据（如 directories, ruleset_downloads）

        Returns:
            Dict: 推送结果
        """
        import json

        agent = self.get_agent_by_id(agent_id)
        if not agent:
            return {'success': False, 'message': 'Agent not found'}

        # 构建 Agent 的 URL
        agent_url = f"http://{agent['host']}:{agent['port']}/api/config/update"

        # 计算配置 MD5
        config_md5 = hashlib.md5(config_content.encode('utf-8')).hexdigest()

        try:
            # 构建 payload
            payload_dict = {
                'config': config_content,
                'md5': config_md5
            }

            # 添加额外数据（如 directories, ruleset_downloads）
            if extra_data:
                payload_dict.update(extra_data)

            # 手动序列化JSON，确保中文不被转义
            payload = json.dumps(payload_dict, ensure_ascii=False)

            # 发送 POST 请求到 Agent
            response = requests.post(
                agent_url,
                data=payload.encode('utf-8'),
                headers={
                    'Authorization': f'Bearer {agent["token"]}',
                    'Content-Type': 'application/json; charset=utf-8'
                },
                timeout=10
            )

            if response.status_code == 200:
                result = response.json()
                # 更新配置版本
                if result.get('success'):
                    def update_version(agents):
                        for item in agents:
                            if item['id'] == agent_id:
                                item['config_version'] = config_md5[:8]
                                return True
                        return False

                    self._update_agents(update_version)
                    return {'success': True, 'message': 'Config pushed successfully'}
                else:
                    return {'success': False, 'message': result.get('message', 'Unknown error')}
            else:
                return {'success': False, 'message': f'HTTP {response.status_code}'}

        except requests.exceptions.RequestException as e:
            return {'success': False, 'message': f'Connection error: {str(e)}'}

    def restart_agent_service(self, agent_id: str) -> Dict[str, Any]:
        """
        触发 Agent 重启服务

        Args:
            agent_id: Agent ID

        Returns:
            Dict: 操作结果
        """
        agent = self.get_agent_by_id(agent_id)
        if not agent:
            return {'success': False, 'message': 'Agent not found'}

        agent_url = f"http://{agent['host']}:{agent['port']}/api/restart"

        try:
            response = requests.post(
                agent_url,
                headers={
                    'Authorization': f'Bearer {agent["token"]}'
                },
                timeout=10
            )

            if response.status_code == 200:
                return response.json()
            else:
                return {'success': False, 'message': f'HTTP {response.status_code}'}

        except requests.exceptions.RequestException as e:
            return {'success': False, 'message': f'Connection error: {str(e)}'}

    def get_agent_status(self, agent_id: str) -> Dict[str, Any]:
        """
        获取 Agent 状态

        Args:
            agent_id: Agent ID

        Returns:
            Dict: 状态信息
        """
        agent = self.get_agent_by_id(agent_id)
        if not agent:
            return {'success': False, 'message': 'Agent not found'}

        agent_url = f"http://{agent['host']}:{agent['port']}/api/status"

        try:
            response = requests.get(
                agent_url,
                headers={
                    'Authorization': f'Bearer {agent["token"]}'
                },
                timeout=5
            )

            if response.status_code == 200:
                return response.json()
            else:
                return {'success': False, 'message': f'HTTP {response.status_code}'}

        except requests.exceptions.RequestException as e:
            return {'success': False, 'message': f'Connection error: {str(e)}'}

    def get_agent_logs(self, agent_id: str, lines: int = 100, log_path: str = '') -> Dict[str, Any]:
        """
        获取 Agent 日志

        Args:
            agent_id: Agent ID
            lines: 日志行数
            log_path: 可选的自定义日志路径

        Returns:
            Dict: 日志内容
        """
        agent = self.get_agent_by_id(agent_id)
        if not agent:
            return {'success': False, 'message': 'Agent not found'}

        # 构建 URL,包含可选的日志路径参数
        agent_url = f"http://{agent['host']}:{agent['port']}/api/logs?lines={lines}"
        if log_path:
            # URL 编码日志路径
            from urllib.parse import quote
            agent_url += f"&log_path={quote(log_path)}"

        try:
            response = requests.get(
                agent_url,
                headers={
                    'Authorization': f'Bearer {agent["token"]}'
                },
                timeout=5
            )

            if response.status_code == 200:
                return response.json()
            else:
                return {'success': False, 'message': f'HTTP {response.status_code}'}

        except requests.exceptions.RequestException as e:
            return {'success': False, 'message': f'Connection error: {str(e)}'}

    def clear_agent_log(self, agent_id: str, log_path: str) -> Dict[str, Any]:
        """
        清空 Agent 指定日志文件

        Args:
            agent_id: Agent ID
            log_path: 日志文件路径

        Returns:
            Dict: 操作结果
        """
        agent = self.get_agent_by_id(agent_id)
        if not agent:
            return {'success': False, 'message': 'Agent not found'}

        agent_url = f"http://{agent['host']}:{agent['port']}/api/logs/clear"

        try:
            response = requests.post(
                agent_url,
                json={'log_path': log_path},
                headers={
                    'Authorization': f'Bearer {agent["token"]}'
                },
                timeout=5
            )

            if response.status_code == 200:
                return response.json()
            else:
                return {'success': False, 'message': f'HTTP {response.status_code}'}

        except requests.exceptions.RequestException as e:
            return {'success': False, 'message': f'Connection error: {str(e)}'}

    def uninstall_agent(self, agent_id: str) -> Dict[str, Any]:
        """
        卸载远程 Agent

        Args:
            agent_id: Agent ID

        Returns:
            Dict: 卸载结果
        """
        agent = self.get_agent_by_id(agent_id)
        if not agent:
            return {'success': False, 'message': 'Agent not found'}

        agent_url = f"http://{agent['host']}:{agent['port']}/api/uninstall"

        try:
            response = requests.post(
                agent_url,
                headers={
                    'Authorization': f'Bearer {agent["token"]}'
                },
                timeout=10
            )

            if response.status_code == 200:
                result = response.json()
                return {
                    'success': True,
                    'message': 'Agent uninstall started. The agent will be removed in a few seconds.'
                }
            else:
                return {'success': False, 'message': f'HTTP {response.status_code}'}

        except requests.exceptions.RequestException as e:
            return {'success': False, 'message': f'Connection error: {str(e)}'}

    def update_agent_version(self, agent_id: str, new_version: str, binary_url: str) -> Dict[str, Any]:
        """
        触发 Agent 更新

        Args:
            agent_id: Agent ID
            new_version: 新版本号
            binary_url: 新版本二进制文件下载 URL

        Returns:
            Dict: 更新结果
        """
        agent = self.get_agent_by_id(agent_id)
        if not agent:
            return {'success': False, 'message': 'Agent not found'}

        agent_url = f"http://{agent['host']}:{agent['port']}/api/update"

        try:
            payload = {
                'version': new_version,
                'download_url': binary_url
            }

            response = requests.post(
                agent_url,
                json=payload,
                headers={
                    'Authorization': f'Bearer {agent["token"]}',
                    'Content-Type': 'application/json'
                },
                timeout=10
            )

            if response.status_code == 200:
                return {
                    'success': True,
                    'message': 'Agent update started'
                }
            else:
                return {'success': False, 'message': f'HTTP {response.status_code}'}

        except requests.exceptions.RequestException as e:
            return {'success': False, 'message': f'Connection error: {str(e)}'}

    def validate_agent_log_path(self, agent_id: str, log_path: str) -> Dict[str, Any]:
        """
        验证自定义日志路径是否有效

        Args:
            agent_id: Agent ID
            log_path: 日志文件路径

        Returns:
            Dict: 验证结果
        """
        agent = self.get_agent_by_id(agent_id)
        if not agent:
            return {'success': False, 'message': 'Agent not found'}

        agent_url = f"http://{agent['host']}:{agent['port']}/api/logs/validate"

        try:
            response = requests.post(
                agent_url,
                json={'path': log_path},
                headers={
                    'Authorization': f'Bearer {agent["token"]}',
                    'Content-Type': 'application/json'
                },
                timeout=5
            )

            if response.status_code == 200:
                return response.json()
            else:
                return {'success': False, 'message': f'HTTP {response.status_code}'}

        except requests.exceptions.RequestException as e:
            return {'success': False, 'message': f'Connection error: {str(e)}'}

    def get_logging_config(self, agent_id: str) -> Dict[str, Any]:
        """
        获取 Agent 日志配置状态

        Args:
            agent_id: Agent ID

        Returns:
            Dict: 日志配置信息
        """
        agent = self.get_agent_by_id(agent_id)
        if not agent:
            return {'success': False, 'message': 'Agent not found'}

        agent_url = f"http://{agent['host']}:{agent['port']}/api/config/logging"

        try:
            response = requests.get(
                agent_url,
                headers={
                    'Authorization': f'Bearer {agent["token"]}'
                },
                timeout=5
            )

            if response.status_code == 200:
                return response.json()
            else:
                return {'success': False, 'message': f'HTTP {response.status_code}'}

        except requests.exceptions.RequestException as e:
            return {'success': False, 'message': f'Connection error: {str(e)}'}

    def set_logging_config(self, agent_id: str, enabled: bool) -> Dict[str, Any]:
        """
        设置 Agent 日志启用/禁用

        Args:
            agent_id: Agent ID
            enabled: 是否启用日志

        Returns:
            Dict: 操作结果
        """
        agent = self.get_agent_by_id(agent_id)
        if not agent:
            return {'success': False, 'message': 'Agent not found'}

        agent_url = f"http://{agent['host']}:{agent['port']}/api/config/logging"

        try:
            response = requests.post(
                agent_url,
                json={'enabled': enabled},
                headers={
                    'Authorization': f'Bearer {agent["token"]}',
                    'Content-Type': 'application/json'
                },
                timeout=5
            )

            if response.status_code == 200:
                result = response.json()
                # 更新本地配置记录
                if result.get('success'):
                    def update_logging(agents):
                        for item in agents:
                            if item['id'] == agent_id:
                                item.setdefault('logging_config', {})['enabled'] = enabled
                                return True
                        return False

                    self._update_agents(update_logging)
                return result
            else:
                return {'success': False, 'message': f'HTTP {response.status_code}'}

        except requests.exceptions.RequestException as e:
            return {'success': False, 'message': f'Connection error: {str(e)}'}
