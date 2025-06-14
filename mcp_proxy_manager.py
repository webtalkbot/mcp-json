#!/usr/bin/env python3
"""
mcp_proxy_manager.py - Concurrent proxy server with path-based routing
Handles multiple simultaneous connections efficiently while maintaining endpoint compatibility
"""

import asyncio
import aiohttp
import subprocess
import json
import os
import signal
import sys
import time
from typing import Dict, List, Optional, Set
from aiohttp import web, ClientSession
from concurrent.futures import ThreadPoolExecutor
import threading
import weakref

class MCPSingleProxyManager:
    def __init__(self, proxy_port=None):
        # Load configuration from environment
        if proxy_port is None:
            proxy_port = int(os.getenv("PROXY_PORT", 9000))
        
        self.proxy_port = proxy_port
        self.manager_port = int(os.getenv("PORT", 8999))
        self.config_file = "proxy_config.json"
        
        # Concurrent processing
        self.executor = ThreadPoolExecutor(max_workers=10)
        self.active_connections: Set[weakref.ref] = set()
        self.session: Optional[ClientSession] = None
        self.app: Optional[web.Application] = None
        self.runner: Optional[web.AppRunner] = None
        
        # State management
        self.running = True
        self.proxy_process: Optional[subprocess.Popen] = None
        self.monitor_task: Optional[asyncio.Task] = None
        
        print(f"🔧 Concurrent Proxy Manager initialized:")
        print(f"   Proxy port: {self.proxy_port}")
        print(f"   Manager port: {self.manager_port}")
        print(f"   Max workers: {self.executor._max_workers}")

    async def get_active_servers(self) -> List[Dict]:
        """Asynchronously get active servers from main wrapper"""
        try:
            async with self.session.get(
                f"http://localhost:{self.manager_port}/servers",
                timeout=aiohttp.ClientTimeout(total=5)
            ) as response:
                if response.status == 200:
                    data = await response.json()
                    return [s for s in data.get('servers', []) if s['status'] == 'running']
                return []
        except Exception as e:
            print(f"Error getting active servers: {e}")
            return []

    def get_server_config(self, server_name: str) -> Dict:
        """Get server configuration (cached for performance)"""
        try:
            config_path = f"servers/{server_name}/{server_name}_config.json"
            if os.path.exists(config_path):
                with open(config_path, 'r') as f:
                    return json.load(f)
            return {'mode': 'unrestricted'}
        except Exception as e:
            print(f"Error reading config for {server_name}: {e}")
            return {'mode': 'unrestricted'}

    async def health_check(self, request):
        """Health check endpoint"""
        servers = await self.get_active_servers()
        return web.json_response({
            "status": "healthy",
            "proxy_port": self.proxy_port,
            "active_servers": len(servers),
            "active_connections": len(self.active_connections),
            "server_names": [s['name'] for s in servers]
        })

    async def proxy_request_sparfenyuk(self, request):
        """Proxy requests to MCP servers - ORIGINAL /servers/{name}/sse format"""
        try:
            # Extract server name from path: /servers/{server_name}/sse
            path_parts = request.path.strip('/').split('/')
            if len(path_parts) < 3 or path_parts[0] != 'servers':
                return web.json_response(
                    {"error": "Invalid path format. Use /servers/server_name/sse"}, 
                    status=400
                )
            
            server_name = path_parts[1]  # servers/[SERVER_NAME]/sse
            endpoint_type = path_parts[2]  # servers/server_name/[SSE]
            
            if endpoint_type != 'sse':
                return web.json_response(
                    {"error": "Only SSE endpoints supported"}, 
                    status=400
                )

            # Use common handler
            return await self._handle_proxy_request(request, server_name)

        except Exception as e:
            print(f"Error in proxy_request_sparfenyuk: {e}")
            return web.json_response({"error": "Internal proxy error"}, status=500)

    async def proxy_request_short(self, request):
        """Proxy requests - SHORT /{name}/sse format for convenience"""
        try:
            # Extract server name from path: /{server_name}/sse
            path_parts = request.path.strip('/').split('/')
            if len(path_parts) < 2:
                return web.json_response(
                    {"error": "Invalid path format. Use /server_name/sse"}, 
                    status=400
                )
            
            server_name = path_parts[0]  # [SERVER_NAME]/sse
            endpoint_type = path_parts[1]  # server_name/[SSE]
            
            if endpoint_type != 'sse':
                return web.json_response(
                    {"error": "Only SSE endpoints supported"}, 
                    status=400
                )

            # Use common handler
            return await self._handle_proxy_request(request, server_name)

        except Exception as e:
            print(f"Error in proxy_request_short: {e}")
            return web.json_response({"error": "Internal proxy error"}, status=500)

    async def _handle_proxy_request(self, request, server_name):
        """Common handler for both endpoint formats"""
        # Verify server exists and is running
        servers = await self.get_active_servers()
        server_exists = any(s['name'] == server_name for s in servers)
        
        if not server_exists:
            return web.json_response(
                {"error": f"Server '{server_name}' not found or not running"}, 
                status=404
            )

        # Forward request to actual MCP server
        target_url = f"http://localhost:{self.manager_port}/mcp/{server_name}/sse"
        
        # Handle different HTTP methods
        if request.method == 'GET':
            return await self._proxy_get_request(request, target_url)
        elif request.method == 'POST':
            return await self._proxy_post_request(request, target_url)
        else:
            return web.json_response(
                {"error": f"Method {request.method} not supported"}, 
                status=405
            )

    async def _proxy_get_request(self, request, target_url):
        """Handle GET requests (typically SSE connections)"""
        try:
            # Forward headers (excluding host)
            headers = {k: v for k, v in request.headers.items() 
                      if k.lower() not in ['host', 'content-length']}
            
            async with self.session.get(
                target_url,
                headers=headers,
                params=request.query,
                timeout=aiohttp.ClientTimeout(total=None)  # No timeout for SSE
            ) as response:
                
                # Create streaming response
                if response.content_type == 'text/event-stream':
                    return await self._stream_sse_response(response)
                else:
                    # Regular response
                    body = await response.read()
                    return web.Response(
                        body=body,
                        status=response.status,
                        headers=response.headers
                    )
                    
        except Exception as e:
            print(f"Error proxying GET request: {e}")
            return web.json_response({"error": "Proxy error"}, status=500)

    async def _proxy_post_request(self, request, target_url):
        """Handle POST requests"""
        try:
            # Read request body
            body = await request.read()
            
            # Forward headers
            headers = {k: v for k, v in request.headers.items() 
                      if k.lower() not in ['host']}
            
            async with self.session.post(
                target_url,
                data=body,
                headers=headers,
                timeout=aiohttp.ClientTimeout(total=30)
            ) as response:
                response_body = await response.read()
                return web.Response(
                    body=response_body,
                    status=response.status,
                    headers=response.headers
                )
                
        except Exception as e:
            print(f"Error proxying POST request: {e}")
            return web.json_response({"error": "Proxy error"}, status=500)

    async def _stream_sse_response(self, upstream_response):
        """Stream SSE response with connection tracking"""
        response = web.StreamResponse(
            status=upstream_response.status,
            headers={
                'Content-Type': 'text/event-stream',
                'Cache-Control': 'no-cache',
                'Connection': 'keep-alive',
                'Access-Control-Allow-Origin': '*',
                'Access-Control-Allow-Headers': 'Cache-Control'
            }
        )
        
        await response.prepare()
        
        # Track connection
        connection_ref = weakref.ref(response)
        self.active_connections.add(connection_ref)
        
        try:
            # Stream data from upstream to client
            async for chunk in upstream_response.content.iter_any():
                await response.write(chunk)
                
        except Exception as e:
            print(f"Error streaming SSE: {e}")
        finally:
            # Clean up connection tracking
            self.active_connections.discard(connection_ref)
            
        return response

    async def list_endpoints(self, request):
        """List all available endpoints with BOTH formats"""
        servers = await self.get_active_servers()
        
        endpoints = []
        for server in servers:
            server_name = server['name']
            server_config = self.get_server_config(server_name)
            mode = server_config.get('mode', 'unrestricted')
            
            endpoints.append({
                'server': server_name,
                'endpoint_primary': f"http://localhost:{self.proxy_port}/servers/{server_name}/sse",
                'endpoint_short': f"http://localhost:{self.proxy_port}/{server_name}/sse",
                'mode': mode,
                'status': server['status']
            })
        
        return web.json_response({
            "proxy_port": self.proxy_port,
            "endpoints": endpoints,
            "total_servers": len(endpoints),
            "active_connections": len(self.active_connections),
            "supported_formats": [
                "/servers/{server_name}/sse (primary - sparfenyuk compatible)",
                "/{server_name}/sse (short format)"
            ]
        })

    def create_app(self):
        """Create aiohttp application with concurrent request handling"""
        app = web.Application()
        
        # Health check
        app.router.add_get('/health', self.health_check)
        app.router.add_get('/status', self.health_check)
        
        # List endpoints
        app.router.add_get('/endpoints', self.list_endpoints)
        app.router.add_get('/', self.list_endpoints)
        
        # ✅ ORIGINAL ENDPOINT FORMATS PRESERVED
        # sparfenyuk mcp-proxy compatible endpoints: /servers/{name}/sse
        app.router.add_route('*', '/servers/{server_name}/sse', self.proxy_request_sparfenyuk)
        app.router.add_route('*', '/servers/{server_name}/sse/{tail:.*}', self.proxy_request_sparfenyuk)
        
        # Optionally, a shorter format for backward compatibility
        app.router.add_route('*', '/{server_name}/sse', self.proxy_request_short)
        app.router.add_route('*', '/{server_name}/sse/{tail:.*}', self.proxy_request_short)
        
        return app

    async def start_proxy_server(self):
        """Start the concurrent proxy server"""
        try:
            # Create HTTP session for outgoing requests
            connector = aiohttp.TCPConnector(
                limit=100,              # Total connection pool size
                limit_per_host=30,      # Connections per host
                ttl_dns_cache=300,      # DNS cache TTL
                use_dns_cache=True,
            )
            
            self.session = ClientSession(
                connector=connector,
                timeout=aiohttp.ClientTimeout(total=30)
            )
            
            # Create and configure app
            self.app = self.create_app()
            self.runner = web.AppRunner(self.app)
            await self.runner.setup()
            
            # Start server
            site = web.TCPSite(
                self.runner, 
                host='0.0.0.0', 
                port=self.proxy_port,
                reuse_address=True,
                reuse_port=True
            )
            
            await site.start()
            
            print(f"✅ Concurrent proxy server started on port {self.proxy_port}")
            return True
            
        except Exception as e:
            print(f"❌ Error starting proxy server: {e}")
            return False

    # Compatibility methods for backward compatibility
    def start_single_proxy(self) -> bool:
        """Compatibility method - now starts async proxy"""
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        return loop.run_until_complete(self.start_proxy_server())

    async def monitor_servers(self):
        """Monitor server changes and update routing"""
        last_servers = set()
        
        while self.running:
            try:
                # Get current servers
                current_servers_list = await self.get_active_servers()
                current_servers = {s['name'] for s in current_servers_list}
                
                # Check for changes
                if current_servers != last_servers:
                    print(f"🔄 Server configuration changed:")
                    print(f"   Added: {current_servers - last_servers}")
                    print(f"   Removed: {last_servers - current_servers}")
                    print(f"   Active: {current_servers}")
                    
                    last_servers = current_servers
                
                # Clean up dead connections
                dead_connections = [ref for ref in self.active_connections if ref() is None]
                for ref in dead_connections:
                    self.active_connections.discard(ref)
                
                # Wait before next check (faster than original)
                await asyncio.sleep(30)  # Check every 30 seconds
                
            except Exception as e:
                print(f"❌ Error in server monitoring: {e}")
                await asyncio.sleep(5)

    def monitor_and_restart(self):
        """Compatibility method for threading - now runs async monitoring"""
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        loop.run_until_complete(self.monitor_servers())

    async def wait_for_manager(self):
        """Wait until MCP manager is running"""
        print("🔄 Waiting for MCP manager...")
        
        for i in range(30):
            try:
                if not self.session:
                    connector = aiohttp.TCPConnector()
                    self.session = ClientSession(connector=connector)
                
                async with self.session.get(
                    f"http://localhost:{self.manager_port}/",
                    timeout=aiohttp.ClientTimeout(total=2)
                ) as response:
                    if response.status == 200:
                        print("✅ MCP manager is available")
                        return True
            except:
                pass

            print(f"   Attempt {i+1}/30...")
            await asyncio.sleep(1)

        print("❌ MCP manager is not available")
        return False

    def show_endpoints_sparfenyuk(self, servers: List[Dict]):
        """Show available endpoints for sparfenyuk mcp-proxy - ORIGINAL FORMAT"""
        print(f"\n🎯 Concurrent Proxy Endpoints (Port: {self.proxy_port}):")
        print(f"{'Server':<20} {'Endpoint':<50} {'Mode':<12}")
        print("-" * 82)

        for server in servers:
            server_name = server['name']
            # ✅ ORIGINAL FORMAT PRESERVED
            endpoint = f"http://localhost:{self.proxy_port}/servers/{server_name}/sse"
            server_config = self.get_server_config(server_name)
            mode = server_config.get('mode', 'unrestricted')

            print(f"{server_name:<20} {endpoint:<50} {mode:<12}")

        print(f"\n💡 Endpoint Formats (both supported):")
        print(f"   🎯 Primary: /servers/{{server_name}}/sse (sparfenyuk compatible)")
        print(f"   ⚡ Short:   /{{server_name}}/sse (convenience)")
        
        print(f"\n💡 Concurrent Features:")
        print(f"   🔗 Multiple simultaneous connections supported")
        print(f"   ⚡ Connection pooling and reuse")
        print(f"   📊 Real-time connection tracking")
        print(f"   🔄 Automatic server discovery")
        print(f"   🔄 100% backward compatibility maintained")
        
        print(f"\n🔗 Usage examples:")
        print(f"   Claude Desktop: Add http://localhost:{self.proxy_port}/servers/{{server_name}}/sse")
        print(f"   Test endpoint: curl http://localhost:{self.proxy_port}/servers/opensubtitles/sse")
        print(f"   Health check: curl http://localhost:{self.proxy_port}/health")

    def show_endpoints(self, config: Dict):
        """Compatibility method - show endpoints from config"""
        print(f"\n🎯 Concurrent Proxy Endpoints (Port: {self.proxy_port}):")
        print(f"{'Server':<20} {'Endpoint':<50} {'Mode':<12}")
        print("-" * 82)

        for server_name in config["mcpServers"].keys():
            # ✅ ORIGINAL FORMAT PRESERVED
            endpoint = f"http://localhost:{self.proxy_port}/servers/{server_name}/sse"
            server_config = self.get_server_config(server_name)
            mode = server_config.get('mode', 'unrestricted')
            
            print(f"{server_name:<20} {endpoint:<50} {mode:<12}")

        print(f"\n💡 Usage examples:")
        print(f"   Claude Desktop: Add http://localhost:{self.proxy_port}/servers/{{server_name}}/sse")
        print(f"   Test endpoint: curl http://localhost:{self.proxy_port}/servers/opensubtitles/sse")
        print(f"   All servers available on a single port with path routing!")

    async def shutdown(self):
        """Graceful shutdown"""
        print("🛑 Shutting down concurrent proxy manager...")
        
        self.running = False
        
        # Cancel monitoring task
        if self.monitor_task:
            self.monitor_task.cancel()
            try:
                await self.monitor_task
            except asyncio.CancelledError:
                pass
        
        # Close HTTP session
        if self.session:
            await self.session.close()
        
        # Stop web server
        if self.runner:
            await self.runner.cleanup()
        
        # Shutdown thread pool
        self.executor.shutdown(wait=True)
        
        print("✅ Concurrent proxy manager shut down")

    def stop_proxy(self):
        """Compatibility method for stopping proxy"""
        if hasattr(self, '_shutdown_future'):
            return
        
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        loop.run_until_complete(self.shutdown())

    async def run_async(self):
        """Main async loop"""
        print("🚀 Starting Concurrent MCP Proxy Manager...")

        try:
            # Wait for main wrapper
            if not await self.wait_for_manager():
                return False

            # Start proxy server
            if not await self.start_proxy_server():
                print("❌ Failed to start proxy server")
                return False

            # Start monitoring
            self.monitor_task = asyncio.create_task(self.monitor_servers())

            # Show endpoints
            servers = await self.get_active_servers()
            self.show_endpoints_sparfenyuk(servers)

            # Keep running
            print(f"✅ Concurrent proxy running on port {self.proxy_port}")
            print(f"📊 Ready to handle multiple simultaneous connections")
            
            # Wait for shutdown signal
            try:
                await self.monitor_task
            except asyncio.CancelledError:
                pass
                
            return True

        except Exception as e:
            print(f"❌ Error in main loop: {e}")
            return False
        finally:
            await self.shutdown()

    def run(self):
        """Main loop - compatibility method"""
        print("🚀 MCP Concurrent Proxy Manager starting...")

        # Setup async event loop
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)

        def signal_handler_async(sig, frame):
            print(f"\n🛑 Received signal {sig}")
            self.running = False
            # Create task for graceful shutdown
            loop.create_task(self.shutdown())

        signal.signal(signal.SIGINT, signal_handler_async)
        signal.signal(signal.SIGTERM, signal_handler_async)

        try:
            # Run the async main loop
            success = loop.run_until_complete(self.run_async())
            if not success:
                sys.exit(1)
        except KeyboardInterrupt:
            print("\n🛑 Interrupted by user")
            loop.run_until_complete(self.shutdown())
        finally:
            loop.close()

    def get_status(self):
        """Get status of proxy"""
        return {
            "proxy_port": self.proxy_port,
            "proxy_running": self.runner is not None,
            "config_file": self.config_file,
            "manager_port": self.manager_port,
            "active_connections": len(self.active_connections),
            "concurrent_enabled": True
        }

    # Compatibility methods for config generation (not used in async version)
    def generate_proxy_config(self) -> Dict:
        """Generate proxy configuration for all running servers (legacy)"""
        # This is kept for compatibility but not used in async version
        servers = []  # Would need to be async call
        
        proxy_config = {
            "mcpProxy": {
                "authTokens": [],
                "endpoint": f"http://localhost:{self.proxy_port}"
            },
            "mcpServers": {}
        }

        for server in servers:
            server_name = server['name']
            server_config = self.get_server_config(server_name)
            mode = server_config.get('mode', 'unrestricted')
            
            proxy_config["mcpServers"][server_name] = {
                "command": "python",
                "args": ["concurrent_mcp_server.py", "--mode", mode],
                "env": {
                    "MCP_SERVER_NAME": server_name,
                    "MCP_TRANSPORT": "sse"
                }
            }

        return proxy_config

    def save_proxy_config(self, config: Dict):
        """Save proxy configuration to file (legacy)"""
        try:
            with open(self.config_file, 'w') as f:
                json.dump(config, f, indent=2)
            print(f"✅ Proxy config saved to {self.config_file}")
        except Exception as e:
            print(f"❌ Error saving proxy config: {e}")


if __name__ == "__main__":
    # Load environment variables from .env if available
    try:
        from dotenv import load_dotenv
        load_dotenv()
    except ImportError:
        pass  # dotenv not available, continue with system env vars
    
    manager = MCPSingleProxyManager()  # Now uses PROXY_PORT from .env

    def signal_handler(sig, frame):
        manager.running = False
        manager.stop_proxy()
        sys.exit(0)

    signal.signal(signal.SIGINT, signal_handler)
    manager.run()
