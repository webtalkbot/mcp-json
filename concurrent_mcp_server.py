#!/usr/bin/env python3
"""
concurrent_mcp_server.py - Thread-safe configuration MCP Server  
Optimized for high concurrency and multiple simultaneous users
"""

import argparse # NEW
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
    from dotenv import load_dotenv
    load_dotenv()
    print("INFO: .env file loaded", file=sys.stderr)
except:
    print("WARNING: .env not loaded", file=sys.stderr)

# MCP-safe logging
def safe_log(message):
    """Log message to stderr to avoid interfering with MCP JSON-RPC communication"""
    import sys
    sys.stderr.write(f"{message}\n")
    sys.stderr.flush()

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
    from mcp.types import Resource, Tool, TextContent, Implementation, ServerCapabilities, ToolsCapability, ResourcesCapability, PromptsCapability, LoggingCapability
    import mcp.types as types
    from mcp.server.stdio import stdio_server
except ImportError as e:
    error_msg = f"Missing MCP library: {e}. Install: pip install mcp"
    sys.stderr.write(f"CRITICAL ERROR: {error_msg}\n")

# NEW: Argument parsing function
def parse_arguments():
    """Parse command line arguments for server selection"""
    parser = argparse.ArgumentParser(
        description="Concurrent MCP Server with selective server loading",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  python concurrent_mcp_server.py                                    # Load all servers
  python concurrent_mcp_server.py servers --opensubtitles --coingecko  # Load only specified servers
  python concurrent_mcp_server.py servers opensubtitles coingecko      # Alternative syntax
        """
    )
    
    # NEW: Add mode argument
    parser.add_argument(
        '--mode', 
        choices=['admin', 'public', 'unrestricted'], 
        default='unrestricted',
        help='Server mode: admin (only management tools), public (exclude admin tools), unrestricted (all tools)'
    )
    
    # Subcommand for servers
    subparsers = parser.add_subparsers(dest='command', help='Commands')
    
    # Servers subcommand
    servers_parser = subparsers.add_parser('servers', help='Select specific servers to load')
    servers_parser.add_argument(
        'server_names', 
        nargs='*', 
        help='Names of servers to load (e.g., opensubtitles coingecko)'
    )
    
    # Alternative: using flags
    servers_parser.add_argument(
        '--opensubtitles', 
        action='store_true', 
        help='Load OpenSubtitles server'
    )
    servers_parser.add_argument(
        '--coingecko', 
        action='store_true', 
        help='Load CoinGecko server'
    )
    servers_parser.add_argument(
        '--all', 
        action='store_true', 
        help='Load all available servers (default)'
    )
    
    # Also add mode to servers subcommand
    servers_parser.add_argument(
        '--mode', 
        choices=['admin', 'public', 'unrestricted'], 
        default='unrestricted',
        help='Server mode: admin (only management tools), public (exclude admin tools), unrestricted (all tools)'
    )
    
    return parser.parse_args()

# NEW: Define admin-only endpoints (management tools)
ADMIN_ENDPOINTS = {
    "list_servers",
    "list_endpoints", 
    "get_endpoint_details",
    "reload_server",
    "test_authentication",
    "list_auth_status",
    "refresh_authentication",
    "security_providers",
    "show_server_filter"
}

def is_admin_endpoint(tool_name: str) -> bool:
    """Check if endpoint is admin-only"""
    # Extract base name from tool (remove server prefix if present)
    if "__" in tool_name:
        # For server tools like "server__endpoint"
        return False  # Server tools are never admin tools
    else:
        # For management tools
        return tool_name in ADMIN_ENDPOINTS

def should_include_endpoint(tool_name: str, mode: str) -> bool:
    """Determine if endpoint should be included based on mode"""
    is_admin = is_admin_endpoint(tool_name)
    
    if mode == "admin":
        # Admin mode: only admin endpoints
        return is_admin
    elif mode == "public": 
        # Public mode: exclude admin endpoints
        return not is_admin
    elif mode == "unrestricted":
        # Unrestricted mode: include all endpoints
        return True
    else:
        # Default to unrestricted for unknown modes
        return True
    
    # Log to error log if possible
    try:
        import logging
        logging.basicConfig(level=logging.ERROR)
        logger = logging.getLogger(__name__)
        logger.error(f"MCP Import Error: {error_msg}")
        
        # Try to log to our error logger
        try:
            from error_logger import log_custom_error
            log_custom_error("CRITICAL", "MCP_IMPORT_ERROR", "concurrent_mcp_server", 
                           error_msg, e, {"import_error": str(e)})
        except Exception:
            pass  # Error logger might not be available yet
    except Exception:
        pass
    
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
                        # FIXED: Bezpečnejšia kontrola HTTP status codes 5xx (server errors)
                        (hasattr(e, 'status') and isinstance(getattr(e, 'status', None), int) and e.status >= 500),
                        # For network-level errors
                        "Connection" in str(e),
                        "Timeout" in str(e),
                        "Network" in str(e)
                    ]
                    
                    if attempt < max_retries and any(retry_conditions):
                        # Exponential backoff with jitter
                        delay = base_delay * (backoff_factor ** attempt) + random.uniform(0, 0.5)
                        safe_log(f"HTTP retry attempt {attempt + 1}/{max_retries + 1} po {delay:.1f}s: {type(e).__name__}: {e}")
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
            safe_log(f"ERROR: File {endpoints_file} does not exist")
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
            safe_log(f"ERROR: Error parsing JSON files for server{server_name}: {e}")
            return None
        except Exception as e:
            safe_log(f"ERROR: Error loading server configuration {server_name}: {e}")
            return None

class ThreadSafeConfigManager:
    """Thread-safe management of MCP server configurations with caching"""
    
    def __init__(self, config_dir: str = ".", cache_ttl: int = 300, allowed_servers: List[str] = None):
        self.config_dir = config_dir
        self.cache_ttl = cache_ttl
        self.allowed_servers = allowed_servers  # NEW: Filter for allowed servers
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
        """Finds all available MCP servers in servers/ directory with optional filtering"""
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
                        # NEW: Apply server filtering
                        if self.allowed_servers is None or item in self.allowed_servers:
                            servers.append(item)
                        else:
                            safe_log(f"INFO: Server '{item}' filtered out (not in allowed list)")
            
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
                        safe_log(f"DEBUG: Using cache for server {server_name}")
                        return cached_config
                    else:
                        safe_log(f"DEBUG: Cache for server {server_name} is invalid")
                        del self._cache[server_name]
            
            # Loads from files
            config = await MCPServerConfig.load_from_files_async(server_name, self.config_dir)
            
            if config:
                async with self._cache_lock:
                    self._cache[server_name] = config
                    safe_log(f"DEBUG: Server {server_name} loaded and stored in cache")
            
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

    def set_allowed_servers(self, allowed_servers: List[str]):
        """Update the list of allowed servers"""
        self.allowed_servers = allowed_servers
        safe_log(f"INFO: Allowed servers updated: {allowed_servers}")

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
        """FIXED: Prepares URL with path parameters and query parameters"""
        import re
        
        # FIXED: Identifikuj ktoré user_params sú určené pre path substitution
        path_placeholders = set()
        for match in re.finditer(r'\{(\w+)\}', url):
            path_placeholders.add(match.group(1))
        
        # FIXED: Rozdeľ user_params na path a query
        path_user_params = {k: v for k, v in user_params.items() if k in path_placeholders}
        query_user_params = {k: v for k, v in user_params.items() if k not in path_placeholders}
        
        # Merge path params
        all_path_params = {}
        all_path_params.update(path_params)  # Default values from config
        all_path_params.update(path_user_params)  # FIXED: Len relevantné path params
        
        # Replace {placeholders} in URL
        final_url = self._substitute_placeholders(url, all_path_params)
        
        # Prepare query parameters
        final_params = {}
        for key, value in query_params.items():
            final_params[key] = self._substitute_placeholders(str(value), user_params)
        
        # FIXED: Add remaining user params as query params
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
                    safe_log(f"DEBUG: Security context obtained for {server_name}")
                except Exception as e:
                    safe_log(f"WARNING: Security context not available for {server_name}: {e}")
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
                        safe_log(f"DEBUG: Security applied for {server_name}: {len(security_context.headers)} headers")
                    except Exception as e:
                        safe_log(f"WARNING: Error applying security for {server_name}: {e}")
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
                    safe_log(f"Debug call #{self._debug_calls[server_name]} pre {server_name}:")
                    safe_log(f"   Method: {method}")
                    safe_log(f"   URL: {final_url}")
                    safe_log(f"   Headers: {dict(headers)}")
                    safe_log(f"   Query params loaded: {query_params}")
                    safe_log(f"   Final params: {final_params}")                    
                    safe_log(f"   Security: {'Applied' if security_applied else 'None'}")
                    if cookies:
                        safe_log(f"   Cookies: {len(cookies)} items")
                
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

# NEW: Helper function for parsing arguments
def extract_server_list_from_args(args) -> Optional[List[str]]:
    """Extract server list from parsed arguments"""
    if args.command != 'servers':
        return None  # Load all servers
    
    selected_servers = []
    
    # Method 1: Positional arguments
    if args.server_names:
        selected_servers.extend(args.server_names)
    
    # Method 2: Flag-based selection
    if args.opensubtitles:
        selected_servers.append('opensubtitles')
    if args.coingecko:
        selected_servers.append('coingecko')
    
    # If --all is specified or no servers selected, return None (load all)
    if args.all or not selected_servers:
        return None
    
    return selected_servers

# Thread-safe global variables
config_manager = None
session = None
server = Server("concurrent-config-mcp-server")

# FIXED: Add notification handler to prevent validation errors
async def handle_notification(method: str, params: Optional[Dict] = None) -> None:
    """Handle MCP notifications (messages without id field)"""
    try:
        safe_log(f"INFO: Received notification: {method}")
        
        if method == "notifications/initialized":
            safe_log("INFO: Client initialized notification received")
            # No action needed for initialized notification
            return
        elif method.startswith("notifications/"):
            safe_log(f"INFO: Notification {method} processed")
            return
        else:
            safe_log(f"WARNING: Unknown notification method: {method}")
            
    except Exception as e:
        safe_log(f"ERROR: Error handling notification {method}: {e}")
        try:
            from error_logger import log_custom_error
            log_custom_error("ERROR", "NOTIFICATION_HANDLER_ERROR", "concurrent_mcp_server", 
                           f"Error handling notification {method}: {e}", e, {
                               "method": method,
                               "params": params
                           })
        except Exception:
            pass

# Register notification handlers for all notification types
async def universal_notification_handler(method: str, params: Optional[Dict] = None) -> None:
    """Universal handler for all MCP notifications"""
    try:
        safe_log(f"INFO: Received notification: {method}")
        
        if method == "notifications/initialized":
            safe_log("INFO: Client initialized notification received")
            return
        elif method.startswith("notifications/"):
            safe_log(f"INFO: Notification {method} processed")
            return
        else:
            safe_log(f"WARNING: Unknown notification method: {method}")
            
    except Exception as e:
        safe_log(f"ERROR: Error handling notification {method}: {e}")

# Register all known notification types
notification_types = [
    "notifications/initialized",
    "notifications/progress",
    "notifications/message",
    "notifications/log",
    "notifications/resource_updated",
    "notifications/prompt_list_changed",
    "notifications/tool_list_changed"
]

for notification_type in notification_types:
    server.notification_handlers[notification_type] = universal_notification_handler

safe_log(f"INFO: Registered {len(notification_types)} notification handlers")

@server.list_tools()
async def handle_list_tools() -> List[Tool]:
    """Thread-safe generation of tools list with server filtering and mode-based endpoint filtering"""
    global config_manager, server_mode  # Add server_mode global variable
    max_retries = 5
    
    for attempt in range(max_retries):
        try:
            safe_log(f"INFO: handle_list_tools() called (attempt {attempt + 1}/{max_retries}) - Mode: {server_mode}")
            
            if not config_manager:
                if attempt < max_retries - 1:
                    safe_log(f"WARNING: Config manager is not initialized, retrying in 2s (attempt {attempt + 1}/{max_retries})")
                    await asyncio.sleep(2)
                    continue
                else:
                    safe_log("ERROR: Config manager is not initialized after retries")
                    return []
            
            await asyncio.sleep(0.5)
            tools = []
            
            # Basic management tools with mode filtering
            management_tools = [
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
            ]
            
            # NEW: Filter management tools based on mode
            for tool in management_tools:
                if should_include_endpoint(tool.name, server_mode):
                    tools.append(tool)
                    safe_log(f"INFO: Added management tool {tool.name} (mode: {server_mode})")
                else:
                    safe_log(f"INFO: Filtered out management tool {tool.name} (mode: {server_mode})")
            
            # Add mode info tool
            if server_mode in ["admin", "unrestricted"]:
                tools.append(
                    Tool(
                        name="show_server_mode",
                        description=f"Shows current server mode: {server_mode}",
                        inputSchema={"type": "object", "properties": {}, "required": []}
                    )
                )
            
            # Add server filter info (if applicable)
            if config_manager.allowed_servers:
                safe_log(f"INFO: Server filtering active: {config_manager.allowed_servers}")
                if should_include_endpoint("show_server_filter", server_mode):
                    tools.append(
                        Tool(
                            name="show_server_filter",
                            description=f"Shows current server filter: {', '.join(config_manager.allowed_servers)}",
                            inputSchema={"type": "object", "properties": {}, "required": []}
                        )
                    )
            
            safe_log(f"INFO: Management tools created: {len([t for t in tools if should_include_endpoint(t.name, server_mode)])}")

            # Get active servers and their endpoints
            try:
                available_servers = []
                try:
                    async with aiohttp.ClientSession() as session:
                        async with session.get("http://localhost:8999/servers") as response:
                            if response.status == 200:
                                data = await response.json()
                                running_servers = [
                                    server["name"] for server in data.get("servers", [])
                                    if server.get("status") == "running"
                                ]
                                available_servers = running_servers
                                safe_log(f"INFO: Active servers from mcp_wrapper: {available_servers}")
                            else:
                                available_servers = await config_manager.discover_servers()
                                safe_log(f"INFO: Fallback - Found servers via file discovery: {available_servers}")
                except Exception as e:
                    safe_log(f"WARNING: Cannot connect to mcp_wrapper: {e}")
                    available_servers = await config_manager.discover_servers()
                    safe_log(f"INFO: Fallback - Found servers via file discovery: {available_servers}")
                    
            except Exception as e:
                safe_log(f"ERROR: Error getting active servers: {e}")
                if attempt < max_retries - 1:
                    safe_log(f"RETRY: Retrying server discovery in 1s...")
                    await asyncio.sleep(1)
                    continue
                else:
                    safe_log("ERROR: Server discovery failed after all retries")
                    return tools  # Return management tools only
            
            # Process each server with endpoint filtering
            for server_name in available_servers:
                safe_log(f"INFO: Processing server: {server_name} (mode: {server_mode})")
                try:
                    await asyncio.sleep(0.1)
                    
                    endpoints = await config_manager.get_all_endpoints_for_server(server_name)
                    safe_log(f"INFO: Server {server_name} has endpoints: {list(endpoints.keys())}")
                    
                    if not isinstance(endpoints, dict):
                        safe_log(f"ERROR: Endpoints for {server_name} is not dict: {type(endpoints)}")
                        continue
                    
                    for endpoint_name, endpoint_config in endpoints.items():
                        try:
                            if not isinstance(endpoint_config, dict):
                                safe_log(f"ERROR: Endpoint config for {endpoint_name} is not dict")
                                continue
                            
                            method = endpoint_config.get("method", "GET")
                            url = endpoint_config.get("url", "")
                            description = endpoint_config.get("description", f"{method} {url}")
                            
                            if not method or not url:
                                safe_log(f"ERROR: Missing method or URL for endpoint {endpoint_name}")
                                continue
                            
                            tool_name = f"{server_name}__{endpoint_name}"
                            
                            # NEW: Filter server endpoints based on mode
                            if not should_include_endpoint(tool_name, server_mode):
                                safe_log(f"INFO: Filtered out server endpoint {tool_name} (mode: {server_mode})")
                                continue
                            
                            # Create tool schema
                            tool_schema = {
                                "type": "object",
                                "properties": {
                                    "params": {
                                        "type": "object",
                                        "description": "Parameters for URL and query string",
                                        "default": {}
                                    }
                                },
                                "required": []
                            }
                            
                            if method.upper() in ["POST", "PUT", "PATCH"]:
                                tool_schema["properties"]["data"] = {
                                    "type": "object",
                                    "description": "Data for request body",
                                    "default": {}
                                }
                            
                            tool_description = f"[{server_name}] {description}"
                            
                            if not tool_name or not tool_description:
                                safe_log(f"ERROR: Invalid tool name or description for {endpoint_name}")
                                continue
                            
                            tool = Tool(
                                name=tool_name,
                                description=tool_description,
                                inputSchema=tool_schema
                            )
                            tools.append(tool)
                            safe_log(f"INFO: Tool {tool_name} created successfully (mode: {server_mode})")
                            
                        except Exception as e:
                            safe_log(f"ERROR: Error creating tool for endpoint {endpoint_name}: {e}")
                            continue
                
                except Exception as e:
                    safe_log(f"ERROR: Error processing server {server_name}: {e}")
                    continue
            
            safe_log(f"INFO: Total tools created: {len(tools)} (mode: {server_mode})")
            return tools
            
        except Exception as e:
            if attempt < max_retries - 1:
                safe_log(f"WARNING: Tools list attempt {attempt + 1} failed, retrying in 2s: {e}")
                await asyncio.sleep(2)
                continue
            else:
                safe_log(f"ERROR: Tools list failed after {max_retries} attempts: {e}")
                # Return at least basic tools as fallback
                basic_tools = [
                    Tool(
                        name="show_server_mode",
                        description=f"Shows current server mode: {server_mode}",
                        inputSchema={"type": "object", "properties": {}, "required": []}
                    )
                ]
                return basic_tools

@server.call_tool()
async def handle_call_tool(name: str, arguments: Dict[str, Any]) -> List[types.TextContent]:
    """Thread-safe processing of tool call with mode filtering"""
    global session, config_manager, server_mode
    
    if not session or not config_manager:
        return [types.TextContent(type="text", text="Server is not properly initialized")]
    
    # NEW: Check if tool is allowed in current mode
    if not should_include_endpoint(name, server_mode):
        return [types.TextContent(type="text", text=f"Tool '{name}' is not available in '{server_mode}' mode")]
    
    # Uses the same security manager as in main()
    security_manager = get_security_manager(config_manager.config_dir)
    client = ConcurrentRESTClient(session, config_manager, security_manager)
    
    try:
        # NEW: Handle show_server_mode
        if name == "show_server_mode":
            mode_info = f"**Current server mode:** {server_mode}\n\n"
            
            if server_mode == "admin":
                mode_info += "**Admin mode** - Only management and administration tools are available:\n"
                mode_info += "- Server management (list, reload, status)\n"
                mode_info += "- Authentication management\n" 
                mode_info += "- Security configuration\n"
                mode_info += "- System administration tools\n\n"
                mode_info += "External API endpoints from configured servers are hidden."
                
            elif server_mode == "public":
                mode_info += "**Public mode** - All external API endpoints are available:\n"
                mode_info += "- All configured server endpoints\n"
                mode_info += "- External API integrations\n"
                mode_info += "- Data retrieval tools\n\n"
                mode_info += "Management and administration tools are hidden for security."
                
            elif server_mode == "unrestricted":
                mode_info += "**Unrestricted mode** - All tools are available:\n"
                mode_info += "- Management and administration tools\n"
                mode_info += "- All configured server endpoints\n"
                mode_info += "- External API integrations\n"
                mode_info += "- Complete system access\n\n"
                mode_info += "⚠️ **Warning:** This mode provides full access to all functionality."
            
            return [types.TextContent(type="text", text=mode_info)]
        
        # Existing tool handling - kompletne
        if name == "list_servers":
            servers = await config_manager.discover_servers()
            
            if not servers:
                return [types.TextContent(type="text", text="No MCP servers are configured\n\nCreate files in format: {name}_endpoints.json")]
            
            output = f"**Available MCP servers (Mode: {server_mode}):**\n\n"
            
            for server_name in servers:
                config = await config_manager.load_server(server_name)
                if config:
                    endpoint_count = len(config.endpoints)
                    header_count = len(config.headers)
                    output += f"**{server_name}**\n"
                    output += f"   Endpoints: {endpoint_count}\n"
                    output += f"   Headers: {header_count}\n"
                    output += f"   Files: {server_name}_endpoints.json, {server_name}_headers.json\n\n"
                else:
                    output += f"**{server_name}** (configuration error)\n\n"
            
            if server_mode != "unrestricted":
                output += f"\n*Note: Running in {server_mode} mode - some tools may be filtered*"
            
            return [types.TextContent(type="text", text=output)]
        
        elif name == "list_endpoints":
            server_name = arguments["server_name"]
            endpoints = await config_manager.get_all_endpoints_for_server(server_name)
            
            if not endpoints:
                return [types.TextContent(type="text", text=f"Server '{server_name}' not found or has no endpoints")]
            
            output = f"**Endpoints for server '{server_name}' (Mode: {server_mode}):**\n\n"
            
            for endpoint_name, endpoint_config in endpoints.items():
                method = endpoint_config.get("method", "GET")
                url = endpoint_config.get("url", "N/A")
                description = endpoint_config.get("description", "")
                
                output += f"**{endpoint_name}**\n"
                output += f"   {method} {url}\n"
                if description:
                    output += f"   {description}\n"
                output += f"   Tool: `{server_name}__{endpoint_name}`\n\n"
            
            return [types.TextContent(type="text", text=output)]
        
        elif name == "get_endpoint_details":
            server_name = arguments["server_name"]
            endpoint_name = arguments["endpoint_name"]
            
            config = await config_manager.load_server(server_name)
            if not config or endpoint_name not in config.endpoints:                
                return [types.TextContent(type="text", text=f"Endpoint '{endpoint_name}' not found in server '{server_name}'")]
            endpoint = config.endpoints[endpoint_name]
            
            output = f"**Endpoint details '{endpoint_name}' (server: {server_name}):**\n\n"
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
                return [types.TextContent(type="text", text=f"Server '{server_name}' was reloaded ({endpoint_count} endpoints)")]
            else:
                return [types.TextContent(type="text", text=f"Error loading server '{server_name}'")]
        
        elif name == "show_server_filter":
            if config_manager.allowed_servers:
                output = f"**Current server filter:** {', '.join(config_manager.allowed_servers)}\n\n"
                output += f"**Filtered servers:** Only these servers are loaded\n"
                output += f"**Mode:** {server_mode}"
            else:
                output = f"**No server filter active** - all servers are loaded\n\n"
                output += f"**Mode:** {server_mode}"
            
            return [types.TextContent(type="text", text=output)]

        elif "__" in name:
            # Dynamic endpoint call
            parts = name.split("__", 1)
            if len(parts) != 2:
                return [types.TextContent(type="text", text="Invalid tool name format")]
            
            server_name, endpoint_name = parts
            params = arguments.get("params", {})
            data = arguments.get("data", {})
            
            result = await client.call_endpoint(server_name, endpoint_name, params, data)
            
            if "error" in result:
                return [types.TextContent(type="text", text=f"**Error:** {result['error']}\n\n**Response time:** {result.get('response_time', 0):.2f}s")]
            
            output = f"**{server_name}__{endpoint_name} - Successful response**\n\n"
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

        # Admin-only tools (ak mode povoľuje)
        elif name == "test_authentication":
            server_name = arguments["server_name"]
            # Mock implementation since we don't have security manager
            return [types.TextContent(type="text", text=f"Authentication test for '{server_name}' - Mock implementation")]

        elif name == "list_auth_status":
            return [types.TextContent(type="text", text="Authentication status - Mock implementation")]

        elif name == "refresh_authentication":
            server_name = arguments["server_name"]
            return [types.TextContent(type="text", text=f"Authentication refresh for '{server_name}' - Mock implementation")]
            
        elif name == "security_providers":
            return [types.TextContent(type="text", text="Security providers - Mock implementation")]

        else:
            return [types.TextContent(type="text", text=f"Unknown tool: {name}")]
    
    except Exception as e:
        safe_log(f"ERROR: Error executing tool '{name}': {e}")
        return [types.TextContent(type="text", text=f"Unexpected error: {str(e)}")]

# Global variable for server mode
server_mode = "unrestricted"

async def main():
    """Thread-safe main function with selective server loading and mode support"""
    global session, config_manager, server_mode
    
    try:
        # NEW: Parse command line arguments with mode support
        args = parse_arguments()
        selected_servers = extract_server_list_from_args(args)
        
        # NEW: Set server mode
        server_mode = getattr(args, 'mode', 'unrestricted')
        
        if selected_servers:
            safe_log(f"INFO: Loading only selected servers: {selected_servers}")
        else:
            safe_log(f"INFO: Loading all available servers")
        
        safe_log(f"INFO: Server mode: {server_mode}")
        
        # Log mode implications
        if server_mode == "admin":
            safe_log(f"INFO: Admin mode - only management tools will be available")
        elif server_mode == "public":
            safe_log(f"INFO: Public mode - admin tools will be hidden")
        elif server_mode == "unrestricted":
            safe_log(f"INFO: Unrestricted mode - all tools will be available")
        
        # Gets directory where this script is located
        script_dir = os.path.dirname(os.path.abspath(__file__))
        safe_log(f"INFO: Script directory: {script_dir}")
        
        # Creates thread-safe config manager with server filtering
        try:
            config_manager = ThreadSafeConfigManager(
                config_dir=script_dir, 
                allowed_servers=selected_servers  # NEW: Pass selected servers
            )
            safe_log(f"INFO: Config manager created with server filtering")
        except Exception as e:
            error_msg = f"Failed to create config manager: {e}"
            safe_log(f"ERROR: {error_msg}")
            try:
                from error_logger import log_custom_error
                log_custom_error("CRITICAL", "CONFIG_MANAGER_ERROR", "concurrent_mcp_server", 
                               error_msg, e, {"script_dir": script_dir})
            except Exception:
                pass
            sys.exit(1)
        
        # Creates connection pool
        try:
            connector = aiohttp.TCPConnector(
                limit=100,
                limit_per_host=30,
                ttl_dns_cache=300,
                use_dns_cache=True,
                keepalive_timeout=60,
                enable_cleanup_closed=True
            )
            
            timeout = aiohttp.ClientTimeout(total=30, connect=10, sock_read=30)
            safe_log(f"INFO: HTTP connector and timeout configured")
        except Exception as e:
            error_msg = f"Failed to create HTTP connector: {e}"
            safe_log(f"ERROR: {error_msg}")
            try:
                from error_logger import log_custom_error
                log_custom_error("CRITICAL", "HTTP_CONNECTOR_ERROR", "concurrent_mcp_server", 
                               error_msg, e)
            except Exception:
                pass
            sys.exit(1)
        
        # Start HTTP session
        try:
            async with aiohttp.ClientSession(
                timeout=timeout, 
                connector=connector
            ) as http_session:
                session = http_session
                safe_log(f"INFO: HTTP session started")
                
                # Initialize security manager
                try:
                    security_manager = get_security_manager(script_dir)
                    await security_manager.initialize(http_session)
                    safe_log(f"INFO: Security manager initialized")
                except Exception as e:
                    error_msg = f"Security manager initialization failed: {e}"
                    safe_log(f"WARNING: {error_msg}")
                    try:
                        from error_logger import log_custom_error
                        log_custom_error("WARNING", "SECURITY_INIT_ERROR", "concurrent_mcp_server", 
                                       error_msg, e, {"script_dir": script_dir})
                    except Exception:
                        pass
                    # Continue without security manager
                
                # Discover servers
                try:
                    servers = await config_manager.discover_servers()
                    safe_log(f"INFO: Concurrent Config MCP Server started")
                    safe_log(f"INFO: Found {len(servers)} servers: {servers}")
                except Exception as e:
                    error_msg = f"Server discovery failed: {e}"
                    safe_log(f"WARNING: {error_msg}")
                    try:
                        from error_logger import log_custom_error
                        log_custom_error("WARNING", "SERVER_DISCOVERY_ERROR", "concurrent_mcp_server", 
                                       error_msg, e, {"script_dir": script_dir})
                    except Exception:
                        pass
                    servers = []
                
                # Start MCP server
                try:
                    async with stdio_server() as (read_stream, write_stream):
                        safe_log(f"INFO: stdio_server started")
                        
                        # MCP Protocol version 2025-03-26 compliance
                        try:
                            # FIXED: Explicitly define capabilities as empty objects instead of using server.get_capabilities()
                            explicit_capabilities = ServerCapabilities(
                                tools=ToolsCapability(),
                                resources=ResourcesCapability(),
                                prompts=PromptsCapability(),
                                logging=LoggingCapability()
                            )
                            
                            init_options = InitializationOptions(
                                server_name="concurrent-config-mcp-server",
                                server_version="1.0.0",
                                capabilities=explicit_capabilities,
                                protocol_version="2024-11-05"  # FIXED: Unified MCP Protocol version
                            )
                            
                            safe_log(f"INFO: MCP Server initialized with explicit capabilities as empty objects")
                            safe_log(f"INFO: Protocol version: 2024-11-05")
                        except Exception as e:
                            error_msg = f"Failed to create initialization options: {e}"
                            safe_log(f"ERROR: {error_msg}")
                            try:
                                from error_logger import log_custom_error
                                log_custom_error("CRITICAL", "INIT_OPTIONS_ERROR", "concurrent_mcp_server", 
                                               error_msg, e)
                            except Exception:
                                pass
                            sys.exit(1)
                        
                        # Run the server
                        try:
                            safe_log(f"INFO: Starting MCP server.run()...")
                            await server.run(read_stream, write_stream, init_options)
                        except Exception as e:
                            error_msg = f"MCP server.run() failed: {e}"
                            safe_log(f"ERROR: {error_msg}")
                            
                            # Check if this is the notifications/initialized validation error
                            if "notifications/initialized" in str(e) and "ValidationError" in str(e):
                                safe_log(f"DETECTED: MCP protocol validation error with notifications/initialized")
                                safe_log(f"REASON: Notification messages should not have 'id' field")
                                
                                try:
                                    from error_logger import log_custom_error
                                    log_custom_error("CRITICAL", "MCP_NOTIFICATION_PROTOCOL_ERROR", "concurrent_mcp_server", 
                                                   "MCP protocol error: notifications/initialized sent with ID field, but notifications must not have ID", 
                                                   e, {
                                                       "server_name": "concurrent-config-mcp-server",
                                                       "protocol_version": "2024-11-05",
                                                       "fix_needed": "Remove 'id' field from notification messages",
                                                       "mcp_spec": "Notifications must not contain 'id' field according to MCP protocol"
                                                   })
                                except Exception:
                                    pass
                                    
                                safe_log(f"BUG IDENTIFIED: The concurrent_mcp_server.py is running as standalone MCP server")
                                safe_log(f"SOLUTION: This server should NOT be run directly, it's meant to be called by mcp_wrapper.py")
                                safe_log(f"ACTION NEEDED: Fix the script to prevent direct execution or fix MCP protocol compliance")
                            else:
                                try:
                                    from error_logger import log_custom_error
                                    log_custom_error("CRITICAL", "SERVER_RUN_ERROR", "concurrent_mcp_server", 
                                                   error_msg, e, {
                                                       "server_name": "concurrent-config-mcp-server",
                                                       "protocol_version": "2024-11-05"
                                                   })
                                except Exception:
                                    pass
                            raise
                            
                except Exception as e:
                    error_msg = f"stdio_server context error: {e}"
                    safe_log(f"ERROR: {error_msg}")
                    try:
                        from error_logger import log_custom_error
                        log_custom_error("CRITICAL", "STDIO_SERVER_ERROR", "concurrent_mcp_server", 
                                       error_msg, e)
                    except Exception:
                        pass
                    raise
                    
        except Exception as e:
            error_msg = f"HTTP session error: {e}"
            safe_log(f"ERROR: {error_msg}")
            try:
                from error_logger import log_custom_error
                log_custom_error("CRITICAL", "HTTP_SESSION_ERROR", "concurrent_mcp_server", 
                               error_msg, e)
            except Exception:
                pass
            raise
    
    except KeyboardInterrupt:
        safe_log(f"INFO: Server shutdown requested (Ctrl+C)")
        try:
            from error_logger import log_custom_error
            log_custom_error("INFO", "SHUTDOWN", "concurrent_mcp_server", 
                           "Server shutdown requested by user")
        except Exception:
            pass
        sys.exit(0)
    
    except Exception as e:
        error_msg = f"Critical server error: {e}"
        safe_log(f"CRITICAL ERROR: {error_msg}")
        
        # Log with full traceback
        try:
            import traceback
            traceback_str = traceback.format_exc()
            safe_log(f"TRACEBACK:\n{traceback_str}")
            
            from error_logger import log_custom_error
            log_custom_error("CRITICAL", "MAIN_FUNCTION_ERROR", "concurrent_mcp_server", 
                           error_msg, e, {
                               "traceback": traceback_str,
                               "error_type": type(e).__name__
                           })
        except Exception as log_error:
            safe_log(f"WARNING: Could not log error: {log_error}")
        
        sys.exit(1)

if __name__ == "__main__":
    asyncio.run(main())
