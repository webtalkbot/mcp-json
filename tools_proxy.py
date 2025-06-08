#!/usr/bin/env python3
"""
tools_proxy.py - MCP Tools Proxy for aggregating tools from multiple servers
"""

import asyncio
import logging
from typing import Dict, List, Optional, Any
import json
from dataclasses import dataclass

logger = logging.getLogger(__name__)

@dataclass
class ToolInfo:
    """Information about a tool"""
    name: str
    server_name: str
    description: str
    input_schema: Dict[str, Any]
    original_name: str  # Original name without server prefix
    
class ToolsProxy:
    """Proxy for aggregating and routing tools from multiple MCP servers"""
    
    def __init__(self, process_manager):
        self.process_manager = process_manager
        self.tools_cache: Dict[str, ToolInfo] = {}
        self.server_tools_cache: Dict[str, List[Dict]] = {}
        self.cache_timeout = 60  # Cache for 60 seconds
        self.last_cache_update = 0
        self.lock = asyncio.Lock()
    
    async def get_all_tools(self, force_refresh: bool = False) -> List[Dict[str, Any]]:
        """
        🔧 FIXED: Aggregate tools from all running servers with optimized cache handling
        Returns MCP-compliant tools list with server prefixes for conflict resolution
        """
        import time
        current_time = time.time()
        
        # 🔧 FIXED: Check cache outside lock first to minimize lock contention
        if not force_refresh:
            async with self.lock:
                if (current_time - self.last_cache_update) < self.cache_timeout:
                    logger.debug("Tools: Using cached tools list")
                    return self._get_cached_tools_list()  # Return copy
        
        # 🔧 FIXED: Refresh logic with double-check pattern
        async with self.lock:
            # Double-check inside lock to prevent race conditions
            if not force_refresh and (current_time - self.last_cache_update) < self.cache_timeout:
                logger.debug("Tools: Using cached tools list (double-check)")
                return self._get_cached_tools_list()
            
            logger.info("Tools: Refreshing tools cache from all running servers")
            
            # Clear old cache
            self.tools_cache.clear()
            self.server_tools_cache.clear()
            
            # Get all running servers
            with self.process_manager.lock:
                running_servers = list(self.process_manager.processes.keys())
            
            # Aggregate tools from all servers
            aggregated_tools = []
            server_conflicts = {}
            
            for server_name in running_servers:
                try:
                    mcp_process = self.process_manager.processes.get(server_name)
                    if not mcp_process or not mcp_process.is_running() or not mcp_process.initialized:
                        logger.debug(f"Tools: Skipping server {server_name} - not ready")
                        continue
                    
                    # Get tools from server
                    logger.debug(f"Tools: Getting tools from server {server_name}")
                    response = await self.process_manager.send_request_to_server(server_name, "tools/list")
                    
                    if "result" in response and "tools" in response["result"]:
                        server_tools = response["result"]["tools"]
                        self.server_tools_cache[server_name] = server_tools
                        
                        logger.info(f"Tools: Server {server_name} has {len(server_tools)} tools")
                        
                        # Process each tool
                        for tool in server_tools:
                            original_name = tool.get("name", "unknown")
                            tool_description = tool.get("description", "")
                            tool_schema = tool.get("inputSchema", {})
                            
                            # Check for naming conflicts
                            if original_name in server_conflicts:
                                server_conflicts[original_name].append(server_name)
                            else:
                                server_conflicts[original_name] = [server_name]
                            
                            # Create namespaced tool name (server__toolname)
                            namespaced_name = f"{server_name}__{original_name}"
                            
                            # Create tool info
                            tool_info = ToolInfo(
                                name=namespaced_name,
                                server_name=server_name,
                                description=f"[{server_name}] {tool_description}",
                                input_schema=tool_schema,
                                original_name=original_name
                            )
                            
                            # Store in cache
                            self.tools_cache[namespaced_name] = tool_info
                            
                            # Create MCP-compliant tool definition
                            mcp_tool = {
                                "name": namespaced_name,
                                "description": f"[{server_name}] {tool_description}",
                                "inputSchema": tool_schema
                            }
                            
                            aggregated_tools.append(mcp_tool)
                    
                    else:
                        logger.warning(f"Tools: Server {server_name} returned invalid tools/list response")
                
                except Exception as e:
                    logger.error(f"Tools: Error getting tools from server {server_name}: {e}")
                    continue
            
            # Log conflicts
            conflicts = {name: servers for name, servers in server_conflicts.items() if len(servers) > 1}
            if conflicts:
                logger.warning(f"Tools: Name conflicts detected and resolved with server prefixes: {conflicts}")
            
            # Update cache timestamp
            self.last_cache_update = current_time
            
            logger.info(f"Tools: Aggregated {len(aggregated_tools)} tools from {len(running_servers)} servers")
            return aggregated_tools
    
    def _get_cached_tools_list(self) -> List[Dict[str, Any]]:
        """Get tools list from cache"""
        tools_list = []
        for tool_info in self.tools_cache.values():
            mcp_tool = {
                "name": tool_info.name,
                "description": tool_info.description,
                "inputSchema": tool_info.input_schema
            }
            tools_list.append(mcp_tool)
        return tools_list
    
    async def call_tool(self, tool_name: str, arguments: Dict[str, Any]) -> Dict[str, Any]:
        """
        Route tool call to the appropriate server
        Handles namespaced tool names (server__toolname)
        """
        logger.info(f"Tools: Calling tool {tool_name}")
        
        # Check if tool exists in cache
        if tool_name not in self.tools_cache:
            # Try to refresh cache
            await self.get_all_tools(force_refresh=True)
            
            if tool_name not in self.tools_cache:
                logger.error(f"Tools: Tool {tool_name} not found")
                return {
                    "jsonrpc": "2.0",
                    "error": {
                        "code": -32601,
                        "message": f"Tool '{tool_name}' not found. Available tools: {list(self.tools_cache.keys())}"
                    }
                }
        
        tool_info = self.tools_cache[tool_name]
        server_name = tool_info.server_name
        original_tool_name = tool_info.original_name
        
        logger.info(f"Tools: Routing tool {tool_name} -> server {server_name} (original: {original_tool_name})")
        
        try:
            # Check if server is still running
            mcp_process = self.process_manager.processes.get(server_name)
            if not mcp_process or not mcp_process.is_running():
                logger.error(f"Tools: Server {server_name} is not running")
                return {
                    "jsonrpc": "2.0",
                    "error": {
                        "code": -32000,
                        "message": f"Server '{server_name}' is not running"
                    }
                }
            
            # Call the tool on the server using original name
            response = await self.process_manager.send_request_to_server(
                server_name, 
                "tools/call", 
                {
                    "name": original_tool_name,
                    "arguments": arguments
                }
            )
            
            logger.info(f"Tools: Tool {tool_name} executed successfully on {server_name}")
            return response
            
        except Exception as e:
            logger.error(f"Tools: Error calling tool {tool_name} on server {server_name}: {e}")
            return {
                "jsonrpc": "2.0",
                "error": {
                    "code": -32000,
                    "message": f"Error executing tool '{tool_name}': {str(e)}"
                }
            }
    
    async def get_server_for_tool(self, tool_name: str) -> Optional[str]:
        """Get the server name that provides a specific tool"""
        if tool_name in self.tools_cache:
            return self.tools_cache[tool_name].server_name
        
        # Try to refresh cache
        await self.get_all_tools(force_refresh=True)
        
        if tool_name in self.tools_cache:
            return self.tools_cache[tool_name].server_name
        
        return None
    
    async def get_tools_by_server(self, server_name: str) -> List[Dict[str, Any]]:
        """Get all tools for a specific server"""
        if server_name not in self.server_tools_cache:
            await self.get_all_tools(force_refresh=True)
        
        return self.server_tools_cache.get(server_name, [])
    
    def get_cache_stats(self) -> Dict[str, Any]:
        """Get cache statistics"""
        import time
        return {
            "cached_tools": len(self.tools_cache),
            "cached_servers": len(self.server_tools_cache),
            "last_update": self.last_cache_update,
            "cache_age": time.time() - self.last_cache_update,
            "cache_timeout": self.cache_timeout
        }
    
    async def invalidate_cache(self):
        """Manually invalidate the cache"""
        async with self.lock:
            self.tools_cache.clear()
            self.server_tools_cache.clear()
            self.last_cache_update = 0
            logger.info("Tools: Cache manually invalidated")
