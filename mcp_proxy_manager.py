#!/usr/bin/env python3
"""
mcp_proxy_manager.py - Unified proxy manager with routing and transport prefixes
"""

import subprocess
import requests
import time
import threading
import signal
import sys
import os
import json


class MCPProxyManager:
    def __init__(self, base_port=9000):
        self.base_port = base_port
        self.proxy_process = None
        self.running = True

    def get_active_servers(self):
        """Get active servers from main wrapper"""
        try:
            response = requests.get("http://localhost:8999/servers", timeout=5)
            if response.status_code == 200:
                data = response.json()
                return [s for s in data.get('servers', []) if s['status'] == 'running']
            return []
        except:
            return []

    def get_server_config(self, server_name):
        """Get server configuration to determine transport type"""
        try:
            # Skús načítať konfiguráciu servera
            config_path = f"servers/{server_name}/{server_name}_config.json"
            if os.path.exists(config_path):
                with open(config_path, 'r') as f:
                    config = json.load(f)
                    return config.get('transport', 'sse')  # default SSE
            return 'sse'  # fallback
        except:
            return 'sse'  # fallback

    def start_unified_proxy(self):
        """Start single mcp-proxy with multiple named servers and transport routing"""
        servers = self.get_active_servers()

        if not servers:
            print("📋 No active servers found")
            return False

        port = self.base_port
        cmd = [
            "mcp-proxy",
            "--port", str(port),
            "--host", "0.0.0.0",
            "--allow-origin", "*", 
            "--pass-environment",
            "--debug"
        ]

        # Group servers by transport type
        transport_groups = {
            'sse': [],
            'streamable': []
        }

        for server in servers:
            server_name = server['name']
            transport = self.get_server_config(server_name)
            transport_groups[transport].append(server)

        # Add named servers for each transport type
        for transport, servers_list in transport_groups.items():
            for server in servers_list:
                server_name = server['name']

                # Create command with environment context
                server_command = f"MCP_SERVER_NAME={server_name} MCP_TRANSPORT={transport} python concurrent_mcp_server.py"

                # Use transport prefix in name
                proxy_name = f"{transport}_{server_name}"

                cmd.extend([
                    "--named-server", 
                    proxy_name,
                    server_command
                ])

        try:
            env = os.environ.copy()
            self.proxy_process = subprocess.Popen(
                cmd, 
                stdout=subprocess.PIPE, 
                stderr=subprocess.PIPE,
                env=env
            )

            print(f"✅ Unified MCP proxy started on port {port}")
            print(f"📡 Available endpoints:")

            # Show endpoints grouped by transport
            for transport, servers_list in transport_groups.items():
                if servers_list:
                    print(f"\n   📋 {transport.upper()} Transport:")
                    for server in servers_list:
                        server_name = server['name']
                        if transport == 'sse':
                            endpoint = f"http://localhost:{port}/servers/sse_{server_name}/sse"
                        else:
                            endpoint = f"http://localhost:{port}/servers/streamable_{server_name}/mcp"
                        print(f"      {server_name}: {endpoint}")

            return True

        except Exception as e:
            print(f"❌ Error starting unified proxy: {e}")
            return False

    def wait_for_manager(self):
        """Wait until MCP manager is running"""
        print("🔄 Waiting for MCP manager...")

        for i in range(30):
            try:
                response = requests.get("http://localhost:8999/health", timeout=2)
                if response.status_code == 200:
                    print("✅ MCP manager is available")
                    return True
            except:
                pass

            print(f"   Attempt {i+1}/30...")
            time.sleep(1)

        print("❌ MCP manager is not available")
        return False

    def show_ngrok_instructions(self):
        """Show instructions for ngrok setup"""
        servers = self.get_active_servers()

        print(f"\n🎯 For external access (Claude.ai, etc.):")
        print(f"   1. Run: ngrok http {self.base_port}")
        print(f"   2. Use these endpoints:")

        for server in servers:
            server_name = server['name']
            transport = self.get_server_config(server_name)

            if transport == 'sse':
                endpoint = f"/servers/sse_{server_name}/sse"
                print(f"      {server_name} (SSE): https://xxx.ngrok.io{endpoint}")
            else:
                endpoint = f"/servers/streamable_{server_name}/mcp"  
                print(f"      {server_name} (HTTP): https://xxx.ngrok.io{endpoint}")

    def run(self):
        """Main loop"""
        print("🚀 MCP Unified Proxy Manager starting...")

        # Wait for main wrapper
        if not self.wait_for_manager():
            sys.exit(1)

        # Start unified proxy
        if self.start_unified_proxy():
            self.show_ngrok_instructions()

            print(f"\n📊 Proxy Status:")
            print(f"   Port: {self.base_port}")
            print(f"   Status: http://localhost:{self.base_port}/status")
        else:
            print("❌ Failed to start unified proxy")
            sys.exit(1)

        # Keep running
        try:
            while self.running:
                time.sleep(1)
        except KeyboardInterrupt:
            print("\n🛑 Shutting down...")
            self.stop_unified_proxy()

    def stop_unified_proxy(self):
        """Stop the unified proxy"""
        if self.proxy_process:
            try:
                self.proxy_process.terminate()
                self.proxy_process.wait(timeout=5)
                print("✅ Unified proxy stopped")
            except:
                self.proxy_process.kill()
        self.proxy_process = None

    def get_proxy_status(self):
        """Get status of unified proxy"""
        try:
            response = requests.get(f"http://localhost:{self.base_port}/status", timeout=2)
            if response.status_code == 200:
                return response.json()
        except:
            pass
        return {"error": "Proxy not responding"}


if __name__ == "__main__":
    manager = MCPProxyManager()

    def signal_handler(sig, frame):
        manager.running = False
        manager.stop_unified_proxy()
        sys.exit(0)

    signal.signal(signal.SIGINT, signal_handler)
    manager.run()
