#!/usr/bin/env python3
"""
MCP Proxy Config Generator
Generuje config.json pre TBXark/mcp-proxy na základe serverov v mcp_servers.db bez filtrovania nástrojov
"""

import sqlite3
import json
import argparse
import os
import sys
from pathlib import Path
from typing import List, Dict, Tuple, Any


def load_servers_from_db(db_path: str = "./data/mcp_servers.db") -> List[Tuple]:
    """
    Retrieves servers from SQLite database with new structure
    """
    try:
        if not os.path.exists(db_path):
            print(f"Error: Database file '{db_path}' not found!")
            return []

        conn = sqlite3.connect(db_path)
        cursor = conn.cursor()

        # Skús zistiť štruktúru tabulky
        cursor.execute("SELECT name FROM sqlite_master WHERE type='table';")
        tables = cursor.fetchall()
        print(f"Found tables: {[table[0] for table in tables]}")

        # Načítaj z tabuľky mcp_servers s novou štruktúrou
        try:
            cursor.execute("PRAGMA table_info(mcp_servers)")
            columns = [col[1] for col in cursor.fetchall()]
            print(f"Columns in mcp_servers: {columns}")

            # Načítaj len bežiace servery
            query = """
                    SELECT name, script_path, description, status, config_data
                    FROM mcp_servers
                    WHERE status = 'running'
                    """
            cursor.execute(query)
            servers = cursor.fetchall()

            # Konvertuj do formátu (name, command, args, env, status)
            converted_servers = []
            for server in servers:
                name, script_path, description, status, config_data = server

                # Parsuj config_data ak existuje
                try:
                    if config_data:
                        config = json.loads(config_data)
                        transport = config.get('transport', 'sse')
                        mode = config.get('mode', 'public')
                    else:
                        transport = 'sse'
                        mode = 'public'
                except:
                    transport = 'sse'
                    mode = 'public'

                # Vytvor command, args a env na základe script_path
                command = 'python'
                args = ['concurrent_mcp_server.py', '--mode', mode]
                env = {
                    'MCP_SERVER_NAME': name,
                    'MCP_TRANSPORT': transport
                }

                # Konvertuj args a env na stringy pre kompatibilitu
                args_str = json.dumps(args)
                env_str = json.dumps(env)

                converted_servers.append((name, command, args_str, env_str, status))
                print(f"Loaded server: {name} (transport: {transport}, mode: {mode})")

            conn.close()
            return converted_servers

        except sqlite3.OperationalError as e:
            print(f"Error reading mcp_servers table: {e}")
            conn.close()
            return []

    except sqlite3.Error as e:
        print(f"SQLite error: {e}")
        return []
    except Exception as e:
        print(f"Error loading database: {e}")
        return []


def get_server_details_from_api(base_url: str = "http://localhost:8999") -> List[Dict]:
    """
    Pokúsi sa získať detaily serverov z API ako fallback
    """
    try:
        import requests
        response = requests.get(f"{base_url}/servers", timeout=5)
        if response.status_code == 200:
            data = response.json()
            print(f"API response type: {type(data)}")

            # API vracia objekt s 'servers' kľúčom
            if isinstance(data, dict) and 'servers' in data:
                servers_list = data['servers']
                print(f"Found {len(servers_list)} servers in API response")

                result = []
                for server in servers_list:
                    if isinstance(server, dict):
                        result.append({
                            'name': server.get('name', 'unknown'),
                            'status': server.get('status', 'running'),
                            'transport': server.get('transport', 'sse')
                        })
                    elif isinstance(server, str):
                        result.append({
                            'name': server,
                            'status': 'running',
                            'transport': 'sse'
                        })

                return result
            else:
                print(f"Unexpected API response format: {data}")
                return []

    except Exception as e:
        print(f"Could not fetch from API: {e}")
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


def create_config_from_api_fallback(base_url: str, api_url: str = "http://localhost:8999") -> Dict[str, Any]:
    """Vytvorí config na základe API ako fallback bez filtrovania"""
    servers_data = get_server_details_from_api(api_url)

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

    for server in servers_data:
        name = server.get('name', 'unknown')
        transport = server.get('transport', 'sse')

        server_config = {
            "command": "python",
            "args": [
                "concurrent_mcp_server.py",
                "--mode",
                "public"
            ],
            "env": {
                "MCP_SERVER_NAME": name,
                "MCP_TRANSPORT": transport
            },
            "options": {
                "logEnabled": True
            }
        }

        config["mcpServers"][name] = server_config
        print(f"Added server '{name}' from API (transport: {transport}, no filter)")

    return config


def create_config(base_url: str, servers_data: List[Tuple], output_file: str = "config.json") -> Dict[str, Any]:
    """Vytvorí config.json súbor pre TBXark/mcp-proxy bez filtrovania"""

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

    # Pridaj servery z databázy
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

        config["mcpServers"][name] = server_config
        print(f"Added server '{name}': {command} {' '.join(args_list)} (no filter)")

    # Zapíš do súboru
    try:
        with open(output_file, 'w', encoding='utf-8') as f:
            json.dump(config, f, indent=2, ensure_ascii=False)
        print(f"\nConfig file created: {output_file}")
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
    """Zobrazí dostupné endpointy bez informácií o filtrovaní"""
    base_url = config.get("mcpProxy", {}).get("baseURL", "")
    servers = config.get("mcpServers", {})

    if not servers:
        print("No servers configured!")
        return

    print(f"\n🚀 Available endpoints:")
    print(f"📡 Base URL: {base_url}")
    print(f"🔧 Local address: http://localhost:9000")
    print()

    for server_name in servers.keys():
        endpoint = f"{base_url}/{server_name}/sse"
        print(f"  ✨ {server_name:15} → {endpoint}")

    print(f"\n💡 Usage in Claude.ai:")
    for server_name in servers.keys():
        print(f"   Add: {base_url}/{server_name}/sse")


def main():
    """Hlavná funkcia"""
    parser = argparse.ArgumentParser(
        description='Generate TBXark/mcp-proxy config from mcp_servers.db without tool filtering',
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  python generate_config.py --url https://abc123.ngrok-free.app
  python generate_config.py --env-file .env
  python generate_config.py --env-file .env --use-api-fallback --show-endpoints
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
    parser.add_argument('--use-api-fallback', action='store_true', help='Use API as fallback if database fails')

    args = parser.parse_args()

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

    # Načítaj servery z databázy
    servers = load_servers_from_db(args.db)

    # Ak databáza zlyhá a je povolený API fallback
    if not servers and args.use_api_fallback:
        print("⚠️  Database failed, trying API fallback...")
        config = create_config_from_api_fallback(base_url)

        if config.get("mcpServers"):
            print(f"✅ Found {len(config['mcpServers'])} servers via API")

            # Zapíš config
            try:
                with open(args.output, 'w', encoding='utf-8') as f:
                    json.dump(config, f, indent=2, ensure_ascii=False)
                print(f"📝 Config file created: {args.output}")

                if args.show_endpoints:
                    show_endpoints(config)

                print(f"\n🎯 Configuration completed without filtering!")
                return

            except Exception as e:
                print(f"Error writing config file: {e}")
                sys.exit(1)

    if not servers:
        print("❌ No running servers found!")
        print("\nTips:")
        print("- Check if any servers have status 'running'")
        print("- Try: python mcp_manager.py list")
        print("- Try --use-api-fallback option")
        sys.exit(1)

    print(f"\n✅ Found {len(servers)} running servers:")
    for i, server in enumerate(servers, 1):
        name = server[0]
        command = server[1] if len(server) > 1 else "python"
        status = server[4] if len(server) > 4 else "running"
        print(f"  {i}. {name} ({command}) [{status}]")

    # Vytvor config
    config = create_config(base_url, servers, args.output)

    if not config:
        print("❌ Failed to create config!")
        sys.exit(1)

    # Zobraz endpointy
    if args.show_endpoints:
        show_endpoints(config)

    print(f"\n🎯 Configuration completed without filtering!")
    print(f"📝 Config file: {args.output}")
    print(f"🚀 Start proxy: mcp-proxy --config {args.output}")


if __name__ == "__main__":
    main()