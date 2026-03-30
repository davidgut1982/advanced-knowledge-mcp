#!/usr/bin/env python3
"""
Complete HTTP MCP Transport Wrapper for knowledge-mcp Server
Uses proper Server-Sent Events (SSE) transport format for Claude Code compatibility
"""

import argparse
import asyncio
import importlib
import json
import logging
import os
import sys
import uuid
from datetime import datetime
from pathlib import Path
from typing import Any, Dict

import uvicorn
from starlette.applications import Starlette
from starlette.routing import Route
from starlette.responses import JSONResponse, StreamingResponse
from starlette.requests import Request
from starlette.middleware import Middleware
from starlette.middleware.cors import CORSMiddleware
from mcp.server.sse import SseServerTransport

class _SseResponse:
    """No-op Response for SSE endpoints."""
    async def __call__(self, scope, receive, send):
        pass

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s'
)
logger = logging.getLogger(__name__)

# Session management
SESSIONS = {}

def load_mcp_server(module_path: str):
    """Import the MCP server module and return the server instance."""
    logger.info(f"Loading MCP module: {module_path}")

    module = importlib.import_module(module_path)

    # Try common variable names for MCP server instance
    for attr_name in ['app', 'server', 'mcp', 'mcp_server']:
        if hasattr(module, attr_name):
            server = getattr(module, attr_name)
            # Check if it's an MCP Server instance
            if hasattr(server, 'run') and hasattr(server, 'create_initialization_options'):
                logger.info(f"Found MCP server as '{attr_name}'")
                return server, attr_name

    raise ValueError(
        f"Could not find MCP server instance in {module_path}. "
        f"Expected 'app', 'server', 'mcp', or 'mcp_server' variable."
    )

async def execute_tool(tool_name: str, tool_args: dict, mcp_server, server_module_name: str):
    """Execute a tool using the actual MCP server's call_tool handler."""
    try:
        logger.info(f"Executing tool: {tool_name} with args: {tool_args}")

        # Use the MCP server's call_tool method directly
        if hasattr(mcp_server, '_tool_handlers') and tool_name in mcp_server._tool_handlers:
            # Call the tool handler through the MCP server framework
            handler = mcp_server._tool_handlers[tool_name]
            result = await handler(tool_name, tool_args)
            logger.info(f"Tool {tool_name} executed successfully via MCP framework")
            return result

        # Fallback: try to call the module's call_tool function directly
        module = importlib.import_module(server_module_name)
        if hasattr(module, 'call_tool'):
            call_tool_func = getattr(module, 'call_tool')
            if callable(call_tool_func):
                result = await call_tool_func(tool_name, tool_args)
                logger.info(f"Tool {tool_name} executed via module call_tool function")
                return result

        # Last resort: look for tool-specific handlers in the module
        handler_name = f"handle_{tool_name.replace('-', '_')}"
        if hasattr(module, handler_name):
            handler = getattr(module, handler_name)
            if callable(handler):
                result = handler(**tool_args)
                logger.info(f"Tool {tool_name} executed via handler {handler_name}")
                return result

        raise ValueError(f"No handler found for tool '{tool_name}'")

    except Exception as e:
        logger.error(f"Error executing tool {tool_name}: {e}", exc_info=True)
        raise

def create_app(mcp_server, server_name: str):
    """Create Starlette app with HTTP MCP endpoints including initialize."""

    sse = SseServerTransport("/mcp")

    async def handle_sse(request):
        """Handle SSE connection for MCP protocol."""
        async with sse.connect_sse(
            request.scope, request.receive, request._send
        ) as streams:
            await mcp_server.run(
                streams[0], streams[1], mcp_server.create_initialization_options()
            )
        return _SseResponse()

    async def health_check(request: Request):
        """Health check endpoint."""
        return JSONResponse({"status": "healthy"})

    async def handle_mcp(request: Request):
        """Handle HTTP MCP requests - JSON-RPC over HTTP with SSE responses."""
        try:
            body = await request.json()
            method = body.get("method")
            params = body.get("params", {})
            request_id = body.get("id")

            logger.info(f"Received JSON-RPC request: {method}")

            if method == "initialize":
                # Generate session ID
                session_id = str(uuid.uuid4())
                SESSIONS[session_id] = {
                    "created_at": datetime.now().isoformat(),
                    "client_info": params.get("clientInfo", {}),
                    "protocol_version": params.get("protocolVersion", "2025-11-25")
                }

                # Get available tools
                tools_list = []

                # Try to get tools from the server module
                try:
                    module = importlib.import_module(server_name)
                    if hasattr(module, '_TOOL_DEFINITIONS'):
                        tool_definitions = module._TOOL_DEFINITIONS
                        for tool in tool_definitions:
                            tools_list.append({
                                "name": tool.name,
                                "description": tool.description
                            })
                        logger.info(f"Found {len(tools_list)} tools: {[t['name'] for t in tools_list]}")
                except Exception as e:
                    logger.warning(f"Could not load tools from {server_name}: {e}")

                result = {
                    "protocolVersion": "2025-11-25",
                    "capabilities": {
                        "tools": {
                            "listChanged": False
                        },
                        "resources": {
                            "subscribe": False,
                            "listChanged": False
                        }
                    },
                    "serverInfo": {
                        "name": "knowledge-mcp-http",
                        "version": "1.0.0"
                    },
                    "_meta": {
                        "sessionId": session_id,
                        "availableTools": tools_list
                    }
                }

                response_data = {
                    "jsonrpc": "2.0",
                    "id": request_id,
                    "result": result
                }

                async def generate_sse():
                    yield f"event: message\n"
                    yield f"data: {json.dumps(response_data)}\n\n"

                return StreamingResponse(
                    generate_sse(),
                    media_type="text/event-stream",
                    headers={
                        "MCP-Session-Id": session_id,
                        "Cache-Control": "no-cache, no-transform",
                        "Connection": "keep-alive"
                    }
                )

            elif method == "initialized":
                logger.info("Client initialized successfully")

                response_data = {
                    "jsonrpc": "2.0",
                    "id": request_id,
                    "result": {}
                }

                async def generate_sse():
                    yield f"event: message\n"
                    yield f"data: {json.dumps(response_data)}\n\n"

                return StreamingResponse(
                    generate_sse(),
                    media_type="text/event-stream",
                    headers={
                        "Cache-Control": "no-cache, no-transform",
                        "Connection": "keep-alive"
                    }
                )

            elif method == "tools/list":
                # Get tools from the MCP server
                tools_list = []

                try:
                    module = importlib.import_module(server_name)
                    if hasattr(module, '_TOOL_DEFINITIONS'):
                        tool_definitions = module._TOOL_DEFINITIONS
                        tools_list = []
                        for tool in tool_definitions:
                            tools_list.append({
                                "name": tool.name,
                                "description": tool.description,
                                "inputSchema": tool.inputSchema
                            })
                        logger.info(f"Found {len(tools_list)} tools: {[t['name'] for t in tools_list]}")
                    else:
                        logger.warning(f"No _TOOL_DEFINITIONS found in {server_name}")
                except Exception as e:
                    logger.error(f"Error loading tools: {e}")

                result = {"tools": tools_list}

                response_data = {
                    "jsonrpc": "2.0",
                    "id": request_id,
                    "result": result
                }

                async def generate_sse():
                    yield f"event: message\n"
                    yield f"data: {json.dumps(response_data)}\n\n"

                return StreamingResponse(
                    generate_sse(),
                    media_type="text/event-stream",
                    headers={
                        "Cache-Control": "no-cache, no-transform",
                        "Connection": "keep-alive"
                    }
                )

            elif method == "tools/call":
                tool_name = params.get("name")
                tool_args = params.get("arguments", {})

                if not tool_name:
                    error = {
                        "code": -32602,
                        "message": "Invalid params",
                        "data": "Missing required parameter 'name'"
                    }
                    response_data = {
                        "jsonrpc": "2.0",
                        "id": request_id,
                        "error": error
                    }
                else:
                    try:
                        # Execute the actual tool using the MCP server
                        result = await execute_tool(tool_name, tool_args, mcp_server, server_name)

                        # Handle different result formats
                        if hasattr(result, '__iter__') and not isinstance(result, (str, dict)):
                            # If result is a list of MCP content items, convert to proper format
                            content_items = []
                            for item in result:
                                if hasattr(item, 'text'):
                                    content_items.append({"type": "text", "text": item.text})
                                elif isinstance(item, dict):
                                    content_items.append(item)
                                else:
                                    content_items.append({"type": "text", "text": str(item)})
                            formatted_result = {"content": content_items}
                        elif isinstance(result, dict):
                            formatted_result = result
                        elif isinstance(result, str):
                            formatted_result = {"content": [{"type": "text", "text": result}]}
                        else:
                            formatted_result = {"content": [{"type": "text", "text": str(result)}]}

                        response_data = {
                            "jsonrpc": "2.0",
                            "id": request_id,
                            "result": formatted_result
                        }

                    except Exception as e:
                        logger.error(f"Tool execution failed: {e}", exc_info=True)
                        error = {
                            "code": -32603,
                            "message": "Tool execution error",
                            "data": str(e)
                        }
                        response_data = {
                            "jsonrpc": "2.0",
                            "id": request_id,
                            "error": error
                        }

                async def generate_sse():
                    yield f"event: message\n"
                    yield f"data: {json.dumps(response_data)}\n\n"

                return StreamingResponse(
                    generate_sse(),
                    media_type="text/event-stream",
                    headers={
                        "Cache-Control": "no-cache, no-transform",
                        "Connection": "keep-alive"
                    }
                )

            else:
                error = {
                    "code": -32601,
                    "message": "Method not found",
                    "data": f"Unknown method: {method}"
                }

                response_data = {
                    "jsonrpc": "2.0",
                    "id": request_id,
                    "error": error
                }

                async def generate_sse():
                    yield f"event: message\n"
                    yield f"data: {json.dumps(response_data)}\n\n"

                return StreamingResponse(
                    generate_sse(),
                    media_type="text/event-stream",
                    headers={
                        "Cache-Control": "no-cache, no-transform",
                        "Connection": "keep-alive"
                    }
                )

        except Exception as e:
            logger.error(f"Error handling request: {e}", exc_info=True)

            error = {
                "code": -32603,
                "message": "Internal error",
                "data": str(e)
            }

            response_data = {
                "jsonrpc": "2.0",
                "id": request_id if 'request_id' in locals() else None,
                "error": error
            }

            async def generate_sse():
                yield f"event: message\n"
                yield f"data: {json.dumps(response_data)}\n\n"

            return StreamingResponse(
                generate_sse(),
                media_type="text/event-stream",
                headers={
                    "Cache-Control": "no-cache, no-transform",
                    "Connection": "keep-alive"
                }
            )

    # Define routes
    routes = [
        Route("/jsonrpc", handle_mcp, methods=["POST"]),
        Route("/mcp", handle_mcp, methods=["POST"]),
        Route("/health", health_check, methods=["GET"]),
    ]

    # Create Starlette app with CORS middleware
    middleware = [
        Middleware(CORSMiddleware,
                  allow_origins=["*"],
                  allow_credentials=True,
                  allow_methods=["*"],
                  allow_headers=["*"])
    ]

    return Starlette(routes=routes, middleware=middleware)


def main():
    """Main entry point"""
    parser = argparse.ArgumentParser(description="HTTP/SSE MCP Server Wrapper")
    parser.add_argument("--module", required=True, help="MCP server module")
    parser.add_argument("--host", default="0.0.0.0", help="Host to bind to")
    parser.add_argument("--port", type=int, default=5555, help="Port to bind to")

    args = parser.parse_args()

    # Add server path to sys.path
    sys.path.insert(0, str(Path(__file__).parent.parent / "src"))

    try:
        mcp_server, server_attr = load_mcp_server(args.module)
        logger.info(f"Successfully loaded MCP server from {args.module}.{server_attr}")
    except Exception as e:
        logger.error(f"Failed to load MCP server from {args.module}: {e}")
        return 1

    # Create the app
    app = create_app(mcp_server, args.module)

    logger.info(f"Starting HTTP/SSE MCP Server on {args.host}:{args.port}")

    uvicorn.run(
        app,
        host=args.host,
        port=args.port,
        log_level="info",
        access_log=True
    )

if __name__ == "__main__":
    main()