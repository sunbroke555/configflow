"""ConfigFlow MCP Server

以 Streamable HTTP (JSON-RPC 2.0) 形式暴露 /mcp 端点，
让外部 MCP 客户端（Claude Desktop / Claude Code 等）调用 ConfigFlow 的全部业务能力。
"""
from backend.mcp_server.server import mcp_bp

__all__ = ['mcp_bp']
