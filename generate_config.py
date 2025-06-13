#!/usr/bin/env python3
"""
MCP Proxy Config Generator s automatickým toolFilter
Generuje config.json pre TBXark/mcp-proxy s automatickým filtrovaním nástrojov podľa endpoints.json
"""

import sqlite3
import json
import argparse
import os
import sys
import requests
import glob
from pathlib import Path
from typing import List, Dict, Tuple, Any


def load_endpoints_from_json(servers_dir: str = "./servers") -> Dict[str, List[str]]:
    """
    Načíta endpointy z *_endpoints.json súborov a vytvorí tool filter zoznamy
    """
    server_tools = {}
    
    if not os.path.exists(servers_dir):
        print(f"⚠️  Servers directory not found: {servers_dir}")
        return {}
    
    print(f"🔍 Looking for endpoint files in: {servers_dir}")
    
    # Nájdi všetky *_endpoints.json súbory
    pattern = os.path.join(servers_dir, "**", "*_endpoints.json")
    endpoint_files = glob.glob(pattern, recursive=True)
    
    if not endpoint_files:
        print(f"⚠️  No *_endpoints.json files found in {servers_dir}")
        return {}
    
    for file_path in endpoint_files:
        try:
            # Získaj názov servera z názvu súboru
            filename = os.path.basename(file_path)
            server_name = filename.replace('_endpoints.json', '')
            
            print(f"📋 Processing {filename} → server: {server_name}")
            
            with open(file_path, 'r', encoding='utf-8') as f:
                endpoints = json.load(f)
            
            # Vytvor zoznam tool names pre tento server
            tools_list = []
            for endpoint_name in endpoints.keys():
                tool_name = f"{server_name}__{endpoint_name}"
                tools_list.append(tool_name)
                print(f"   ✅ {tool_name}")
            
            server_tools[server_name] = tools_list
            print(f"📦 {server_name}: {len(tools_list)} tools defined")
            
        except Exception as e:
            print(f"❌ Error processing {file_path}: {e}")
            continue
    
    return server_tools


def get_running_servers_from_api(api_url: str = "http://localhost:8999") -> Dict[str, Dict]:
    """
    Získa zoznam skutočne bežiacich serverov z API
    """
    try:
        print(f"🔍 Getting running servers from API: {api_url}/servers")
        response = requests.get(f"{api_url}/servers", timeout=10)
        if response.status_code == 200:
            data = response.json()
            
            running_servers = {}
            if 'servers' in data and isinstance(data['servers'], list):
                for server in data['servers']:
                    if isinstance(server, dict):
                        name = server.get('name', 'unknown')
                        status = server.get('status', 'unknown')
                        if status == 'running':
                            running_servers[name] = {
                                'pid': server.get('pid'),
                                'transport': server.get('transport', 'sse'),
                                'mode': server.get('mode', 'public'),
                                'status': status
                            }
                            print(f"   ✅ {name}: PID {server.get('pid')}, transport: {server.get('transport', 'sse')}")
                        else:
                            print(f"   ⏸️  {name}: {status}")
                            
                print(f"✅ Found {len(running_servers)} running servers")
                return running_servers
            else:
                print(f"⚠️  Unexpected API response format")
        else:
            print(f"❌ API returned status {response.status_code}")
            
    except Exception as e:
        print(f"❌ Could not get servers from API: {e}")
        
    return {}


def load_servers_from_db(db_path: str = "./data/mcp_servers.db") -> List[Tuple]:
    """
    Načíta servery z databázy (pre config detaily) ale skombinuje s API stavom
    """
    try:
        if not os.path.exists(db_path):
            print(f"Error: Database file '{db_path}' not found!")
            return []

        # Najprv získaj skutočne bežiace servery z API
        running_servers = get_running_servers_from_api()
        if not running_servers:
            print("⚠️  No running servers found via API")
            return []

        print(f"\n📋 Found {len(running_servers)} running servers, loading config from DB...")

        conn = sqlite3.connect(db_path)
        cursor = conn.cursor()

        # Skús načítať config detaily z databázy pre bežiace servery
        converted_servers = []
        
        for server_name, server_info in running_servers.items():
            try:
                # Pokús sa načítať z databázy
                cursor.execute("""
                    SELECT name, script_path, description, config_data
                    FROM mcp_servers
                    WHERE name = ?
                    """, (server_name,))
                
                db_row = cursor.fetchone()
                
                if db_row:
                    name, script_path, description, config_data = db_row
                    
                    # Parsuj config_data ak existuje
                    try:
                        if config_data:
                            config = json.loads(config_data)
                            transport = config.get('transport', server_info.get('transport', 'sse'))
                            mode = config.get('mode', server_info.get('mode', 'public'))
                        else:
                            transport = server_info.get('transport', 'sse')
                            mode = server_info.get('mode', 'public')
                    except:
                        transport = server_info.get('transport', 'sse') 
                        mode = server_info.get('mode', 'public')
                        
                    print(f"   📂 {server_name}: loaded from DB (transport: {transport}, mode: {mode})")
                else:
                    # Server beží ale nie je v DB - použi default config
                    transport = server_info.get('transport', 'sse')
                    mode = server_info.get('mode', 'public')
                    print(f"   🆕 {server_name}: not in DB, using defaults (transport: {transport}, mode: {mode})")

                # Vytvor command, args a env
                command = 'python'
                args = ['concurrent_mcp_server.py', '--mode', mode]
                env = {
                    'MCP_SERVER_NAME': server_name,
                    'MCP_TRANSPORT': transport
                }

                # Konvertuj args a env na stringy pre kompatibilitu
                args_str = json.dumps(args)
                env_str = json.dumps(env)

                converted_servers.append((server_name, command, args_str, env_str, 'running'))
                
            except Exception as e:
                print(f"   ⚠️  Error processing {server_name}: {e}")
                continue

        conn.close()
        return converted_servers

    except sqlite3.Error as e:
        print(f"SQLite error: {e}")
        return []
    except Exception as e:
        print(f"Error loading from database: {e}")
        return []


def parse_env_string(env_str: str) -> Dict[str, str]:
    """Parsuje environment string do dictionary"""
    if not env_str:
        return {}

    env_dict = {}
    try:
        if env_str.strip().startswith('{'):
            env_dict = json.loads(env_str)
        else:
            lines = env_str.replace(',', '\n').split('\n')
            for line in lines:
                line = line.strip()
                if line and '=' in line:
                    key, value = line.split('=', 1)
                    env_dict[key.strip()] = value.strip()
    except Exception as e:
        print(f"Warning: Failed to parse env string: {e}")

    return env_dict


def parse_args_string(args_str: str) -> List[str]:
    """Parsuje arguments string do listu"""
    if not args_str:
        return []

    try:
        if args_str.strip().startswith('['):
            return json.loads(args_str)
        elif ',' in args_str:
            return [arg.strip() for arg in args_str.split(',') if arg.strip()]
        else:
            return args_str.split()
    except Exception as e:
        print(f"Warning: Failed to parse args string: {e}")
        return args_str.split() if args_str else []


def create_config(base_url: str, servers_data: List[Tuple], output_file: str = "config.json", 
                 enable_filter: bool = True, servers_dir: str = "./servers") -> Dict[str, Any]:
    """Vytvorí config.json súbor pre TBXark/mcp-proxy s filtrovaním na základe endpoints.json"""

    config = {
        "mcpProxy": {
            "baseURL": base_url,
            "addr": ":9000", 
            "name": "MCP Proxy",
            "version": "1.0.0",
            "options": {
                "logEnabled": True,
                "authTokens": []
            }
        },
        "mcpServers": {}
    }

    # Načítaj endpoints z JSON súborov ak je filter povolený
    server_tools_from_json = {}
    if enable_filter:
        print(f"🔧 Tool filtering enabled - loading endpoints from JSON files...")
        server_tools_from_json = load_endpoints_from_json(servers_dir)
        
        if not server_tools_from_json:
            print("⚠️  No endpoint files found - no filtering will be applied")
            print(f"💡 Make sure *_endpoints.json files exist in {servers_dir}")

    # Pridaj servery
    for server in servers_data:
        if len(server) >= 4:
            name, command, args_str, env_str = server[:4]
            status = server[4] if len(server) > 4 else "running"
        else:
            print(f"Warning: Incomplete server data: {server}")
            continue

        # Parsuj args a env
        args_list = parse_args_string(args_str)
        env_dict = parse_env_string(env_str)

        # Ak nie sú dostupné detaily, použi predvolené
        if not command:
            command = "python"
        if not args_list:
            args_list = ["concurrent_mcp_server.py", "--mode", "public"]
        if not env_dict:
            env_dict = {
                "MCP_SERVER_NAME": name,
                "MCP_TRANSPORT": "sse"
            }

        server_config = {
            "command": command,
            "args": args_list,
            "env": env_dict,
            "options": {
                "logEnabled": True
            }
        }

        # Pridaj toolFilter na základe endpoints.json ak máme definované nástroje
        if enable_filter and name in server_tools_from_json:
            tools = server_tools_from_json[name]
            server_config["options"]["toolFilter"] = {
                "mode": "allow",
                "list": tools
            }
            print(f"✅ {name}: {len(tools)} tools from endpoints.json")
        elif enable_filter:
            print(f"⚠️  {name}: No endpoints.json found - no filter applied")
        else:
            print(f"ℹ️  {name}: Tool filtering disabled")

        config["mcpServers"][name] = server_config

    # Zapíš do súboru
    try:
        with open(output_file, 'w', encoding='utf-8') as f:
            json.dump(config, f, indent=2, ensure_ascii=False)
        print(f"\n📝 Config file created: {output_file}")
    except Exception as e:
        print(f"Error writing config file: {e}")
        return {}

    return config


def load_url_from_env(env_file: str, var_name: str = "NGROK_URL") -> str:
    """Načíta URL z .env súboru"""
    if not os.path.exists(env_file):
        print(f"Warning: Environment file '{env_file}' not found!")
        return ""

    try:
        with open(env_file, 'r', encoding='utf-8') as f:
            for line in f:
                line = line.strip()
                if line and not line.startswith('#') and '=' in line:
                    key, value = line.split('=', 1)
                    if key.strip() == var_name:
                        return value.strip().strip('"').strip("'")
    except Exception as e:
        print(f"Error reading env file: {e}")

    return ""


def validate_url(url: str) -> bool:
    """Validuje či URL má správny formát"""
    if not url:
        return False
    return url.startswith(('http://', 'https://'))


def show_endpoints(config: Dict[str, Any]) -> None:
    """Zobrazí dostupné endpointy s informáciami o filtrovaní"""
    base_url = config.get("mcpProxy", {}).get("baseURL", "")
    servers = config.get("mcpServers", {})

    if not servers:
        print("No servers configured!")
        return

    print(f"\n🚀 Available endpoints with tool filtering:")
    print(f"📡 Base URL: {base_url}")
    print(f"🔧 Local address: http://localhost:9000")
    print()

    for server_name, server_config in servers.items():
        endpoint = f"{base_url}/{server_name}/sse"
        
        # Zobraz info o filtrovaní
        filter_info = ""
        if "toolFilter" in server_config.get("options", {}):
            tool_filter = server_config["options"]["toolFilter"]
            tool_count = len(tool_filter.get("list", []))
            filter_info = f" ({tool_count} filtered tools)"
        else:
            filter_info = " (all tools)"
            
        print(f"  ✨ {server_name:15} → {endpoint}{filter_info}")

    print(f"\n💡 Usage in Claude.ai:")
    for server_name in servers.keys():
        print(f"   Add: {base_url}/{server_name}/sse")


def main():
    """Hlavná funkcia"""
    parser = argparse.ArgumentParser(
        description='Generate TBXark/mcp-proxy config with tool filtering based on *_endpoints.json files',
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  python generate_config.py --url https://abc123.ngrok-free.app
  python generate_config.py --env-file .env --enable-filter
  python generate_config.py --env-file .env --no-filter --show-endpoints
  python generate_config.py --env-file .env --servers-dir ./servers
        """
    )

    # URL argumenty
    url_group = parser.add_mutually_exclusive_group(required=True)
    url_group.add_argument('--url', help='Base URL (e.g., https://abc123.ngrok-free.app)')
    url_group.add_argument('--env-file', help='Load URL from .env file (default: .env)')

    # Ostatné argumenty
    parser.add_argument('--var-name', default='NGROK_URL', help='Environment variable name (default: NGROK_URL)')
    parser.add_argument('--db', default='./data/mcp_servers.db', help='Database path (default: ./data/mcp_servers.db)')
    parser.add_argument('--output', default='config.json', help='Output config file (default: config.json)')
    parser.add_argument('--show-endpoints', action='store_true', help='Show available endpoints after generation')
    parser.add_argument('--servers-dir', default='./servers', help='Directory with *_endpoints.json files (default: ./servers)')
    
    # Argumenty pre filtrovanie
    filter_group = parser.add_mutually_exclusive_group()
    filter_group.add_argument('--enable-filter', action='store_true', default=True, help='Enable tool filtering based on endpoints.json (default)')
    filter_group.add_argument('--no-filter', action='store_true', help='Disable tool filtering')

    args = parser.parse_args()

    # Zistí či má povoliť filtrovanie
    enable_filter = not args.no_filter

    # Získaj URL
    if args.url:
        base_url = args.url
    else:
        env_file = args.env_file if args.env_file else '.env'
        base_url = load_url_from_env(env_file, args.var_name)
        if not base_url:
            print(f"Error: Could not load {args.var_name} from {env_file}")
            sys.exit(1)

    # Validuj URL
    if not validate_url(base_url):
        print(f"Error: Invalid URL format: {base_url}")
        print("URL must start with http:// or https://")
        sys.exit(1)

    print(f"🔗 Using base URL: {base_url}")
    print(f"📂 Database: {args.db}")
    print(f"📁 Servers directory: {args.servers_dir}")
    print(f"🔧 Tool filtering: {'enabled' if enable_filter else 'disabled'}")

    # Načítaj servery (kombinuje API status + DB config)
    servers = load_servers_from_db(args.db)

    if not servers:
        print("❌ No running servers found!")
        print("\nTips:")
        print("- Make sure mcp_wrapper.py is running: python mcp_wrapper.py")
        print("- Check server status: python mcp_manager.py list")
        sys.exit(1)

    print(f"\n✅ Found {len(servers)} running servers:")
    for i, server in enumerate(servers, 1):
        name = server[0]
        command = server[1] if len(server) > 1 else "python"
        print(f"  {i}. {name} ({command})")

    # Vytvor config
    config = create_config(base_url, servers, args.output, enable_filter, args.servers_dir)

    if not config:
        print("❌ Failed to create config!")
        sys.exit(1)

    # Zobraz endpointy
    if args.show_endpoints:
        show_endpoints(config)

    filter_status = "with tool filtering from endpoints.json" if enable_filter else "without filtering"
    print(f"\n🎯 Configuration completed {filter_status}!")
    print(f"📝 Config file: {args.output}")
    print(f"🚀 Start proxy: mcp-proxy --config {args.output}")


if __name__ == "__main__":
    main()