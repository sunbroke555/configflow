"""MCP 协议端点

以 Streamable HTTP 传输实现 MCP：客户端把 JSON-RPC 2.0 请求 POST 到 /mcp，
服务端直接以 application/json 返回结果。服务是无状态的，不需要 SSE 通道，
因此 Flask（WSGI）即可承载，无需引入 ASGI 运行时。
"""
import json
from typing import Any, Dict, List, Optional, Union

from flask import Blueprint, Response, jsonify, request

from backend.mcp_server import auth
from backend.mcp_server.invoker import ApiError
from backend.mcp_server.tools import call_tool, list_tools, has_tool
from backend.utils.logger import get_logger
from backend.version import get_version_info

logger = get_logger(__name__)

mcp_bp = Blueprint('mcp', __name__, url_prefix='/mcp')

# 本服务实现的协议版本；客户端请求的版本在支持列表内时按客户端的版本应答
SUPPORTED_PROTOCOL_VERSIONS = ['2025-06-18', '2025-03-26', '2024-11-05']
DEFAULT_PROTOCOL_VERSION = SUPPORTED_PROTOCOL_VERSIONS[0]

SERVER_NAME = 'configflow'

# JSON-RPC 标准错误码
PARSE_ERROR = -32700
INVALID_REQUEST = -32600
METHOD_NOT_FOUND = -32601
INVALID_PARAMS = -32602
INTERNAL_ERROR = -32603


class InvalidParams(Exception):
    """请求参数不合法。

    用独立异常而非 ValueError：工具内部意外抛出的 ValueError
    （如解码失败）不应被误报成「参数非法」而掩盖真实故障。
    """


def _result(request_id: Any, result: Any) -> Dict[str, Any]:
    return {'jsonrpc': '2.0', 'id': request_id, 'result': result}


def _error(request_id: Any, code: int, message: str) -> Dict[str, Any]:
    return {'jsonrpc': '2.0', 'id': request_id, 'error': {'code': code, 'message': message}}


def _text_content(payload: Any) -> List[Dict[str, str]]:
    if isinstance(payload, str):
        text = payload
    else:
        text = json.dumps(payload, ensure_ascii=False, indent=2, default=str)
    return [{'type': 'text', 'text': text}]


def _handle_initialize(params: Dict[str, Any]) -> Dict[str, Any]:
    requested = params.get('protocolVersion')
    version = requested if requested in SUPPORTED_PROTOCOL_VERSIONS else DEFAULT_PROTOCOL_VERSION
    return {
        'protocolVersion': version,
        'capabilities': {'tools': {'listChanged': False}},
        'serverInfo': {
            'name': SERVER_NAME,
            'version': get_version_info().get('version', 'unknown'),
        },
        'instructions': (
            'ConfigFlow 代理配置管理平台。可管理订阅、节点、规则、规则仓库、策略组、'
            '订阅聚合、MosDNS 与远程 Agent，并生成 Mihomo / Surge / MosDNS 配置。'
            '建议先调用 get_overview 了解当前状态；修改配置后需调用 generate_config '
            '才会写入订阅链接对应的配置文件。'
        ),
    }


def _handle_tools_call(params: Dict[str, Any]) -> Dict[str, Any]:
    name = params.get('name')
    if not name:
        raise InvalidParams('tools/call 缺少参数 name')

    arguments = params.get('arguments') or {}
    if not isinstance(arguments, dict):
        raise InvalidParams('tools/call 的 arguments 必须是对象')

    if not has_tool(name):
        raise InvalidParams(f"未知的工具: {name}")

    try:
        payload = call_tool(name, arguments)
    except ApiError as exc:
        # 业务失败通过 isError 回给模型，让它有机会自行纠正，而不是中断整个会话
        logger.warning(f"MCP 工具 {name} 调用失败: {exc.message}")
        return {'content': _text_content(f"调用失败（HTTP {exc.status_code}）：{exc.message}"), 'isError': True}

    return {'content': _text_content(payload), 'isError': False}


def _dispatch(message: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    """处理单条 JSON-RPC 消息，通知类消息返回 None"""
    request_id = message.get('id')
    is_notification = 'id' not in message
    method = message.get('method')
    params = message.get('params') or {}

    if message.get('jsonrpc') != '2.0' or not method:
        return None if is_notification else _error(request_id, INVALID_REQUEST, '不是合法的 JSON-RPC 2.0 请求')

    if is_notification:
        # notifications/initialized、notifications/cancelled 等无需应答
        return None

    try:
        if method == 'initialize':
            return _result(request_id, _handle_initialize(params))
        if method == 'ping':
            return _result(request_id, {})
        if method == 'tools/list':
            return _result(request_id, {'tools': list_tools()})
        if method == 'tools/call':
            return _result(request_id, _handle_tools_call(params))
        return _error(request_id, METHOD_NOT_FOUND, f"不支持的方法: {method}")
    except InvalidParams as exc:
        return _error(request_id, INVALID_PARAMS, str(exc))
    except Exception as exc:  # noqa: BLE001 - 兜底，避免单条消息把整个端点打挂
        logger.exception(f"MCP 方法 {method} 执行异常")
        return _error(request_id, INTERNAL_ERROR, str(exc))


@mcp_bp.route('', methods=['POST'])
@mcp_bp.route('/', methods=['POST'])
def handle_mcp() -> Union[Response, tuple]:
    """MCP Streamable HTTP 端点"""
    if not auth.authenticate():
        return jsonify(_error(None, INVALID_REQUEST, 'Unauthorized')), 401

    try:
        payload = json.loads(request.get_data(as_text=True) or '')
    except ValueError:
        return jsonify(_error(None, PARSE_ERROR, '请求体不是合法 JSON')), 400

    if isinstance(payload, list):
        if not payload:
            return jsonify(_error(None, INVALID_REQUEST, '批量请求不能为空')), 400
        responses = [r for r in (_dispatch(m) for m in payload if isinstance(m, dict)) if r]
        if not responses:
            return Response(status=202)
        return jsonify(responses)

    if not isinstance(payload, dict):
        return jsonify(_error(None, INVALID_REQUEST, '请求体必须是对象或数组')), 400

    response = _dispatch(payload)
    if response is None:
        return Response(status=202)
    return jsonify(response)


@mcp_bp.route('', methods=['GET', 'DELETE'])
@mcp_bp.route('/', methods=['GET', 'DELETE'])
def unsupported_transport() -> tuple:
    """无状态服务不提供服务端主动推送的 SSE 通道，也没有需要终止的会话"""
    return jsonify(_error(None, METHOD_NOT_FOUND, '本服务为无状态 MCP，仅支持 POST /mcp')), 405
