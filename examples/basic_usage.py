#!/usr/bin/env python3
"""
Basic usage example for Knowledge MCP server.
This demonstrates how to interact with the server programmatically.
"""

import asyncio
import json
from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client


async def main():
    """Basic example of using Knowledge MCP."""

    # Connect to the Knowledge MCP server
    server_params = StdioServerParameters(
        command="knowledge-mcp",
        env={
            "KNOWLEDGE_LOG_LEVEL": "INFO",
            "KNOWLEDGE_DATA_DIR": "./example_data"
        }
    )

    async with stdio_client(server_params) as (read, write):
        async with ClientSession(read, write) as session:

            # Initialize the session
            await session.initialize()

            # List available tools
            tools = await session.list_tools()
            print("Available tools:")
            for tool in tools.tools:
                print(f"  - {tool.name}: {tool.description}")

            # Add a knowledge entry
            result = await session.call_tool(
                "kb_add",
                {
                    "content": "Python is a high-level programming language known for its simplicity and readability.",
                    "metadata": {
                        "source": "example",
                        "topic": "programming",
                        "language": "python"
                    }
                }
            )
            print(f"Added entry: {result}")

            # Search for knowledge
            search_result = await session.call_tool(
                "kb_search",
                {
                    "query": "programming language",
                    "limit": 5
                }
            )
            print(f"Search results: {search_result}")

            # Add a research note
            note_result = await session.call_tool(
                "research_add_note",
                {
                    "experiment_id": "python_study",
                    "content": "Python's simplicity makes it great for beginners.",
                    "note_type": "observation"
                }
            )
            print(f"Added research note: {note_result}")

            # Add a journal entry
            journal_result = await session.call_tool(
                "journal_append",
                {
                    "content": "Today I learned about the Knowledge MCP server capabilities.",
                    "tags": ["learning", "mcp", "knowledge-management"]
                }
            )
            print(f"Added journal entry: {journal_result}")


if __name__ == "__main__":
    asyncio.run(main())