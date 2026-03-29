"""
MCP Index Scanner - Scans MCP servers and indexes their tools

This module provides functionality to:
- Scan /srv/latvian_mcp/servers/ for MCP server directories
- Parse server.py files to extract tool definitions
- Index servers and tools in Supabase
- Track changes and versions
"""

import re
import ast
import time
import logging
from pathlib import Path
from typing import Dict, List, Any, Tuple
from datetime import datetime

logger = logging.getLogger(__name__)


class MCPIndexScanner:
    """Scanner for MCP servers and their tools."""

    def __init__(self, db_client):
        """Initialize scanner with database client."""
        self.db = db_client
        self.servers_path = Path("/srv/latvian_mcp/servers")

    def scan_all_servers(self, triggered_by: str = "manual", config_filter: bool = True, config_path: str = None) -> Dict[str, Any]:
        """
        Scan all MCP servers and index their tools.

        Args:
            triggered_by: Source of scan (manual, cron, deployment, etc.)
            config_filter: If True, scan only servers configured in Claude config (default: True)
            config_path: Path to .claude.json (default: ~/.claude.json)

        Returns:
            Dict with scan results
        """
        start_time = time.time()
        logger.info("Starting MCP index scan...")

        # Get existing servers from DB
        existing_servers = self._get_existing_servers()
        existing_server_ids = {s["server_id"] for s in existing_servers}

        # Get configured servers if filtering enabled
        configured_servers = None
        if config_filter:
            configured_servers = self._read_claude_config(config_path)
            if configured_servers:
                logger.info(f"Config filtering enabled: scanning only {len(configured_servers)} configured servers")
                logger.debug(f"Configured servers: {configured_servers}")

        scanned_servers = []
        all_tools = []
        errors = []

        # Scan each server directory
        for server_dir in self.servers_path.iterdir():
            if not server_dir.is_dir() or server_dir.name.startswith('.'):
                continue

            # Skip if not in configured servers (when filtering enabled)
            if configured_servers is not None and server_dir.name not in configured_servers:
                logger.debug(f"Skipping {server_dir.name} (not in configuration)")
                continue

            try:
                server_data, tools = self._scan_server(server_dir)
                if server_data:
                    self._upsert_server(server_data)
                    self._upsert_tools(tools)
                    scanned_servers.append(server_data["server_id"])
                    all_tools.extend(tools)
            except Exception as e:
                error_msg = f"Error scanning {server_dir.name}: {str(e)}"
                logger.error(error_msg)
                errors.append(error_msg)

        # Mark removed servers as inactive
        removed_servers = existing_server_ids - set(scanned_servers)
        for server_id in removed_servers:
            self._mark_server_inactive(server_id)

        # Mark non-configured servers as "available" (not "active")
        if configured_servers is not None:
            non_configured = existing_server_ids - set(configured_servers)
            for server_id in non_configured:
                if server_id not in removed_servers:  # Don't change removed servers
                    self._mark_server_available(server_id)

        # Calculate changes
        changes = {
            "added": [s for s in scanned_servers if s not in existing_server_ids],
            "removed": list(removed_servers),
            "modified": []  # TODO: Implement change detection
        }

        # Record scan version
        scan_duration = int((time.time() - start_time) * 1000)
        version = self._record_scan_version(
            len(scanned_servers),
            len(all_tools),
            changes,
            scan_duration,
            errors,
            triggered_by
        )

        logger.info(f"Scan complete: {len(scanned_servers)} servers, {len(all_tools)} tools")

        return {
            "version_id": version,
            "servers_scanned": len(scanned_servers),
            "tools_indexed": len(all_tools),
            "changes": changes,
            "scan_duration_ms": scan_duration,
            "errors": errors
        }

    def _scan_server(self, server_dir: Path) -> Tuple[Dict, List[Dict]]:
        """
        Scan a single MCP server directory.

        Returns:
            (server_data, tools_list)
        """
        server_id = server_dir.name
        logger.info(f"Scanning server: {server_id}")

        # Find server.py file
        server_file = self._find_server_file(server_dir, server_id)
        if not server_file or not server_file.exists():
            logger.warning(f"No server.py found for {server_id}")
            return None, []

        # Parse tools from server.py
        tools = self._parse_server_tools(server_file, server_id)

        # Detect Python version
        python_version = self._detect_python_version(server_dir)

        # Extract description from docstring
        description = self._extract_server_description(server_file)

        # Server data
        server_data = {
            "server_id": server_id,
            "server_name": server_id,
            "server_path": str(server_dir),
            "python_version": python_version,
            "status": "active",
            "venv_path": str(server_dir / "venv"),
            "tool_count": len(tools),
            "description": description,
            "tags": self._infer_tags(server_id),
            "last_scanned": datetime.utcnow().isoformat()
        }

        return server_data, tools

    def _find_server_file(self, server_dir: Path, server_id: str) -> Path:
        """Find the server.py file for a given server."""
        # Try src/<server_module>/server.py
        module_name = server_id.replace('-', '_')
        server_file = server_dir / "src" / module_name / "server.py"

        if not server_file.exists():
            # Try alternative locations
            for pattern in ["**/server.py", "server.py"]:
                matches = list(server_dir.glob(pattern))
                if matches:
                    return matches[0]

        return server_file

    def _parse_server_tools(self, server_file: Path, server_id: str) -> List[Dict]:
        """
        Parse tool definitions from server.py file.

        Extracts tools from @app.list_tools() function.
        """
        tools = []

        try:
            with open(server_file, 'r') as f:
                content = f.read()

            # Parse Python AST
            tree = ast.parse(content)

            # Find list_tools function (can be async or sync)
            for node in ast.walk(tree):
                if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name == 'list_tools':
                    # Extract tool definitions from return statement
                    tools = self._extract_tools_from_ast(node, server_id)
                    break

            logger.info(f"Found {len(tools)} tools in {server_id}")

        except Exception as e:
            logger.error(f"Error parsing {server_file}: {e}")

        return tools

    def _extract_tools_from_ast(self, func_node: ast.FunctionDef, server_id: str) -> List[Dict]:
        """Extract tool definitions from list_tools function AST."""
        tools = []

        # Find return statement
        for node in ast.walk(func_node):
            if isinstance(node, ast.Return) and isinstance(node.value, ast.List):
                # Parse each Tool(...) call
                for tool_node in node.value.elts:
                    if isinstance(tool_node, ast.Call):
                        tool_data = self._parse_tool_call(tool_node, server_id)
                        if tool_data:
                            tools.append(tool_data)

        return tools

    def _parse_tool_call(self, tool_node: ast.Call, server_id: str) -> Dict:
        """Parse a single types.Tool(...) call."""
        tool_data = {
            "server_id": server_id,
            "tags": [],
            "use_cases": []
        }

        # Extract keyword arguments
        for keyword in tool_node.keywords:
            key = keyword.arg
            value = keyword.value

            if key == "name" and isinstance(value, ast.Constant):
                tool_name = value.value
                tool_data["tool_name"] = tool_name
                tool_data["tool_id"] = f"{server_id}_{tool_name}"

                # Generate full MCP name (use server_id AS-IS to match Claude Code's namespace)
                server_prefix = server_id
                tool_data["full_name"] = f"mcp__{server_prefix}__{tool_name}"

            elif key == "description" and isinstance(value, ast.Constant):
                tool_data["description"] = value.value

            elif key == "inputSchema" and isinstance(value, ast.Dict):
                # Convert AST dict to JSON, handling f-strings and other complex expressions
                try:
                    tool_data["input_schema"] = self._ast_dict_to_python(value)
                except Exception as e:
                    logger.warning(f"Could not parse inputSchema for {tool_data.get('tool_name', 'unknown')}: {e}")
                    # Skip this tool if we can't parse its schema
                    return None

        # Add derived fields
        if "tool_name" in tool_data and "description" in tool_data:
            tool_data["category"] = self._categorize_tool(tool_data)
            tool_data["tags"] = self._extract_tool_tags(tool_data)
            tool_data["usage_example"] = self._generate_usage_example(tool_data)
            return tool_data

        return None

    def _ast_dict_to_python(self, ast_dict: ast.Dict) -> Dict:
        """
        Convert an AST Dict node to a Python dict, handling f-strings and other expressions.

        This safely evaluates dictionary literals, converting:
        - String constants and f-strings to strings
        - Number constants to numbers
        - Boolean/None constants
        - Lists and nested dicts
        """
        result = {}

        for key_node, value_node in zip(ast_dict.keys, ast_dict.values):
            # Extract key
            if isinstance(key_node, ast.Constant):
                key = key_node.value
            else:
                continue  # Skip non-constant keys

            # Extract and convert value
            result[key] = self._ast_value_to_python(value_node)

        return result

    def _ast_value_to_python(self, node: ast.expr) -> Any:
        """Convert an AST value node to Python object, handling f-strings."""
        if isinstance(node, ast.Constant):
            return node.value

        elif isinstance(node, ast.JoinedStr):
            # Handle f-strings: convert to plain string by joining all values
            # For f-strings, we extract the literal string parts and skip the formatted values
            # This is a safe approach: we get "Path to Dockerfile (default: )" instead of the full value
            # but at least we get valid data instead of failing
            parts = []
            for value in node.values:
                if isinstance(value, ast.Constant):
                    parts.append(str(value.value))
                elif isinstance(value, ast.FormattedValue):
                    # For formatted values, we can't evaluate them at parse time
                    # Use a placeholder
                    parts.append("{...}")
            return "".join(parts)

        elif isinstance(node, ast.Dict):
            return self._ast_dict_to_python(node)

        elif isinstance(node, ast.List):
            return [self._ast_value_to_python(elt) for elt in node.elts]

        elif isinstance(node, ast.Tuple):
            return tuple(self._ast_value_to_python(elt) for elt in node.elts)

        elif isinstance(node, ast.Name):
            # Handle simple name references like True, False, None
            if node.id == "True":
                return True
            elif node.id == "False":
                return False
            elif node.id == "None":
                return None
            else:
                # Other variables: we can't evaluate them
                return f"<variable: {node.id}>"

        else:
            # For other expression types, return a placeholder
            return f"<expression: {type(node).__name__}>"

    def _categorize_tool(self, tool_data: Dict) -> str:
        """Categorize tool based on name and description."""
        name = tool_data.get("tool_name", "").lower()
        desc = tool_data.get("description", "").lower()

        # Search tools
        if any(word in name or word in desc for word in ["search", "find", "query", "list"]):
            return "search"

        # Storage tools
        if any(word in name or word in desc for word in ["add", "insert", "store", "save", "put", "upload"]):
            return "storage"

        # Processing tools
        if any(word in name or word in desc for word in ["process", "transform", "transcribe", "generate", "normalize"]):
            return "processing"

        # Orchestration tools
        if any(word in name or word in desc for word in ["job", "recipe", "plan", "workflow", "queue"]):
            return "orchestration"

        # Monitoring tools
        if any(word in name or word in desc for word in ["status", "health", "monitor", "check", "usage"]):
            return "monitoring"

        # Admin tools
        if any(word in name or word in desc for word in ["restart", "stop", "kill", "deploy", "scan", "rebuild"]):
            return "admin"

        # Analysis tools
        if any(word in name or word in desc for word in ["analyze", "evaluate", "audit", "compare", "metrics"]):
            return "analysis"

        return "general"

    def _extract_tool_tags(self, tool_data: Dict) -> List[str]:
        """Extract relevant tags from tool name and description."""
        tags = []
        name = tool_data.get("tool_name", "").lower()
        desc = tool_data.get("description", "").lower()
        server_id = tool_data.get("server_id", "")

        # Add server type as tag
        if "knowledge" in server_id:
            tags.append("knowledge")
        if "search" in server_id:
            tags.append("search")
        if "storage" in server_id or "r2" in server_id:
            tags.append("storage")
        if "orchestrator" in server_id:
            tags.append("orchestration")
        if "system" in server_id or "ops" in server_id:
            tags.append("system")

        # Add functional tags
        if "kb" in name or "knowledge base" in desc:
            tags.append("kb")
        if "journal" in name:
            tags.append("journal")
        if "research" in name:
            tags.append("research")
        if "mcp_index" in name or "mcp index" in desc:
            tags.append("mcp-index")

        return list(set(tags))  # Remove duplicates

    def _generate_usage_example(self, tool_data: Dict) -> str:
        """Generate a usage example for the tool."""
        full_name = tool_data.get("full_name", "")
        tool_name = tool_data.get("tool_name", "")

        # Generate basic example based on common patterns
        if "search" in tool_name:
            return f'{full_name}("your search query")'
        elif "add" in tool_name or "create" in tool_name:
            return f'{full_name}(data={{...}})'
        elif "get" in tool_name or "list" in tool_name:
            return f'{full_name}()'
        else:
            return f'{full_name}(...)'

    def _detect_python_version(self, server_dir: Path) -> str:
        """Detect Python version used by server."""
        venv_python = server_dir / "venv" / "bin" / "python"

        if venv_python.exists():
            try:
                import subprocess
                result = subprocess.run(
                    [str(venv_python), "--version"],
                    capture_output=True,
                    text=True,
                    timeout=5
                )
                if result.returncode == 0:
                    # Parse "Python 3.13.0" -> "3.13"
                    version = result.stdout.strip().split()[1]
                    major, minor = version.split('.')[:2]
                    return f"{major}.{minor}"
            except Exception:
                pass

        return "unknown"

    def _extract_server_description(self, server_file: Path) -> str:
        """Extract description from server.py module docstring."""
        try:
            with open(server_file, 'r') as f:
                content = f.read()

            tree = ast.parse(content)
            docstring = ast.get_docstring(tree)

            if docstring:
                # Return first line of docstring
                return docstring.split('\n')[0].strip()

        except Exception:
            pass

        return ""

    def _infer_tags(self, server_id: str) -> List[str]:
        """Infer tags from server ID."""
        tags = []

        if "knowledge" in server_id:
            tags.extend(["knowledge", "kb", "search"])
        if "orchestrator" in server_id:
            tags.extend(["orchestration", "workflow", "jobs"])
        if "system" in server_id or "ops" in server_id:
            tags.extend(["system", "monitoring", "admin"])
        if "r2" in server_id or "storage" in server_id:
            tags.extend(["storage", "cloud", "r2"])
        if "search" in server_id:
            tags.extend(["search", "discovery"])
        if "provenance" in server_id:
            tags.extend(["provenance", "lineage", "tracking"])
        if "ingest" in server_id:
            tags.extend(["ingestion", "processing", "audio"])
        if "asr" in server_id:
            tags.extend(["transcription", "audio", "whisper"])
        if "tts" in server_id:
            tags.extend(["tts", "speech", "synthesis", "xtts"])

        return list(set(tags))

    def _get_existing_servers(self) -> List[Dict]:
        """Get existing servers from database."""
        try:
            result = self.db.table("mcp_servers").select("*").execute()
            return result.data
        except Exception as e:
            logger.warning(f"Could not fetch existing servers: {e}")
            return []

    def _upsert_server(self, server_data: Dict):
        """Insert or update server in database."""
        try:
            self.db.table("mcp_servers")\
                .upsert(server_data, on_conflict="server_id")\
                .execute()
        except Exception as e:
            logger.error(f"Error upserting server {server_data['server_id']}: {e}")
            raise

    def _upsert_tools(self, tools: List[Dict]):
        """Insert or update tools in database."""
        for tool in tools:
            try:
                self.db.table("mcp_tools")\
                    .upsert(tool, on_conflict="tool_id")\
                    .execute()
            except Exception as e:
                logger.error(f"Error upserting tool {tool.get('tool_id')}: {e}")

    def _mark_server_inactive(self, server_id: str):
        """Mark a server as inactive."""
        try:
            self.db.table("mcp_servers")\
                .update({"status": "inactive"})\
                .eq("server_id", server_id)\
                .execute()
        except Exception as e:
            logger.error(f"Error marking server {server_id} inactive: {e}")

    def _mark_server_available(self, server_id: str):
        """Mark a server as available (installed but not configured)."""
        try:
            self.db.table("mcp_servers")\
                .update({"status": "available"})\
                .eq("server_id", server_id)\
                .execute()
        except Exception as e:
            logger.error(f"Error marking server {server_id} available: {e}")

    def _read_claude_config(self, config_path: str = None) -> List[str]:
        """
        Read Claude Code configuration from multiple sources and merge.

        Reads servers from:
        1. ~/.claude.json (user-level Tier 1)
        2. .mcp.json files (project-level Tier 2, search from CWD upward)
        3. /srv/latvian_mcp/config/mcp_servers.master.json (fallback)

        Args:
            config_path: Path to user config (default: ~/.claude.json)

        Returns:
            List of ALL configured server IDs from all sources, or None if no configs found
        """
        import json
        from pathlib import Path
        import os

        configured_servers = set()  # Use set to deduplicate
        sources_found = []

        # 1. Read user-level config (~/.claude.json)
        if config_path is None:
            user_config_path = Path.home() / ".claude.json"
        else:
            user_config_path = Path(config_path)

        if user_config_path.exists():
            try:
                with open(user_config_path, 'r') as f:
                    config = json.load(f)
                mcp_servers = config.get("mcpServers", {})
                tier1_count = len(mcp_servers)
                configured_servers.update(mcp_servers.keys())
                sources_found.append(f"Tier 1: {tier1_count} servers from {user_config_path}")
                logger.info(f"Found {tier1_count} Tier 1 servers in {user_config_path}")
            except Exception as e:
                logger.error(f"Error reading user config: {e}")

        # 2. Read project-level configs (.mcp.json)
        # Search from CWD upward to repository root
        current_dir = Path.cwd()
        project_configs_found = 0
        while current_dir != current_dir.parent:  # Stop at filesystem root
            project_config = current_dir / ".mcp.json"
            if project_config.exists():
                try:
                    with open(project_config, 'r') as f:
                        config = json.load(f)
                    mcp_servers = config.get("mcpServers", {})
                    tier2_count = len(mcp_servers)
                    configured_servers.update(mcp_servers.keys())
                    sources_found.append(f"Tier 2: {tier2_count} servers from {project_config}")
                    logger.info(f"Found {tier2_count} Tier 2 servers in {project_config}")
                    project_configs_found += 1
                except Exception as e:
                    logger.error(f"Error reading project config {project_config}: {e}")

            # Stop at git repository root
            if (current_dir / ".git").exists():
                break
            current_dir = current_dir.parent

        # 3. Fallback: Read master config if available
        master_config = Path("/srv/latvian_mcp/config/mcp_servers.master.json")
        if master_config.exists():
            try:
                with open(master_config, 'r') as f:
                    config = json.load(f)
                # Filter out comment entries and metadata
                master_servers = [k for k in config.keys() if not k.startswith("_")]
                # Only use master config as supplement, not replacement
                # Add servers that aren't already discovered
                new_servers = set(master_servers) - configured_servers
                if new_servers:
                    configured_servers.update(new_servers)
                    sources_found.append(f"Master: {len(new_servers)} additional servers from {master_config}")
                    logger.info(f"Found {len(new_servers)} additional servers in master config")
            except Exception as e:
                logger.error(f"Error reading master config: {e}")

        if not configured_servers:
            logger.warning("No configured servers found in any config source")
            return None

        result = sorted(list(configured_servers))  # Convert to sorted list
        logger.info(f"Total configured servers from all sources: {len(result)}")
        logger.info(f"Configuration sources: {', '.join(sources_found)}")
        return result

    def _record_scan_version(self, servers_scanned: int, tools_indexed: int,
                            changes: Dict, scan_duration: int,
                            errors: List[str], triggered_by: str) -> int:
        """Record scan version in database."""
        try:
            result = self.db.table("mcp_index_versions").insert({
                "servers_scanned": servers_scanned,
                "tools_indexed": tools_indexed,
                "changes": changes,
                "scan_duration_ms": scan_duration,
                "errors": errors,
                "triggered_by": triggered_by
            }).execute()

            if result.data:
                return result.data[0]["version_id"]

        except Exception as e:
            logger.error(f"Error recording scan version: {e}")

        return -1

    def search_tools(self, query: str, category: str = None, limit: int = 20) -> List[Dict]:
        """
        Search for tools using PostgreSQL full-text search.

        Args:
            query: Search query
            category: Optional category filter
            limit: Maximum results to return

        Returns:
            List of matching tools with relevance scores
        """
        try:
            # Use full-text search on search_vector column
            # For now, using ILIKE as a fallback since Supabase client doesn't expose ts_rank easily
            # Future: Use raw SQL for better full-text search with ranking

            query_builder = self.db.table("mcp_tools")\
                .select("*, mcp_servers!inner(server_name, status)")\
                .eq("mcp_servers.status", "active")

            # Build OR conditions for each search term
            search_terms = query.lower().split()
            if search_terms:
                or_conditions = []
                for term in search_terms:
                    or_conditions.append(f"tool_name.ilike.%{term}%")
                    or_conditions.append(f"description.ilike.%{term}%")

                query_builder = query_builder.or_(",".join(or_conditions))

            # Filter by category if provided
            if category:
                query_builder = query_builder.eq("category", category)

            # Execute query
            result = query_builder.limit(limit).execute()

            return result.data

        except Exception as e:
            logger.error(f"Error searching tools: {e}")
            return []

    def get_server_tools(self, server_id: str) -> Dict:
        """Get all tools for a specific server."""
        try:
            # Get server info
            server_result = self.db.table("mcp_servers")\
                .select("*")\
                .eq("server_id", server_id)\
                .execute()

            if not server_result.data:
                return None

            # Get tools
            tools_result = self.db.table("mcp_tools")\
                .select("*")\
                .eq("server_id", server_id)\
                .execute()

            return {
                "server": server_result.data[0],
                "tools": tools_result.data
            }

        except Exception as e:
            logger.error(f"Error getting server tools: {e}")
            return None

    def get_tool_details(self, tool_name: str) -> Dict:
        """Get detailed information about a specific tool."""
        try:
            # Try exact match on tool_name
            result = self.db.table("mcp_tools")\
                .select("*, mcp_servers(server_name, status)")\
                .eq("tool_name", tool_name)\
                .execute()

            if result.data:
                return result.data[0]

            # Try match on full_name
            result = self.db.table("mcp_tools")\
                .select("*, mcp_servers(server_name, status)")\
                .ilike("full_name", f"%{tool_name}%")\
                .execute()

            if result.data:
                return result.data[0]

            return None

        except Exception as e:
            logger.error(f"Error getting tool details: {e}")
            return None
