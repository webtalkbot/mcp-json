#!/usr/bin/env python3
"""
mcp_proxy_manager.py - MCP Proxy Manager that creates individual proxy instances
for each running MCP server with proper transport routing
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

class MCPProxyManager:
    def __init__(self, base_port=9000):
        self.base_port = base_port
        self.proxy_processes: Dict[str, subprocess.Popen] = {}
        self.server_ports: Dict[str, int] = {}
        self.running = True
        self.manager_port = 8999  # Default MCP wrapper port

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

    def get_server_config(self, server_name: str) -> Dict:
        """Get server configuration including transport type"""
        try:
            config_path = f"servers/{server_name}/{server_name}_config.json"
            if os.path.exists(config_path):
                with open(config_path, 'r') as f:
                    config = json.load(f)
                    return {
                        'transport': config.get('transport', 'sse'),
                        'mode': config.get('mode', 'unrestricted')
                    }
            return {'transport': 'sse', 'mode': 'unrestricted'}
        except Exception as e:
            print(f"Error reading config for {server_name}: {e}")
            return {'transport': 'sse', 'mode': 'unrestricted'}

    def start_proxy_for_server(self, server_name: str, port: int) -> bool:
        """Start individual mcp-proxy for a specific server"""
        try:
            server_config = self.get_server_config(server_name)
            transport = server_config['transport']
            mode = server_config['mode']

            # Create proxy command for individual server
            cmd = [
                "mcp-proxy",
                "--port", str(port),
                "--host", "0.0.0.0",
                "--allow-origin", "*",
                "--pass-environment",
                "--debug"
            ]

            # Create server command with proper environment
            server_command = f"python concurrent_mcp_server.py --mode {mode}"

            # Add named server
            cmd.extend([
                "--named-server",
                server_name,
                server_command
            ])

            print(f"🚀 Starting proxy for {server_name} on port {port}")
            print(f"   Transport: {transport}, Mode: {mode}")
            print(f"   Command: {' '.join(cmd)}")

            # Set up environment with MCP server variables
            env = os.environ.copy()
            env['MCP_SERVER_NAME'] = server_name
            env['MCP_TRANSPORT'] = transport
            
            process = subprocess.Popen(
                cmd,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                env=env,
                cwd=os.getcwd()  # Ensure working directory is set
            )

            # Store process info
            self.proxy_processes[server_name] = process
            self.server_ports[server_name] = port

            # Wait a bit and check if process started successfully
            time.sleep(2)
            if process.poll() is None:
                print(f"✅ Proxy for {server_name} started successfully")
                
                # Show endpoints
                if transport == 'sse':
                    endpoint = f"http://localhost:{port}/servers/{server_name}/sse"
                else:
                    endpoint = f"http://localhost:{port}/servers/{server_name}/mcp"
                
                print(f"   Endpoint: {endpoint}")
                return True
            else:
                print(f"❌ Proxy for {server_name} failed to start")
                stderr_output = process.stderr.read().decode() if process.stderr else "No stderr"
                print(f"   Error: {stderr_output}")
                return False

        except Exception as e:
            print(f"❌ Error starting proxy for {server_name}: {e}")
            return False

    def stop_proxy_for_server(self, server_name: str):
        """Stop proxy for specific server"""
        if server_name in self.proxy_processes:
            try:
                process = self.proxy_processes[server_name]
                process.terminate()
                process.wait(timeout=5)
                print(f"✅ Stopped proxy for {server_name}")
            except subprocess.TimeoutExpired:
                process.kill()
                print(f"🔪 Killed proxy for {server_name}")
            except Exception as e:
                print(f"❌ Error stopping proxy for {server_name}: {e}")
            finally:
                del self.proxy_processes[server_name]
                if server_name in self.server_ports:
                    del self.server_ports[server_name]

    def start_all_proxies(self):
        """Start proxies for all running servers"""
        servers = self.get_active_servers()
        
        if not servers:
            print("📋 No active servers found")
            return False

        print(f"📋 Starting proxies for {len(servers)} active servers")
        
        success_count = 0
        current_port = self.base_port

        for server in servers:
            server_name = server['name']
            
            if self.start_proxy_for_server(server_name, current_port):
                success_count += 1
                current_port += 1
            else:
                print(f"❌ Failed to start proxy for {server_name}")

        print(f"\n📊 Proxy startup results:")
        print(f"   ✅ Started: {success_count}")
        print(f"   ❌ Failed: {len(servers) - success_count}")
        print(f"   📦 Total servers: {len(servers)}")

        return success_count > 0

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

    def show_summary(self):
        """Show summary of all proxy endpoints"""
        if not self.proxy_processes:
            print("📋 No proxy instances running")
            return

        print(f"\n🎯 Active Proxy Endpoints:")
        print(f"{'Server':<20} {'Port':<6} {'Transport':<12} {'Endpoint'}")
        print("-" * 80)

        for server_name, port in self.server_ports.items():
            server_config = self.get_server_config(server_name)
            transport = server_config['transport']
            
            if transport == 'sse':
                endpoint = f"http://localhost:{port}/servers/{server_name}/sse"
            else:
                endpoint = f"http://localhost:{port}/servers/{server_name}/mcp"
            
            print(f"{server_name:<20} {port:<6} {transport:<12} {endpoint}")

        print(f"\n💡 External access (ngrok):")
        print(f"   1. Run: ngrok http {self.base_port}-{self.base_port + len(self.server_ports)}")
        print(f"   2. Replace localhost with ngrok URL in endpoints above")

    def monitor_and_restart(self):
        """Monitor servers and restart proxies as needed"""
        while self.running:
            try:
                time.sleep(30)  # Check every 30 seconds
                
                current_servers = {s['name']: s for s in self.get_active_servers()}
                
                # Check for new servers
                for server_name in current_servers:
                    if server_name not in self.proxy_processes:
                        print(f"🆕 New server detected: {server_name}")
                        port = max(self.server_ports.values()) + 1 if self.server_ports else self.base_port
                        self.start_proxy_for_server(server_name, port)
                
                # Check for stopped servers
                stopped_servers = []
                for server_name in list(self.proxy_processes.keys()):
                    if server_name not in current_servers:
                        print(f"🛑 Server stopped: {server_name}")
                        stopped_servers.append(server_name)
                
                # Clean up stopped servers
                for server_name in stopped_servers:
                    self.stop_proxy_for_server(server_name)
                
                # Check if proxy processes are still running
                for server_name in list(self.proxy_processes.keys()):
                    process = self.proxy_processes[server_name]
                    if process.poll() is not None:
                        print(f"🔄 Proxy for {server_name} died, restarting...")
                        port = self.server_ports[server_name]
                        self.stop_proxy_for_server(server_name)
                        self.start_proxy_for_server(server_name, port)

            except Exception as e:
                print(f"❌ Error in monitoring: {e}")
                time.sleep(5)

    def run(self):
        """Main loop"""
        print("🚀 MCP Proxy Manager starting...")

        # Wait for main wrapper
        if not self.wait_for_manager():
            sys.exit(1)

        # Start proxies for all servers
        if self.start_all_proxies():
            self.show_summary()
        else:
            print("❌ Failed to start any proxy instances")
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
            self.stop_all_proxies()

    def stop_all_proxies(self):
        """Stop all proxy instances"""
        print("🔄 Stopping all proxy instances...")
        self.running = False
        
        for server_name in list(self.proxy_processes.keys()):
            self.stop_proxy_for_server(server_name)
        
        print("✅ All proxy instances stopped")

    def get_status(self):
        """Get status of all proxy instances"""
        status = {
            "running_proxies": len(self.proxy_processes),
            "base_port": self.base_port,
            "proxies": {}
        }
        
        for server_name, port in self.server_ports.items():
            process = self.proxy_processes.get(server_name)
            server_config = self.get_server_config(server_name)
            
            status["proxies"][server_name] = {
                "port": port,
                "transport": server_config['transport'],
                "mode": server_config['mode'],
                "running": process is not None and process.poll() is None,
                "endpoint": f"http://localhost:{port}/servers/{server_name}/{'sse' if server_config['transport'] == 'sse' else 'mcp'}"
            }
        
        return status


if __name__ == "__main__":
    manager = MCPProxyManager()

    def signal_handler(sig, frame):
        manager.running = False
        manager.stop_all_proxies()
        sys.exit(0)

    signal.signal(signal.SIGINT, signal_handler)
    manager.run()
