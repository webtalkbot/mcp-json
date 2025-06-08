#!/usr/bin/env python3
"""
mcp_manager.py - CLI manager for MCP servers
Modified for your database + added request function
"""

import argparse
import sys
import json
import requests
import os
from dotenv import load_dotenv
from mcp_database import MCPDatabase

# Load environment variables
load_dotenv()

# Get port from environment
PORT = int(os.getenv("PORT", 8999))

def add_server(name: str, script_path: str, description: str = "", auto_start: bool = False):
    """Add new MCP server (without port - communication via stdin/stdout)"""
    db = MCPDatabase()
    
    print(f"ℹ️  Adding MCP server '{name}'...")
    
    success = db.add_server(name, script_path, description, auto_start)
    
    if success:
        print(f"✅ Server {name} added successfully")
    else:
        print(f"❌ Failed to add server {name}")
        return False
    
    return True

def list_servers():
    """Display list of all servers"""
    try:
        response = requests.get(f"http://localhost:{PORT}/servers", timeout=10)
        if response.status_code == 200:
            data = response.json()
            servers = data.get('servers', [])
            
            if servers:
                print("📋 MCP Servers:")
                print(f"{'Name':<20} {'Status':<10} {'PID':<8}")
                print("-" * 40)
                for server in servers:
                    pid = server.get('pid') or 'N/A'
                    print(f"{server['name']:<20} {server['status']:<10} {pid:<8}")
            else:
                print("📋 No servers found")
        else:
            print(f"❌ Error: HTTP {response.status_code}")
    except Exception as e:
        # Fallback to direct database
        print("⚠️  API unavailable, using database...")
        db = MCPDatabase()
        servers = db.list_servers()
        
        if servers:
            print("📋 MCP Servers:")
            print(f"{'Name':<20} {'Status':<10} {'PID':<8}")
            print("-" * 40)
            for server in servers:
                pid = server.get('pid') or 'N/A'
                print(f"{server['name']:<20} {server['status']:<10} {pid:<8}")
        else:
            print("📋 No servers found")

def start_server(name: str):
    """Start MCP server"""
    print(f"ℹ️  Starting MCP server '{name}'...")
    
    try:
        response = requests.post(f"http://localhost:{PORT}/servers/{name}/start", timeout=30)
        if response.status_code == 200:
            print(f"✅ Server {name} started successfully")
        else:
            error_text = response.text
            print(f"❌ Error: HTTP {response.status_code}: {error_text}")
    except Exception as e:
        print(f"❌ Error: {e}")

def stop_server(name: str):
    """Stop MCP server"""
    print(f"ℹ️  Stopping MCP server '{name}'...")
    
    try:
        response = requests.post(f"http://localhost:{PORT}/servers/{name}/stop", timeout=30)
        if response.status_code == 200:
            print(f"✅ Server {name} stopped successfully")
        else:
            error_text = response.text
            print(f"❌ Error: HTTP {response.status_code}: {error_text}")
    except Exception as e:
        print(f"❌ Error: {e}")

def remove_server(name: str):
    """Remove MCP server"""
    print(f"ℹ️  Removing MCP server '{name}'...")
    
    try:
        response = requests.delete(f"http://localhost:{PORT}/servers/{name}", timeout=10)
        if response.status_code == 200:
            print(f"✅ Server {name} removed successfully")
        else:
            error_text = response.text
            print(f"❌ Error: HTTP {response.status_code}: {error_text}")
    except Exception as e:
        print(f"❌ Error: {e}")

def request_server(name: str, method: str, params: dict = None):
    """Send JSON-RPC request to MCP server"""
    print(f"ℹ️  Sending request to server '{name}'...")
    
    # Prepare JSON-RPC request
    request_data = {
        "jsonrpc": "2.0",
        "id": 1,
        "method": method
    }
    
    if params:
        request_data["params"] = params
    
    try:
        response = requests.post(
            f"http://localhost:{PORT}/servers/{name}/request",
            json=request_data,
            timeout=30,
            headers={"Content-Type": "application/json"}
        )
        
        if response.status_code == 200:
            result = response.json()
            print(f"✅ Response from server:")
            print(json.dumps(result, indent=2, ensure_ascii=False))
        else:
            error_text = response.text
            print(f"❌ Error: HTTP {response.status_code}: {error_text}")
            
    except Exception as e:
        print(f"❌ Error: {e}")

def main():
    parser = argparse.ArgumentParser(description='MCP Server Manager CLI')
    subparsers = parser.add_subparsers(dest='command', help='Available commands')
    
    # Add command
    add_parser = subparsers.add_parser('add', help='Add a new MCP server')
    add_parser.add_argument('name', help='Server name')
    add_parser.add_argument('script_path', help='Path to the MCP server script')
    add_parser.add_argument('--description', default='', help='Server description')
    add_parser.add_argument('--auto-start', action='store_true', help='Auto-start server on system startup')
    
    # List command
    list_parser = subparsers.add_parser('list', help='List all MCP servers')
    
    # Start command
    start_parser = subparsers.add_parser('start', help='Start an MCP server')
    start_parser.add_argument('name', help='Server name')
    
    # Stop command
    stop_parser = subparsers.add_parser('stop', help='Stop an MCP server')
    stop_parser.add_argument('name', help='Server name')
    
    # Remove command
    remove_parser = subparsers.add_parser('remove', help='Remove an MCP server')
    remove_parser.add_argument('name', help='Server name')
    
    # Request command - NEW!
    request_parser = subparsers.add_parser('request', help='Send JSON-RPC request to MCP server')
    request_parser.add_argument('name', help='Server name')
    request_parser.add_argument('method', help='JSON-RPC method (e.g., tools/call, tools/list)')
    request_parser.add_argument('--params', help='JSON parameters', default='{}')
    
    args = parser.parse_args()
    
    if args.command == 'add':
        add_server(args.name, args.script_path, args.description, args.auto_start)
    elif args.command == 'list':
        list_servers()
    elif args.command == 'start':
        start_server(args.name)
    elif args.command == 'stop':
        stop_server(args.name)
    elif args.command == 'remove':
        remove_server(args.name)
    elif args.command == 'request':
        try:
            params = json.loads(args.params)
        except json.JSONDecodeError:
            print(f"❌ Invalid JSON in params: {args.params}")
            return
        request_server(args.name, args.method, params)
    else:
        parser.print_help()

if __name__ == "__main__":
    main()
