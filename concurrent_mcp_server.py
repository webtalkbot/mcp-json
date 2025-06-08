#!/usr/bin/env python3
"""
concurrent_mcp_server.py - Thread-safe configuration MCP Server  
Optimized for high concurrency and multiple simultaneous users
"""

import asyncio
import json
import sys
import os
import aiohttp
import glob
import threading
import time
import random
from typing import Any, Dict, List, Optional
from pathlib import Path
from dataclasses import dataclass
from concurrent.futures import ThreadPoolExecutor
import aiofiles
import weakref
from functools import wraps
try:
    from security_manager import get_security_manager
except ImportError:
    # Fallback if security_manager is not available
    def get_security_manager(config_dir="."):
        class MockSecurityManager:
            async def initialize(self, session): pass
            async def get_security_context(self, server_name): 
                from security_providers import SecurityContext
                return SecurityContext({}, {}, {}, {'provider': 'fallback'})
            def apply_security_to_request(self, server_name, context, headers, params):
                return {'headers': headers, 'params': params, 'cookies': {}}
        return MockSecurityManager()

# MCP imports
try:
    from mcp.server import Server, NotificationOptions
    from mcp.server.models import InitializationOptions
    from mcp.types import Resource, Tool, TextContent
    import mcp.types as types
    from mcp.server.stdio import stdio_server
except ImportError:
    sys.stderr.write("Missing MCP library. Install: pip install mcp\n")
    sys.exit(1)

# HTTP Retry decorator for network operations
def http_retry(max_retries: int = 3, base_delay: float = 1.0, backoff_factor: float = 2.0):
    """Decorator for automatic retry of HTTP operations on network errors"""
    def decorator(func):
        @wraps(func)
        async def wrapper(*args, **kwargs):
            last_exception = None
            
            for attempt in range(max_retries + 1):
                try:
                    return await func(*args, **kwargs)
                    
                except (aiohttp.ClientError, asyncio.TimeoutError) as e:
                    last_exception = e
                    
                    # Determines if error is retry-able
                    retry_conditions = [
                        isinstance(e, asyncio.TimeoutError),
                        isinstance(e, aiohttp.ClientTimeout),
                        isinstance(e, aiohttp.ClientConnectionError),
                        isinstance(e, aiohttp.ClientConnectorError),
                        isinstance(e, aiohttp.ServerTimeoutError),
                        isinstance(e, aiohttp.ServerDisconnectedError),
                        # 🔧 FIXED: Bezpečnejšia kontrola HTTP status codes 5xx (server errors)
                        (hasattr(e, 'status') and isinstance(getattr(e, 'status', None), int) and e.status >= 500),
                        # For network-level errors
                        "Connection" in str(e),
                        "Timeout" in str(e),
                        "Network" in str(e)
                    ]
                    
                    if attempt < max_retries and any(retry_conditions):
                        # Exponential backoff with jitter
                        delay = base_delay * (backoff_factor ** attempt) + random.uniform(0, 0.5)
                        print(f"🔄 HTTP retry attempt {attempt + 1}/{max_retries + 1} po {delay:.1f}s: {type(e).__name__}: {e}")
                        await asyncio.sleep(delay)
                        continue
                    
                    # If not retry-able or we've exhausted attempts
                    raise
                    
                except Exception as e:
                    # For other types of exceptions, don't continue with retry
                    raise
            
            # If we've exhausted all attempts
            if last_exception:
                raise last_exception
                
        return wrapper
    return decorator

@dataclass
class MCPServerConfig:
    """Thread-safe MCP server configuration"""
    name: str
    endpoints: Dict[str, Any]
    headers: Dict[str, str]
    last_modified: float
    
    @classmethod
    async def load_from_files_async(cls, server_name: str, config_dir: str = ".") -> Optional['MCPServerConfig']:
        """Asynchronously loads server configuration from files in servers/{server_name}/ structure"""
        server_dir = os.path.join(config_dir, "servers", server_name)
        endpoints_file = os.path.join(server_dir, f"{server_name}_endpoints.json")
        headers_file = os.path.join(server_dir, f"{server_name}_headers.json")
        
        # Checks existence of files
        if not os.path.exists(endpoints_file):
            print(f"❌ ERROR: File {endpoints_file} does not exist")
            return None
        
        try:
            # Asynchronous file reading
            async with aiofiles.open(endpoints_file, 'r', encoding='utf-8') as f:
                endpoints_content = await f.read()
                endpoints = json.loads(endpoints_content)
            
            # Loads headers (optional)
            headers = {}
            if os.path.exists(headers_file):
                async with aiofiles.open(headers_file, 'r', encoding='utf-8') as f:
                    headers_content = await f.read()
                    headers = json.loads(headers_content)
            
            # Gets last modification timestamp
            endpoints_mtime = os.path.getmtime(endpoints_file)
            headers_mtime = os.path.getmtime(headers_file) if os.path.exists(headers_file) else 0
            last_modified = max(endpoints_mtime, headers_mtime)
            
            return cls(
                name=server_name,
                endpoints=endpoints,
                headers=headers,
                last_modified=last_modified
            )
            
        except json.JSONDecodeError as e:
            print(f"❌ ERROR: Error parsing JSON files for server{server_name}: {e}")
            return None
        except Exception as e:
            print(f"❌ ERROR: Error loading server configuration {server_name}: {e}")
            return None

class ThreadSafeConfigManager:
    """Thread-safe management of MCP server configurations with caching"""
    
    def __init__(self, config_dir: str = ".", cache_ttl: int = 300):
        self.config_dir = config_dir
        self.cache_ttl = cache_ttl  # 5 minutes cache
        self._cache = {}
        self._cache_lock = None
        self._file_locks = {}
        self._initialized = False
    
    async def _ensure_initialized(self):
        """Ensures that async components are properly initialized"""
        if not self._initialized:
            self._cache_lock = asyncio.Lock()
            self._initialized = True
        
    async def _get_file_lock(self, server_name: str) -> asyncio.Lock:
        """Gets or creates lock for specific server"""
        if server_name not in self._file_locks:
            self._file_locks[server_name] = asyncio.Lock()
        return self._file_locks[server_name]
    
    async def discover_servers(self) -> List[str]:
        """Finds all available MCP servers in servers/ directory"""
        loop = asyncio.get_event_loop()
        
        def _discover():
            servers = []
            servers_dir = os.path.join(self.config_dir, "servers")
            
            if not os.path.exists(servers_dir):
                return servers
            
            # Searches for directories in servers/
            for item in os.listdir(servers_dir):
                server_dir = os.path.join(servers_dir, item)
                if os.path.isdir(server_dir):
                    # Checks if it has an endpoints file
                    endpoints_file = os.path.join(server_dir, f"{item}_endpoints.json")
                    if os.path.exists(endpoints_file):
                        servers.append(item)
            
            return sorted(servers)
        
        # Runs blocking operation in thread pool
        with ThreadPoolExecutor(max_workers=2) as executor:
            servers = await loop.run_in_executor(executor, _discover)
        
        return servers
    
    async def _is_cache_valid(self, server_name: str, config: MCPServerConfig) -> bool:
        """Checks if cache is still valid"""
        current_time = time.time()
        
        # TTL check
        if current_time - config.last_modified > self.cache_ttl:
            return False
        
        # File modification check - new structure in servers/{server_name}/
        server_dir = os.path.join(self.config_dir, "servers", server_name)
        endpoints_file = os.path.join(server_dir, f"{server_name}_endpoints.json")
        headers_file = os.path.join(server_dir, f"{server_name}_headers.json")
        
        try:
            endpoints_mtime = os.path.getmtime(endpoints_file)
            headers_mtime = os.path.getmtime(headers_file) if os.path.exists(headers_file) else 0
            latest_mtime = max(endpoints_mtime, headers_mtime)
            
            return latest_mtime <= config.last_modified
        except OSError:
            return False
    
    async def load_server(self, server_name: str) -> Optional[MCPServerConfig]:
        """Thread-safe loading of server configuration with caching"""
        await self._ensure_initialized()
        file_lock = await self._get_file_lock(server_name)
        
        async with file_lock:
            # Checks cache
            async with self._cache_lock:
                if server_name in self._cache:
                    cached_config = self._cache[server_name]
                    if await self._is_cache_valid(server_name, cached_config):
                        print(f"🔄 DEBUG: Using cache for server {server_name}")
                        return cached_config
                    else:
                        print(f"🔄 DEBUG: Cache for server {server_name} is invalid")
                        del self._cache[server_name]
            
            # Loads from files
            config = await MCPServerConfig.load_from_files_async(server_name, self.config_dir)
            
            if config:
                async with self._cache_lock:
                    self._cache[server_name] = config
                    print(f"🔄 DEBUG: Server {server_name} loaded and stored in cache")
            
            return config
    
    async def reload_server(self, server_name: str) -> Optional[MCPServerConfig]:
        """Force reload server configuration"""
        await self._ensure_initialized()
        file_lock = await self._get_file_lock(server_name)
        
        async with file_lock:
            # Removes from cache
            async with self._cache_lock:
                if server_name in self._cache:
                    del self._cache[server_name]
            
            # Loads again
            return await self.load_server(server_name)
    
    async def get_all_endpoints_for_server(self, server_name: str) -> Dict[str, Any]:
        """Returns all endpoints for given server"""
        config = await self.load_server(server_name)
        return config.endpoints if config else {}
    
    async def get_headers_for_server(self, server_name: str) -> Dict[str, str]:
        """Returns headers for given server"""
        config = await self.load_server(server_name)
        return config.headers if config else {}

class ConcurrentRESTClient:
    """Concurrent REST client with connection pooling"""
    
    def __init__(self, session: aiohttp.ClientSession, config_manager: ThreadSafeConfigManager, security_manager=None):
        self.session = session
        self.config_manager = config_manager
        self.security_manager = security_manager or get_security_manager(config_manager.config_dir)
        self._request_semaphore = asyncio.Semaphore(50)
        self._debug_calls = {}
    
    def _merge_headers(self, server_headers: Dict[str, str], endpoint_headers: Dict[str, str]) -> Dict[str, str]:
        """Merges server headers with endpoint headers"""
        merged = server_headers.copy()
        merged.update(endpoint_headers)
        return merged
    
    def _substitute_placeholders(self, text: str, params: Dict[str, Any]) -> str:
        """Thread-safe replacement of {placeholders} in text"""
        for key, value in params.items():
            placeholder = f"{{{key}}}"
            text = text.replace(placeholder, str(value))
        return text
    
    def _prepare_url_and_params(self, url: str, path_params: Dict[str, str], 
                               query_params: Dict[str, str], user_params: Dict[str, Any]) -> tuple:
        """🔧 FIXED: Prepares URL with path parameters and query parameters"""
        import re
        
        # 🔧 FIXED: Identifikuj ktoré user_params sú určené pre path substitution
        path_placeholders = set()
        for match in re.finditer(r'\{(\w+)\}', url):
            path_placeholders.add(match.group(1))
        
        # 🔧 FIXED: Rozdeľ user_params na path a query
        path_user_params = {k: v for k, v in user_params.items() if k in path_placeholders}
        query_user_params = {k: v for k, v in user_params.items() if k not in path_placeholders}
        
        # Merge path params
        all_path_params = {}
        all_path_params.update(path_params)  # Default values from config
        all_path_params.update(path_user_params)  # 🔧 FIXED: Len relevantné path params
        
        # Replace {placeholders} in URL
        final_url = self._substitute_placeholders(url, all_path_params)
        
        # Prepare query parameters
        final_params = {}
        for key, value in query_params.items():
            final_params[key] = self._substitute_placeholders(str(value), user_params)
        
        # 🔧 FIXED: Add remaining user params as query params
        final_params.update(query_user_params)
        
        return final_url, final_params
    
    def _prepare_body(self, body_template: str, user_data: Dict[str, Any]) -> Optional[str]:
        """Thread-safe preparation of request body"""
        if not body_template:
            return None
        
        body = self._substitute_placeholders(body_template, user_data)
        
        try:
            # Tries to parse as JSON for validation
            json.loads(body)
            return body
        except json.JSONDecodeError:
            # If not valid JSON, returns as plain text
            return body
    
    @http_retry(max_retries=3, base_delay=1.0, backoff_factor=2.0)
    async def call_endpoint(self, server_name: str, endpoint_name: str, 
                           params: Dict[str, Any] = None, data: Dict[str, Any] = None) -> Dict[str, Any]:
        """Thread-safe endpoint call with rate limiting and security"""
        params = params or {}
        data = data or {}
        
        async with self._request_semaphore:
            start_time = asyncio.get_event_loop().time()
            
            # Loads server configuration
            config = await self.config_manager.load_server(server_name)
            if not config:
                return {"error": f"Server '{server_name}' not found or doesn't have valid configuration"}
            
            # Finds endpoint
            if endpoint_name not in config.endpoints:
                available = list(config.endpoints.keys())
                return {"error": f"Endpoint '{endpoint_name}' not found in server '{server_name}'. Available: {available}"}
            
            endpoint = config.endpoints[endpoint_name]
            
            try:
                # Prepares request parameters
                method = endpoint.get("method", "GET").upper()
                url = endpoint.get("url", "")
                endpoint_headers = endpoint.get("headers", {})
                # Separate handling for path_params and query_params
                path_params = endpoint.get("path_params", {})
                query_params = endpoint.get("query_params", endpoint.get("params", {}))
                body_template = endpoint.get("body_template", "")
                timeout = endpoint.get("timeout", 30)
                
                if not url:
                    return {"error": f"URL is not defined for endpoint '{endpoint_name}'"}
                
                # NEW: Get security context
                security_applied = False
                try:
                    security_context = await self.security_manager.get_security_context(server_name)
                    security_applied = True
                    print(f"🔒 DEBUG: Security context obtained for {server_name}")
                except Exception as e:
                    print(f"⚠️ WARNING: Security context not available for {server_name}: {e}")
                    from security_providers import SecurityContext
                    security_context = SecurityContext(headers={}, query_params={}, cookies={}, 
                                                     auth_data={'provider': 'fallback', 'error': str(e)})
                
                # Merges base headers (server headers + endpoint headers)
                headers = self._merge_headers(config.headers, endpoint_headers)
                
                # Prepares URL and parameters
                final_url, final_params = self._prepare_url_and_params(url, path_params, query_params, params)
                
                # NEW: Apply security if available
                cookies = {}
                if security_context:
                    try:
                        security_data = self.security_manager.apply_security_to_request(
                            server_name, security_context, headers, final_params
                        )
                        headers = security_data['headers']
                        final_params = security_data['params']
                        cookies = security_data.get('cookies', {})
                        print(f"🔒 DEBUG: Security applied for {server_name}: {len(security_context.headers)} headers")
                    except Exception as e:
                        print(f"⚠️ WARNING: Error applying security for {server_name}: {e}")
                        security_applied = False
                
                # Prepares body
                body = self._prepare_body(body_template, data)
                
                # Sets timeout
                client_timeout = aiohttp.ClientTimeout(total=timeout)
                
                # Prepares kwargs for request
                kwargs = {
                    "headers": headers,
                    "params": final_params,
                    "timeout": client_timeout
                }
                
                # Add cookies if they exist
                if cookies:
                    kwargs["cookies"] = cookies
                
                # Adds body for POST/PUT/PATCH
                if body and method in ["POST", "PUT", "PATCH"]:
                    if headers.get("Content-Type", "").startswith("application/json"):
                        try:
                            kwargs["json"] = json.loads(body)
                        except json.JSONDecodeError as e:
                            return {"error": f"Invalid JSON in body template: {e}"}
                    else:
                        kwargs["data"] = body
                
                # Debug output for first 3 calls of each server
                if not hasattr(self, '_debug_calls'):
                    self._debug_calls = {}
                
                if self._debug_calls.get(server_name, 0) < 3:
                    self._debug_calls[server_name] = self._debug_calls.get(server_name, 0) + 1
                    print(f"🔍 Debug call #{self._debug_calls[server_name]} pre {server_name}:")
                    print(f"   Method: {method}")
                    print(f"   URL: {final_url}")
                    print(f"   Headers: {dict(headers)}")
                    print(f"   Query params loaded: {query_params}")
                    print(f"   Final params: {final_params}")                    
                    print(f"   Security: {'✅ Applied' if security_applied else '❌ None'}")
                    if cookies:
                        print(f"   Cookies: {len(cookies)} items")
                
                # Executes request
                async with self.session.request(method, final_url, **kwargs) as response:
                    response_time = asyncio.get_event_loop().time() - start_time
                    
                    # Processes response
                    content_type = response.headers.get("Content-Type", "")
                    
                    try:
                        if "application/json" in content_type:
                            response_data = await response.json()
                        else:
                            response_data = await response.text()
                    except Exception as e:
                        # Fallback if response is not parseable
                        try:
                            response_data = await response.text()
                        except:
                            response_data = f"Unparseable response: {str(e)}"
                    
                    # Successful response
                    result = {
                        "status": response.status,
                        "headers": dict(response.headers),
                        "data": response_data,
                        "response_time": response_time,
                        "url": str(response.url),
                        "method": method,
                        "security_applied": security_applied
                    }
                    
                    # Add security info to response if debug info is needed
                    if security_applied:
                        result["security_info"] = {
                            "provider_type": security_context.auth_data.get('provider', 'unknown'),
                            "headers_added": len(security_context.headers),
                            "params_added": len(security_context.query_params),
                            "cookies_added": len(security_context.cookies)
                        }
                    
                    return result
            
            except asyncio.TimeoutError:
                response_time = asyncio.get_event_loop().time() - start_time
                return {
                    "error": f"Request timeout po {timeout}s",
                    "response_time": response_time,
                    "url": final_url if 'final_url' in locals() else url,
                    "method": method if 'method' in locals() else "UNKNOWN"
                }
            
            except aiohttp.ClientError as e:
                response_time = asyncio.get_event_loop().time() - start_time
                return {
                    "error": f"HTTP Client Error: {str(e)}",
                    "response_time": response_time,
                    "url": final_url if 'final_url' in locals() else url,
                    "method": method if 'method' in locals() else "UNKNOWN"
                }
            
            except Exception as e:
                response_time = asyncio.get_event_loop().time() - start_time
                return {
                    "error": f"Unexpected error: {str(e)}",
                    "response_time": response_time,
                    "url": final_url if 'final_url' in locals() else url,
                    "method": method if 'method' in locals() else "UNKNOWN",
                    "error_type": type(e).__name__
                }

# Thread-safe global variables
config_manager = None
session = None
server = Server("concurrent-config-mcp-server")

@server.list_tools()
async def handle_list_tools() -> List[Tool]:
    """Thread-safe generation of tools list"""
    global config_manager
    
    try:
        print("📋 INFO: handle_list_tools() called")
        
        if not config_manager:
            print("❌ ERROR: Config manager is not initialized")
            return []
        
        tools = []
        
        # Basic tools for management
        tools.extend([
            Tool(
                name="list_servers",
                description="Shows all available MCP servers",
                inputSchema={"type": "object", "properties": {}, "required": []}
            ),
            Tool(
                name="list_endpoints",
                description="Shows endpoints for specific server",
                inputSchema={
                    "type": "object",
                    "properties": {                        
                        "server_name": {"type": "string", "description": "Name of MCP server"}
                    },
                    "required": ["server_name"]
                }
            ),
            Tool(
                name="get_endpoint_details",
                description="Shows details of specific endpoint",
                inputSchema={
                    "type": "object",
                    "properties": {
                        "server_name": {"type": "string", "description": "Name of MCP server"},
                        "endpoint_name": {"type": "string", "description": "Name of endpoint"}
                    },
                    "required": ["server_name", "endpoint_name"]
                }
            ),
            Tool(
                name="reload_server",
                description="Reloads server configuration from files",
                inputSchema={
                    "type": "object",
                    "properties": {                        
                        "server_name": {"type": "string", "description": "Name of MCP server"}
                    },
                    "required": ["server_name"]
                }
            ),
            Tool(
                name="test_authentication",
                description="Test authentication for specific server",
                inputSchema={
                    "type": "object",
                    "properties": {                        
                        "server_name": {"type": "string", "description": "Name of server to test"}
                    },
                    "required": ["server_name"]
                }
            ),
            Tool(
                name="list_auth_status",                
                description="Shows authentication status for all servers",
                inputSchema={"type": "object", "properties": {}, "required": []}
            ),
            Tool(
                name="refresh_authentication",                
                description="Refreshes authentication for specific server",
                inputSchema={
                    "type": "object",
                    "properties": {                        
                        "server_name": {"type": "string", "description": "Name of server"}
                    },
                    "required": ["server_name"]
                }
            ),
            Tool(
                name="security_providers",
                description="Shows available security providers and their configurations",
                inputSchema={"type": "object", "properties": {}, "required": []}
            )
        ])
                
        print(f"📋 INFO: Basic tools created: {len(tools)}")
        
        # 🔧 FIXED: Dynamic tools for each server and endpoint with better error handling
        available_servers = []
        try:
            print("📋 INFO: Attempting to discover servers...")
            available_servers = await config_manager.discover_servers()
            print(f"📋 INFO: Found servers: {available_servers}")
        except Exception as e:
            print(f"❌ ERROR: Error discovering servers: {e}")
            import traceback
            print(f"❌ ERROR: Traceback: {traceback.format_exc()}")
            return tools
        
        for server_name in available_servers:
            print(f"📋 INFO: Processing server: {server_name}")
            try:
                print(f"📋 INFO: Loading endpoints for server {server_name}")
                endpoints = await config_manager.get_all_endpoints_for_server(server_name)                
                print(f"📋 INFO: Server {server_name} has endpoints: {list(endpoints.keys())}")
                
                # 🔧 FIXED: Safe iteration over endpoints
                if not isinstance(endpoints, dict):
                    print(f"❌ ERROR: Endpoints for {server_name} is not dict: {type(endpoints)}")
                    continue
                
                for endpoint_name, endpoint_config in endpoints.items():                    
                    print(f"📋 INFO: Creating tool for endpoint {endpoint_name}")
                    try:
                        if not isinstance(endpoint_config, dict):                            
                            print(f"❌ ERROR: Endpoint config for {endpoint_name} is not dict: {type(endpoint_config)}")
                            continue
                            
                        method = endpoint_config.get("method", "GET")
                        url = endpoint_config.get("url", "")
                        description = endpoint_config.get("description", f"{method} {url}")
                        
                        # 🔧 FIXED: Validate critical fields
                        if not method or not url:
                            print(f"❌ ERROR: Missing method or URL for endpoint {endpoint_name}")
                            continue
                        
                        # Creates schema for tool
                        tool_schema = {
                            "type": "object",
                            "properties": {
                                "params": {
                                    "type": "object",
                                    "description": "Parametre pre URL a query string",
                                    "default": {}
                                }
                            },
                            "required": []
                        }
                        
                        # Adds data parameter for POST/PUT/PATCH
                        if method.upper() in ["POST", "PUT", "PATCH"]:
                            tool_schema["properties"]["data"] = {
                                "type": "object",                                
                                "description": "Data for request body",
                                "default": {}
                            }
                        
                        # 🔧 FIXED: Safe tool creation with validation
                        tool_name = f"{server_name}__{endpoint_name}"
                        tool_description = f"[{server_name}] {description}"
                        
                        # Validate tool name and description
                        if not tool_name or not tool_description:
                            print(f"❌ ERROR: Invalid tool name or description for {endpoint_name}")
                            continue
                        
                        tool = Tool(
                            name=tool_name,
                            description=tool_description,
                            inputSchema=tool_schema
                        )
                        tools.append(tool)                        
                        print(f"📋 INFO: Tool {tool_name} created successfully")
                        
                    except Exception as e:                        
                        print(f"❌ ERROR: Error creating tool for endpoint {endpoint_name} in server {server_name}: {e}")
                        import traceback
                        print(f"❌ ERROR: Tool creation traceback: {traceback.format_exc()}")
                        continue
                        
            except Exception as e:
                print(f"❌ ERROR: Error loading endpoints for server {server_name}: {e}")
                import traceback
                print(f"❌ ERROR: Server processing traceback: {traceback.format_exc()}")
                continue
        
        print(f"📋 INFO: Total tools created: {len(tools)}")
        return tools
        
    except Exception as e:
        print(f"❌ ERROR: Unexpected error in handle_list_tools: {e}")
        # Return at least basic tools
        return [
            Tool(
                name="list_servers",                
                description="Shows all available MCP servers",
                inputSchema={"type": "object", "properties": {}, "required": []}
            )
        ]

@server.call_tool()
async def handle_call_tool(name: str, arguments: Dict[str, Any]) -> List[types.TextContent]:
    """Thread-safe processing of tool call"""
    global session, config_manager
    
    if not session or not config_manager:
        return [types.TextContent(type="text", text="❌ Server is not properly initialized")]
    
    # Uses the same security manager as in main()
    security_manager = get_security_manager(config_manager.config_dir)
    client = ConcurrentRESTClient(session, config_manager, security_manager)
    
    try:
        if name == "list_servers":
            servers = await config_manager.discover_servers()
            
            if not servers:
                return [types.TextContent(type="text", text="📭 No MCP servers are configured\n\nCreate files in format: {name}_endpoints.json")]
            
            output = "🖥️ **Available MCP servers:**\n\n"
            
            for server_name in servers:
                config = await config_manager.load_server(server_name)
                if config:
                    endpoint_count = len(config.endpoints)
                    header_count = len(config.headers)
                    output += f"**{server_name}**\n"
                    output += f"   📋 Endpoints: {endpoint_count}\n"
                    output += f"   🔑 Headers: {header_count}\n"
                    output += f"   📁 Files: {server_name}_endpoints.json, {server_name}_headers.json\n\n"
                else:
                    output += f"**{server_name}** ❌ (configuration error)\n\n"
            
            return [types.TextContent(type="text", text=output)]
        
        elif name == "list_endpoints":
            server_name = arguments["server_name"]
            endpoints = await config_manager.get_all_endpoints_for_server(server_name)
            
            if not endpoints:
                return [types.TextContent(type="text", text=f"❌ Server '{server_name}' not found or has no endpoints")]
            
            output = f"📋 **Endpoints for server '{server_name}':**\n\n"
            
            for endpoint_name, endpoint_config in endpoints.items():
                method = endpoint_config.get("method", "GET")
                url = endpoint_config.get("url", "N/A")
                description = endpoint_config.get("description", "")
                
                output += f"**{endpoint_name}**\n"
                output += f"   🔗 {method} {url}\n"
                if description:
                    output += f"   📝 {description}\n"
                output += f"   🛠️ Tool: `{server_name}__{endpoint_name}`\n\n"
            
            return [types.TextContent(type="text", text=output)]
        
        elif name == "get_endpoint_details":
            server_name = arguments["server_name"]
            endpoint_name = arguments["endpoint_name"]
            
            config = await config_manager.load_server(server_name)
            if not config or endpoint_name not in config.endpoints:                
                return [types.TextContent(type="text", text=f"❌ Endpoint '{endpoint_name}' not found in server '{server_name}'")]
            endpoint = config.endpoints[endpoint_name]
            
            output = f"🔍 **Endpoint details '{endpoint_name}' (server: {server_name}):**\n\n"
            output += f"**Method:** {endpoint.get('method', 'GET')}\n"
            output += f"**URL:** {endpoint.get('url', 'N/A')}\n"
            output += f"**Description:** {endpoint.get('description', 'N/A')}\n"
            output += f"**Timeout:** {endpoint.get('timeout', 30)}s\n"
            
            if endpoint.get('headers'):
                output += f"**Endpoint headers:**\n```json\n{json.dumps(endpoint['headers'], indent=2, ensure_ascii=False)}\n```\n"
            
            if config.headers:
                output += f"**Server headers:**\n```json\n{json.dumps(config.headers, indent=2, ensure_ascii=False)}\n```\n"
            
            if endpoint.get('query_params'):
                output += f"**Query params:**\n```json\n{json.dumps(endpoint['query_params'], indent=2, ensure_ascii=False)}\n```\n"
            
            if endpoint.get('body_template'):
                output += f"**Body template:**\n```json\n{endpoint['body_template']}\n```\n"
            
            return [types.TextContent(type="text", text=output)]
        
        elif name == "reload_server":
            server_name = arguments["server_name"]
            config = await config_manager.reload_server(server_name)
            
            if config:
                endpoint_count = len(config.endpoints)
                return [types.TextContent(type="text", text=f"✅ Server '{server_name}' was reloaded ({endpoint_count} endpoints)")]
            else:
                return [types.TextContent(type="text", text=f"❌ Error loading server '{server_name}'")]
        
        elif "__" in name:
            # Dynamic endpoint call
            parts = name.split("__", 1)
            if len(parts) != 2:
                return [types.TextContent(type="text", text="❌ Invalid tool name format")]
            
            server_name, endpoint_name = parts
            params = arguments.get("params", {})
            data = arguments.get("data", {})
            
            result = await client.call_endpoint(server_name, endpoint_name, params, data)
            
            if "error" in result:
                return [types.TextContent(type="text", text=f"❌ **Error:** {result['error']}\n\n**Response time:** {result.get('response_time', 0):.2f}s")]
            
            output = f"✅ **{server_name}__{endpoint_name} - Successful response**\n\n"
            output += f"**Status:** {result['status']}\n"
            output += f"**Method:** {result['method']}\n"
            output += f"**URL:** {result['url']}\n"
            output += f"**Response time:** {result['response_time']:.2f}s\n\n"
            
            # Formats response data
            if isinstance(result['data'], dict):
                output += f"**Response:**\n```json\n{json.dumps(result['data'], indent=2, ensure_ascii=False)}\n```"
            else:
                response_text = str(result['data'])
                if len(response_text) > 1000:
                    response_text = response_text[:1000] + "...(shortened)"
                output += f"**Response:**\n```\n{response_text}\n```"
            
            return [types.TextContent(type="text", text=output)]

        elif name == "test_authentication":
            server_name = arguments["server_name"]
            
            success = await client.security_manager.test_authentication(server_name)
            
            if success:                
                return [types.TextContent(type="text", text=f"✅ Authentication for server '{server_name}' is functional")]
            else:
                return [types.TextContent(type="text", text=f"❌ Authentication for server '{server_name}' failed")]

        elif name == "list_auth_status":
            status_list = await client.security_manager.list_servers_auth_status()
            
            if not status_list:
                return [types.TextContent(type="text", text="📭 No servers with authentication found")]
            
            output = "🔐 **Server authentication status:**\n\n"
            
            for status in status_list:
                server_name = status["server_name"]
                provider_type = status["provider_type"]
                authenticated = status["authenticated"]
                last_auth = status["last_auth_time"]
                error = status["error"]
                
                status_icon = "✅" if authenticated else "❌"
                
                output += f"**{server_name}**\n"
                output += f"   {status_icon} Typ: {provider_type}\n"
                output += f"   📊 Status: {'Authenticated' if authenticated else 'Not authenticated'}\n"
                
                if last_auth:
                    output += f"   🕐 Last authentication: {last_auth}\n"
                
                if error:
                    output += f"   ⚠️ Error: {error}\n"
                
                output += "\n"
            
            return [types.TextContent(type="text", text=output)]
        
        elif name == "refresh_authentication":
            server_name = arguments["server_name"]
            
            success = await client.security_manager.refresh_authentication(server_name)
            
            if success:
                return [types.TextContent(type="text", text=f"✅ Authentication for server '{server_name}' refreshed")]
            else:
                return [types.TextContent(type="text", text=f"❌ Authentication refresh for server '{server_name}' failed")]
            
        elif name == "security_providers":
            providers = client.security_manager.get_available_providers()
            
            output = "🔒 **Available Security Providers:**\n\n"
            
            for provider_type in providers:
                template = client.security_manager.get_provider_config_template(provider_type)
                
                output += f"**{provider_type}**\n"
                output += f"```json\n{json.dumps(template, indent=2, ensure_ascii=False)}\n```\n\n"
            
            return [types.TextContent(type="text", text=output)]

        else:
            return [types.TextContent(type="text", text=f"❌ Unknown tool: {name}")]
    
    except Exception as e:
        print(f"❌ ERROR: Error executing tool '{name}': {e}")
        return [types.TextContent(type="text", text=f"❌ Unexpected error: {str(e)}")]

async def main():
    """Thread-safe main function with security manager"""
    global session, config_manager
    
    try:
        # Gets directory where this script is located
        script_dir = os.path.dirname(os.path.abspath(__file__))
        print(f"📁 INFO: Script directory: {script_dir}")
        
        # Creates thread-safe config manager
        config_manager = ThreadSafeConfigManager(config_dir=script_dir)
        
        # Creates connection pool
        connector = aiohttp.TCPConnector(
            limit=100,
            limit_per_host=30,
            ttl_dns_cache=300,
            use_dns_cache=True,
            keepalive_timeout=60,
            enable_cleanup_closed=True
        )
        
        timeout = aiohttp.ClientTimeout(total=30, connect=10, sock_read=30)
        
        async with aiohttp.ClientSession(
            timeout=timeout, 
            connector=connector
        ) as http_session:
            session = http_session
            
            # NEW: Initialize security manager
            security_manager = get_security_manager(script_dir)
            await security_manager.initialize(http_session)
            
            # Shows available servers at startup
            servers = await config_manager.discover_servers()
            print(f"🚀 INFO: Concurrent Config MCP Server started")
            print(f"📋 INFO: Found {len(servers)} servers: {servers}")
            print(f"🔒 INFO: Security manager initialized")
            
            async with stdio_server() as (read_stream, write_stream):
                # MCP Protocol version 2025-03-26 compliance
                init_options = InitializationOptions(
                    server_name="concurrent-config-mcp-server",
                    server_version="1.0.0",
                    capabilities=server.get_capabilities(
                        notification_options=NotificationOptions(),
                        experimental_capabilities={}
                    ),
                    protocol_version="2024-11-05"  # 🔧 FIXED: Unified MCP Protocol version
                )
                
                print(f"🔌 INFO: MCP Server initialized with protocol version: 2024-11-05")
                await server.run(read_stream, write_stream, init_options)
    
    except Exception as e:
        print(f"❌ ERROR: Server error: {e}")
        sys.exit(1)

if __name__ == "__main__":
    asyncio.run(main())
