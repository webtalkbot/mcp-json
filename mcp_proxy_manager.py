#!/usr/bin/env python3
"""
mcp_proxy_manager.py - Single proxy server with path-based routing
One port, multiple servers via path routing
"""

import subprocess
import requests
import time
import threading
import signal
import sys
import os
import json
import asyncio
from typing import Dict, List, Optional

class MCPSingleProxyManager:
    def __init__(self, proxy_port=None):
        # Load proxy port from environment or use default
        if proxy_port is None:
            proxy_port = int(os.getenv("PROXY_PORT", 9000))
        
        self.proxy_port = proxy_port
        self.proxy_process: Optional[subprocess.Popen] = None
        self.running = True
        
        # Load manager port from environment too  
        self.manager_port = int(os.getenv("PORT", 8999))
        self.config_file = "proxy_config.json"
        
        print(f"🔧 Proxy manager initialized:")
        print(f"   Proxy port: {self.proxy_port}")
        print(f"   Manager port: {self.manager_port}")

    def get_active_servers(self) -> List[Dict]:
        """Get active servers from main wrapper"""
        try:
            response = requests.get(f"http://localhost:{self.manager_port}/servers", timeout=5)
            if response.status_code == 200:
                data = response.json()
                return [s for s in data.get('servers', []) if s['status'] == 'running']
            return []
        except Exception as e:
            print(f"Error getting active servers: {e}")
            return []

    def generate_proxy_config(self) -> Dict:
        """Generate proxy configuration for all running servers"""
        servers = self.get_active_servers()
        
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
            
            # Configure server command with mode parameter
            proxy_config["mcpServers"][server_name] = {
                "command": "python",
                "args": ["concurrent_mcp_server.py", "--mode", mode],
                "env": {
                    "MCP_SERVER_NAME": server_name,
                    "MCP_TRANSPORT": "sse"
                }
            }

        return proxy_config

    def get_server_config(self, server_name: str) -> Dict:
        """Get server configuration"""
        try:
            config_path = f"servers/{server_name}/{server_name}_config.json"
            if os.path.exists(config_path):
                with open(config_path, 'r') as f:
                    return json.load(f)
            return {'mode': 'unrestricted'}
        except Exception as e:
            print(f"Error reading config for {server_name}: {e}")
            return {'mode': 'unrestricted'}

    def save_proxy_config(self, config: Dict):
        """Save proxy configuration to file"""
        try:
            with open(self.config_file, 'w') as f:
                json.dump(config, f, indent=2)
            print(f"✅ Proxy config saved to {self.config_file}")
        except Exception as e:
            print(f"❌ Error saving proxy config: {e}")

    def start_single_proxy(self) -> bool:
        """Start single proxy server with all servers using named-server approach"""
        try:
            # Get active servers
            servers = self.get_active_servers()

            if not servers:
                print("📋 No active servers found")
                return False

            # Build command with named servers (sparfenyuk mcp-proxy style)
            cmd = [
                "mcp-proxy",
                "--port", str(self.proxy_port),
                "--host", "0.0.0.0"
            ]

            # Add each server as named server
            for server in servers:
                server_name = server['name']
                server_config = self.get_server_config(server_name)
                mode = server_config.get('mode', 'unrestricted')

                # Add named server - sparfenyuk format
                server_command = f"python concurrent_mcp_server.py --mode {mode}"
                cmd.extend(["--named-server", server_name, server_command])

            print(f"🚀 Starting single proxy server on port {self.proxy_port}")
            print(f"📋 Configured servers: {[s['name'] for s in servers]}")
            print(f"   Command: {' '.join(cmd)}")

            # Set up environment with all server environment variables
            env = os.environ.copy()
            env['PYTHONPATH'] = os.getcwd() + ':' + env.get('PYTHONPATH', '')

            # Add environment variables for all servers
            for server in servers:
                server_name = server['name']
                env[f'MCP_SERVER_NAME'] = server_name  # Will be overridden by each server process
                env[f'MCP_TRANSPORT'] = 'sse'

            self.proxy_process = subprocess.Popen(
                cmd,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                env=env,
                cwd=os.getcwd()
            )

            # Wait and check if process started successfully
            time.sleep(3)
            if self.proxy_process.poll() is None:
                print(f"✅ Single proxy server started successfully")
                self.show_endpoints_sparfenyuk(servers)
                return True
            else:
                print(f"❌ Proxy server failed to start")
                stderr_output = self.proxy_process.stderr.read().decode() if self.proxy_process.stderr else "No stderr"
                print(f"   Error: {stderr_output}")
                return False

        except Exception as e:
            print(f"❌ Error starting single proxy: {e}")
            return False

    def show_endpoints_sparfenyuk(self, servers: List[Dict]):
        """Show available endpoints for sparfenyuk mcp-proxy"""
        print(f"\n🎯 Proxy Endpoints (Single Port: {self.proxy_port}):")
        print(f"{'Server':<20} {'Endpoint':<50} {'Mode':<12}")
        print("-" * 82)

        for server in servers:
            server_name = server['name']
            # sparfenyuk mcp-proxy uses /servers/{name}/sse format
            endpoint = f"http://localhost:{self.proxy_port}/servers/{server_name}/sse"
            server_config = self.get_server_config(server_name)
            mode = server_config.get('mode', 'unrestricted')

            print(f"{server_name:<20} {endpoint:<50} {mode:<12}")

        print(f"\n💡 Usage examples:")
        print(f"   Claude Desktop: Add http://localhost:{self.proxy_port}/servers/{{server_name}}/sse")
        print(f"   Test endpoint: curl http://localhost:{self.proxy_port}/servers/opensubtitles/sse")
        print(f"   All servers available on single port with path routing!")

    def show_endpoints(self, config: Dict):
        """Show available endpoints"""
        print(f"\n🎯 Proxy Endpoints (Single Port: {self.proxy_port}):")
        print(f"{'Server':<20} {'Endpoint':<50} {'Mode':<12}")
        print("-" * 82)

        for server_name in config["mcpServers"].keys():
            endpoint = f"http://localhost:{self.proxy_port}/{server_name}/sse"
            server_config = self.get_server_config(server_name)
            mode = server_config.get('mode', 'unrestricted')
            
            print(f"{server_name:<20} {endpoint:<50} {mode:<12}")

        print(f"\n💡 Usage examples:")
        print(f"   Claude Desktop: Add http://localhost:{self.proxy_port}/{{server_name}}/sse")
        print(f"   Test endpoint: curl http://localhost:{self.proxy_port}/opensubtitles/sse")
        print(f"   All servers available on single port with path routing!")

    def wait_for_manager(self):
        """Wait until MCP manager is running"""
        print("🔄 Waiting for MCP manager...")

        for i in range(30):
            try:
                response = requests.get(f"http://localhost:{self.manager_port}/", timeout=2)
                if response.status_code == 200:
                    print("✅ MCP manager is available")
                    return True
            except:
                pass

            print(f"   Attempt {i+1}/30...")
            time.sleep(1)

        print("❌ MCP manager is not available")
        return False

    def monitor_and_restart(self):
        """Monitor servers and restart proxy as needed"""
        while self.running:
            try:
                time.sleep(60)  # Check every minute
                
                # Check if configuration changed
                current_servers = {s['name'] for s in self.get_active_servers()}
                
                # Read current config
                try:
                    with open(self.config_file, 'r') as f:
                        old_config = json.load(f)
                    old_servers = set(old_config.get("mcpServers", {}).keys())
                except:
                    old_servers = set()

                # If servers changed, restart proxy
                if current_servers != old_servers:
                    print(f"🔄 Server configuration changed")
                    print(f"   Old: {old_servers}")
                    print(f"   New: {current_servers}")
                    
                    self.stop_proxy()
                    time.sleep(2)
                    self.start_single_proxy()

                # Check if proxy process is still running
                if self.proxy_process and self.proxy_process.poll() is not None:
                    print(f"🔄 Proxy process died, restarting...")
                    self.start_single_proxy()

            except Exception as e:
                print(f"❌ Error in monitoring: {e}")
                time.sleep(5)

    def stop_proxy(self):
        """Stop proxy server"""
        if self.proxy_process:
            try:
                self.proxy_process.terminate()
                self.proxy_process.wait(timeout=5)
                print(f"✅ Proxy server stopped")
            except subprocess.TimeoutExpired:
                self.proxy_process.kill()
                print(f"🔪 Proxy server killed")
            except Exception as e:
                print(f"❌ Error stopping proxy: {e}")
            finally:
                self.proxy_process = None

    def run(self):
        """Main loop"""
        print("🚀 MCP Single Proxy Manager starting...")

        # Wait for main wrapper
        if not self.wait_for_manager():
            sys.exit(1)

        # Start single proxy for all servers
        if not self.start_single_proxy():
            print("❌ Failed to start proxy server")
            sys.exit(1)

        # Start monitoring in background
        monitor_thread = threading.Thread(target=self.monitor_and_restart, daemon=True)
        monitor_thread.start()

        # Keep running
        try:
            while self.running:
                time.sleep(1)
        except KeyboardInterrupt:
            print("\n🛑 Shutting down...")
            self.stop_proxy()

    def get_status(self):
        """Get status of proxy"""
        return {
            "proxy_port": self.proxy_port,
            "proxy_running": self.proxy_process is not None and self.proxy_process.poll() is None,
            "config_file": self.config_file,
            "manager_port": self.manager_port
        }


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