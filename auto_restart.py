#!/usr/bin/env python3
"""
auto_restart.py - Simple script to start all MCP servers from database
"""

import time
import requests
import json
import sys
import os
from dotenv import load_dotenv

# Load environment variables
load_dotenv()

# Get port from environment
PORT = int(os.getenv("PORT", 8999))

def wait_for_manager():
    """Wait until MCP manager is running"""
    print("🔄 Waiting for MCP manager...")
    
    for i in range(30):  # Max 30 seconds
        try:
            response = requests.get(f"http://localhost:{PORT}/", timeout=2)
            if response.status_code == 200:
                print("✅ MCP manager is available")
                return True
        except:
            pass
        
        print(f"   Attempt {i+1}/30...")
        time.sleep(1)
    
    print("❌ MCP manager is not available")
    return False

def get_all_servers():
    """Get all servers from database"""
    try:
        response = requests.get(f"http://localhost:{PORT}/servers", timeout=10)
        if response.status_code == 200:
            data = response.json()
            return data.get('servers', [])
        else:
            print(f"❌ Error getting servers: {response.status_code}")
            return []
    except Exception as e:
        print(f"❌ Error: {e}")
        return []

def start_server(name):
    """Start one server"""
    try:
        response = requests.post(f"http://localhost:{PORT}/servers/{name}/start", timeout=30)
        if response.status_code == 200:
            print(f"✅ Server '{name}' started")
            return True
        else:
            print(f"❌ Error starting '{name}': {response.status_code}")
            return False
    except Exception as e:
        print(f"❌ Error starting '{name}': {e}")
        return False

def main():
    print("🚀 Auto-restart MCP servers")
    print("=" * 40)
    
    # Wait for MCP manager
    if not wait_for_manager():
        sys.exit(1)
    
    # Get all servers
    servers = get_all_servers()
    
    if not servers:
        print("📋 No servers in database")
        return
    
    print(f"📋 Found {len(servers)} servers in database")
    print()
    
    # Start all servers
    started = 0
    failed = 0
    
    for server in servers:
        name = server['name']
        status = server['status']
        
        print(f"🔄 Starting server '{name}'...")
        
        if start_server(name):
            started += 1
        else:
            failed += 1
        
        # Small pause between starts
        time.sleep(2)
    
    print()
    print("=" * 40)
    print(f"📊 Results:")
    print(f"   ✅ Started: {started}")
    print(f"   ❌ Failed: {failed}")
    print(f"   📦 Total: {len(servers)}")

if __name__ == "__main__":
    main()
