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
import time
import shutil  # Pre odstránenie directories
import subprocess  # Pridaj ak nie je
from dotenv import load_dotenv
from mcp_database import MCPDatabase

# Load environment variables
load_dotenv()

# Get port from environment
PORT = int(os.getenv("PORT", 8999))

def add_server(name: str, script_path: str, description: str = "", auto_start: bool = False, transport: str = "sse"):
    """Add new MCP server with transport type"""
    db = MCPDatabase()

    print(f"ℹ️  Adding MCP server '{name}' with {transport} transport...")

    # Validácia transport typu
    if transport not in ['sse', 'streamable']:
        print(f"❌ Error: Invalid transport type '{transport}'. Use 'sse' or 'streamable'")
        return False

    # Convert to absolute path and validate
    abs_path = os.path.abspath(script_path)

    if not os.path.exists(abs_path):
        print(f"❌ Error: Script file does not exist: {abs_path}")
        return False

    if not os.access(abs_path, os.R_OK):
        print(f"❌ Error: Script file is not readable: {abs_path}")
        return False

    # Store relative path if in current directory
    if abs_path.startswith(os.getcwd()):
        rel_path = os.path.relpath(abs_path)
        print(f"ℹ️  Storing relative path: {rel_path}")
        script_path = rel_path

    # Store config with transport info
    config_data = {
        "transport": transport,
        "description": description,
        "created_at": time.time()
    }

    success = db.add_server(name, script_path, description, auto_start, config_data)

    if success:
        # Create server config file
        create_server_config(name, transport)
        print(f"✅ Server {name} added successfully with {transport} transport")

        # Skús informovať API ak je dostupné
        try:
            requests.post(f"http://localhost:{PORT}/reload", timeout=2)
            print(f"ℹ️  Notified API wrapper about new server")
        except:
            print(f"ℹ️  API wrapper not available (will pick up server on next start)")

    else:
        print(f"❌ Failed to add server {name}")
        return False

    return True


def create_server_config(server_name: str, transport: str):
    """Create server configuration file"""
    server_dir = f"servers/{server_name}"
    os.makedirs(server_dir, exist_ok=True)

    config_file = f"{server_dir}/{server_name}_config.json"
    config = {
        "transport": transport,
        "created_at": time.time()
    }

    with open(config_file, 'w') as f:
        json.dump(config, f, indent=2)

    print(f"ℹ️  Created config file: {config_file}")

def list_servers(quiet_detection=False):
    """List all MCP servers with enhanced info including standalone processes"""
    try:
        response = requests.get(f"http://localhost:{PORT}/servers", timeout=5)
        if response.status_code == 200:
            data = response.json()
            servers = data.get('servers', [])

            print("📋 MCP Servers (Live Status via API):")
            print(f"{'Name':<20} {'Status':<10} {'PID':<8} {'Transport':<12} {'Auto Start':<10} {'Description':<30}")
            print("-" * 100)

            for server in servers:
                name = server.get('name', 'Unknown')
                status = server.get('status', 'unknown')
                pid = server.get('pid', 'N/A')
                transport = server.get('transport', 'sse')

                # Get auto_start and description from database
                db = MCPDatabase()
                server_info = db.get_server(name)
                auto_start = "Yes" if server_info and server_info.get('auto_start') else "No"
                description = server_info.get('description', '')[:29] if server_info else ''

                print(f"{name:<20} {status:<10} {pid:<8} {transport:<12} {auto_start:<10} {description:<30}")

            if not servers:
                print("No servers found")

        else:
            raise requests.exceptions.RequestException("API not responding properly")

    except requests.exceptions.RequestException:
        if not quiet_detection:
            print("⚠️  API unavailable, using database with process detection...")

        # Fallback na databázu + process detection
        db = MCPDatabase()
        servers = db.list_servers()

        print("📋 MCP Servers (Database + Process Detection):")
        print(f"{'Name':<20} {'Status':<10} {'PID':<8} {'Transport':<12} {'Auto Start':<10} {'Description':<30}")
        print("-" * 100)

        for server in servers:
            name = server.get('name', 'Unknown')
            auto_start = 'Yes' if server.get('auto_start', False) else 'No'
            description = server.get('description', '')[:29]

            # Skús načítať transport z config súboru
            transport = 'sse'  # default
            try:
                config_path = f"servers/{name}/{name}_config.json"
                if os.path.exists(config_path):
                    with open(config_path, 'r') as f:
                        config = json.load(f)
                        transport = config.get('transport', 'sse')
            except:
                pass

            # ✅ NOVÁ LOGIKA: Kombinuj databázu + process detection
            status, pid = get_server_actual_status(server, name)

            print(f"{name:<20} {status:<10} {pid:<8} {transport:<12} {auto_start:<10} {description:<30}")

        if not servers:
            print("No servers found in database")

def get_server_actual_status(server_data, server_name):
    """Get actual server status combining database and process detection"""
    db_status = server_data.get('status', 'stopped')
    db_pid = server_data.get('pid')

    # Ak je v databáze stopped, skontroluj procesy
    if db_status == 'stopped':
        process_status, process_pid = detect_standalone_server(server_name)
        return process_status, process_pid

    # Ak je v databáze running, overiť či process skutočne beží
    if db_status == 'running' and db_pid:
        # Skontroluj či PID stále existuje
        if is_pid_running(db_pid, server_name):
            return 'running', str(db_pid)
        else:
            # Process je mŕtvy, aktualizuj databázu
            db = MCPDatabase()
            db.update_server_status(server_name, 'stopped')
            return 'stopped (crashed)', 'N/A'

    # Fallback na process detection
    return detect_standalone_server(server_name)

def is_pid_running(pid, expected_server_name):
    """Check if PID is still running and belongs to expected server"""
    try:
        import psutil

        try:
            proc = psutil.Process(int(pid))
            cmdline = proc.cmdline()
            environ = proc.environ()

            # Skontroluj či je to náš MCP server
            if ('python' in str(cmdline).lower() and 
                'concurrent_mcp_server.py' in ' '.join(cmdline) and
                environ.get('MCP_SERVER_NAME') == expected_server_name):
                return True

        except (psutil.NoSuchProcess, psutil.AccessDenied, ValueError):
            return False

    except ImportError:
        # Bez psutil nemôžeme overiť
        return False

    return False

def detect_standalone_server(server_name):
    """Detect if server is running as standalone process"""
    try:
        import psutil

        for proc in psutil.process_iter(['pid', 'name', 'cmdline', 'environ']):
            try:
                cmdline = proc.info['cmdline']
                environ = proc.info['environ'] or {}

                # Skontroluj či proces patrí k nášmu serveru
                if (cmdline and 'python' in str(cmdline).lower() and 
                    'concurrent_mcp_server.py' in ' '.join(cmdline) and
                    environ.get('MCP_SERVER_NAME') == server_name):

                    return 'running', str(proc.info['pid'])

            except (psutil.NoSuchProcess, psutil.AccessDenied, KeyError, TypeError):
                continue

        return 'stopped', 'N/A'

    except ImportError:
        return 'stopped', 'N/A'
    except Exception:
        return 'stopped', 'N/A'

def get_server_processes(server_name=None):
    """Get list of running MCP server processes"""
    processes = []

    try:
        import psutil

        for proc in psutil.process_iter(['pid', 'name', 'cmdline', 'environ', 'create_time']):
            try:
                cmdline = proc.info['cmdline']
                environ = proc.info['environ'] or {}

                # Skontroluj či proces patrí k MCP serveru
                if (cmdline and 'python' in cmdline[0].lower() and 
                    'concurrent_mcp_server.py' in ' '.join(cmdline)):

                    process_server_name = environ.get('MCP_SERVER_NAME', 'unknown')

                    # Ak hľadáme konkrétny server
                    if server_name and process_server_name != server_name:
                        continue

                    processes.append({
                        'pid': proc.info['pid'],
                        'server_name': process_server_name,
                        'transport': environ.get('MCP_TRANSPORT', 'sse'),
                        'started': proc.info['create_time']
                    })

            except (psutil.NoSuchProcess, psutil.AccessDenied):
                continue

    except ImportError:
        print("⚠️  psutil not available for process detection")
        print("💡 Install with: pip install psutil")
    except Exception as e:
        print(f"⚠️  Error detecting processes: {e}")

    return processes

def start_server(name: str):
    """Start MCP server (with database fallback and standalone mode)"""
    print(f"ℹ️  Starting MCP server '{name}'...")

    # Najprv skús cez API
    try:
        response = requests.post(f"http://localhost:{PORT}/servers/{name}/start", timeout=5)
        if response.status_code == 200:
            print(f"✅ Server {name} started successfully via API")
            return True
        else:
            print(f"❌ Failed to start server {name} via API: {response.text}")
            return False
    except requests.exceptions.RequestException:
        print(f"⚠️  API unavailable, starting server in standalone mode...")

        # Fallback - spusti server priamo
        return start_server_standalone(name)

def start_server_standalone(name: str):
    """Start server directly without API wrapper with proper environment setup"""
    db = MCPDatabase()
    servers = db.list_servers()

    # Nájdi server v databáze
    server_data = None
    for server in servers:
        if server['name'] == name:
            server_data = server
            break

    if not server_data:
        print(f"❌ Server '{name}' not found in database")
        return False

    script_path = server_data['script_path']

    # Skontroluj či súbor existuje
    if not os.path.exists(script_path):
        print(f"❌ Script file not found: {script_path}")
        return False

    try:
        # Načítaj transport z config súboru
        transport = 'sse'  # default
        try:
            config_path = f"servers/{name}/{name}_config.json"
            if os.path.exists(config_path):
                with open(config_path, 'r') as f:
                    config = json.load(f)
                    transport = config.get('transport', 'sse')
        except:
            pass

        # ✅ KĽÚČOVÉ: Nastavenie environment variables
        env = os.environ.copy()
        env['MCP_SERVER_NAME'] = name
        env['MCP_TRANSPORT'] = transport
        env['PYTHONPATH'] = os.getcwd() + ':' + env.get('PYTHONPATH', '')

        print(f"🚀 Starting standalone server with:")
        print(f"   Script: {script_path}")
        print(f"   MCP_SERVER_NAME: {name}")
        print(f"   MCP_TRANSPORT: {transport}")
        print(f"   Working directory: {os.getcwd()}")

        # Spusti server ako subprocess s správnym environment
        process = subprocess.Popen(
            [sys.executable, script_path],
            env=env,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            cwd=os.getcwd()
        )

        # Krátke čakanie aby sa server spustil
        time.sleep(2)

        # Skontroluj či process ešte beží
        if process.poll() is None:
            print(f"✅ Server {name} started in standalone mode (PID: {process.pid})")
            print(f"ℹ️  Transport: {transport}")
            print(f"⚠️  Note: Server is running independently, not managed by wrapper")

            # Aktualizuj databázu s PID (ak je to možné)
            try:
                db.update_server_status(name, 'running', process.pid)
                print(f"ℹ️  Database updated with PID {process.pid}")
            except Exception as e:
                print(f"⚠️  Could not update database: {e}")

            return True
        else:
            exit_code = process.poll()
            stderr_output = ""
            try:
                stderr_output = process.stderr.read().decode()
            except:
                pass

            print(f"❌ Server {name} failed to start in standalone mode (exit code: {exit_code})")
            if stderr_output:
                print(f"❌ Error output: {stderr_output}")
            return False

    except Exception as e:
        print(f"❌ Error starting server {name}: {e}")
        return False

def stop_server(name: str):
    """Stop MCP server (with API and process fallback)"""
    print(f"ℹ️  Stopping MCP server '{name}'...")

    # Najprv skús cez API
    try:
        response = requests.post(f"http://localhost:{PORT}/servers/{name}/stop", timeout=5)
        if response.status_code == 200:
            print(f"✅ Server {name} stopped successfully via API")
            return True
        else:
            print(f"❌ Failed to stop server {name} via API: {response.text}")
    except requests.exceptions.RequestException:
        print(f"⚠️  API unavailable, trying to stop server processes...")

    # Fallback - pokús sa nájsť a zastaviť procesy
    return stop_server_processes(name)

def stop_server_processes(name: str):
    """Stop server by finding and killing its processes"""
    import psutil

    stopped_count = 0

    try:
        # Nájdi procesy ktoré obsahujú názov servera
        for proc in psutil.process_iter(['pid', 'name', 'cmdline', 'environ']):
            try:
                cmdline = proc.info['cmdline']
                environ = proc.info['environ'] or {}

                # Skontroluj či proces patrí k nášmu serveru
                if (cmdline and 'python' in cmdline[0].lower() and 
                    'concurrent_mcp_server.py' in ' '.join(cmdline) and
                    environ.get('MCP_SERVER_NAME') == name):

                    print(f"ℹ️  Found server process: PID {proc.info['pid']}")
                    proc.terminate()

                    # Čakaj na ukončenie
                    try:
                        proc.wait(timeout=5)
                        stopped_count += 1
                        print(f"✅ Stopped process PID {proc.info['pid']}")
                    except psutil.TimeoutExpired:
                        proc.kill()
                        stopped_count += 1
                        print(f"🔪 Killed process PID {proc.info['pid']} (force)")

            except (psutil.NoSuchProcess, psutil.AccessDenied):
                continue

    except ImportError:
        print(f"⚠️  psutil not available, cannot stop processes automatically")
        print(f"💡 Install with: pip install psutil")
        print(f"💡 Or manually kill processes for server '{name}'")
        return False
    except Exception as e:
        print(f"❌ Error stopping processes: {e}")
        return False

    if stopped_count > 0:
        print(f"✅ Stopped {stopped_count} process(es) for server '{name}'")
        return True
    else:
        print(f"ℹ️  No running processes found for server '{name}'")
        return True

def remove_server(name: str):
    """Remove MCP server (works with or without running API)"""
    print(f"ℹ️  Removing MCP server '{name}'...")

    db = MCPDatabase()

    # Najprv skús cez API (ak je dostupné)
    try:
        response = requests.delete(f"http://localhost:{PORT}/servers/{name}", timeout=5)
        if response.status_code == 200:
            print(f"✅ Server {name} removed successfully via API")
            return True
        elif response.status_code == 404:
            print(f"⚠️  Server {name} not found in API, checking database...")
        else:
            print(f"⚠️  API error (status {response.status_code}), falling back to database...")
    except requests.exceptions.RequestException as e:
        print(f"⚠️  API unavailable ({type(e).__name__}), removing from database...")

    # Fallback na databázu
    try:
        # Skontroluj či server existuje v databáze
        servers = db.list_servers()
        server_exists = any(server['name'] == name for server in servers)

        if not server_exists:
            print(f"❌ Server '{name}' not found in database")
            return False

        # Odstráň z databázy
        if db.remove_server(name):
            print(f"✅ Server {name} removed from database")

            # Server directory is preserved
            server_dir = f"servers/{name}"
            if os.path.exists(server_dir):
                print(f"ℹ️  Server directory preserved: {server_dir}")
            else:
                print(f"ℹ️  No server directory found at: {server_dir}")

            return True
        else:
            print(f"❌ Failed to remove server {name} from database")
            return False

    except Exception as e:
        print(f"❌ Database error: {e}")
        return False

def debug_all_processes():
    """Debug function to show all MCP-related processes"""
    try:
        import psutil

        print("🔍 All Python processes with concurrent_mcp_server.py:")
        found_any = False

        for proc in psutil.process_iter(['pid', 'name', 'cmdline', 'environ']):
            try:
                cmdline = proc.info['cmdline']
                environ = proc.info['environ'] or {}

                if (cmdline and 'python' in str(cmdline).lower() and 
                    'concurrent_mcp_server.py' in ' '.join(cmdline)):

                    found_any = True
                    print(f"\n📋 Process PID {proc.info['pid']}:")
                    print(f"   Command: {' '.join(cmdline)}")
                    print(f"   MCP_SERVER_NAME: {environ.get('MCP_SERVER_NAME', 'NOT_SET')}")
                    print(f"   MCP_TRANSPORT: {environ.get('MCP_TRANSPORT', 'NOT_SET')}")
                    print(f"   Working dir: {proc.cwd() if hasattr(proc, 'cwd') else 'N/A'}")

            except (psutil.NoSuchProcess, psutil.AccessDenied, KeyError, TypeError, AttributeError):
                continue

        if not found_any:
            print("❌ No MCP server processes found")

    except ImportError:
        print("⚠️  psutil not available")
    except Exception as e:
        print(f"❌ Error: {e}")

def start_server_debug(name: str):
    """Start server with full debug output to see why it crashes"""
    db = MCPDatabase()
    servers = db.list_servers()

    # Nájdi server v databáze
    server_data = None
    for server in servers:
        if server['name'] == name:
            server_data = server
            break

    if not server_data:
        print(f"❌ Server '{name}' not found in database")
        return False

    script_path = server_data['script_path']

    if not os.path.exists(script_path):
        print(f"❌ Script file not found: {script_path}")
        return False

    try:
        # Načítaj transport
        transport = 'sse'
        try:
            config_path = f"servers/{name}/{name}_config.json"
            if os.path.exists(config_path):
                with open(config_path, 'r') as f:
                    config = json.load(f)
                    transport = config.get('transport', 'sse')
        except:
            pass

        # Environment variables
        env = os.environ.copy()
        env['MCP_SERVER_NAME'] = name
        env['MCP_TRANSPORT'] = transport
        env['PYTHONPATH'] = os.getcwd() + ':' + env.get('PYTHONPATH', '')

        print(f"🔍 Debug mode - starting server with full output:")
        print(f"   Script: {script_path}")
        print(f"   Environment: MCP_SERVER_NAME={name}, MCP_TRANSPORT={transport}")

        # Spusti server BEZ redirect stdout/stderr
        process = subprocess.Popen(
            [sys.executable, script_path],
            env=env,
            cwd=os.getcwd()
            # ✅ NEVYUŽÍVAME stdout=PIPE, stderr=PIPE aby sme videli output
        )

        print(f"🚀 Process started with PID: {process.pid}")
        print(f"⏳ Waiting 5 seconds to see if it crashes...")

        # Čakaj 5 sekúnd
        time.sleep(5)

        if process.poll() is None:
            print(f"✅ Server is still running after 5 seconds")
            print(f"💡 Press Ctrl+C to stop the debug session")

            try:
                # Čakaj na input od usera
                input("Press Enter to terminate the server...")
            except KeyboardInterrupt:
                pass

            process.terminate()
            process.wait()
            print(f"🛑 Server terminated")
            return True
        else:
            exit_code = process.poll()
            print(f"❌ Server crashed with exit code: {exit_code}")
            return False

    except Exception as e:
        print(f"❌ Error in debug start: {e}")
        return False

def show_server_status(name: str):
    """Show detailed status of specific server"""
    print(f"📊 Status for server '{name}':")

    # Skontroluj databázu
    db = MCPDatabase()
    servers = db.list_servers()
    server_data = None

    for server in servers:
        if server['name'] == name:
            server_data = server
            break

    if not server_data:
        print(f"❌ Server '{name}' not found in database")
        return False

    print(f"   📋 Database Info:")
    print(f"      Name: {server_data['name']}")
    print(f"      Script: {server_data['script_path']}")
    print(f"      Description: {server_data.get('description', 'N/A')}")
    print(f"      Auto Start: {'Yes' if server_data.get('auto_start', False) else 'No'}")

    # Skontroluj config
    try:
        config_path = f"servers/{name}/{name}_config.json"
        if os.path.exists(config_path):
            with open(config_path, 'r') as f:
                config = json.load(f)
                print(f"      Transport: {config.get('transport', 'sse')}")
                if 'created_at' in config:
                    import datetime
                    created = datetime.datetime.fromtimestamp(config['created_at'])
                    print(f"      Created: {created.strftime('%Y-%m-%d %H:%M:%S')}")
    except:
        print(f"      Config: Not available")

    # Skontroluj API status
    try:
        response = requests.get(f"http://localhost:{PORT}/servers/{name}", timeout=3)
        if response.status_code == 200:
            api_data = response.json()
            print(f"   🌐 API Status:")
            print(f"      Status: {api_data.get('status', 'unknown')}")
            print(f"      PID: {api_data.get('pid', 'N/A')}")
        else:
            print(f"   🌐 API Status: Not available")
    except:
        print(f"   🌐 API Status: Offline")

    # Skontroluj standalone procesy
    processes = get_server_processes(name)
    if processes:
        print(f"   🔄 Standalone Processes:")
        for proc in processes:
            import datetime
            started = datetime.datetime.fromtimestamp(proc['started'])
            print(f"      PID {proc['pid']}: {proc['transport']} transport, started {started.strftime('%H:%M:%S')}")
    else:
        print(f"   🔄 Standalone Processes: None")

    return True

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

    # Add command - ROZŠÍRENÝ
    add_parser = subparsers.add_parser('add', help='Add a new MCP server')
    add_parser.add_argument('name', help='Server name')
    add_parser.add_argument('script_path', help='Path to the MCP server script')
    add_parser.add_argument('--description', default='', help='Server description')
    add_parser.add_argument('--auto-start', action='store_true', help='Auto-start server on system startup')
    add_parser.add_argument('--transport', choices=['sse', 'streamable'], default='sse', 
                          help='Transport type (default: sse)')

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

    # Request command
    request_parser = subparsers.add_parser('request', help='Send JSON-RPC request to MCP server')
    request_parser.add_argument('name', help='Server name')
    request_parser.add_argument('method', help='JSON-RPC method (e.g., tools/call, tools/list)')
    request_parser.add_argument('--params', help='JSON parameters', default='{}')

    # Status command - NOVÝ
    status_parser = subparsers.add_parser('status', help='Show detailed status of a server')
    status_parser.add_argument('name', help='Server name')

    # Processes command - NOVÝ  
    processes_parser = subparsers.add_parser('processes', help='Show all running MCP server processes')

    # Debug command - NOVÝ
    debug_parser = subparsers.add_parser('debug-processes', help='Debug all MCP server processes')

    # Start-debug command - NOVÝ
    start_debug_parser = subparsers.add_parser('start-debug', help='Start server with debug output')
    start_debug_parser.add_argument('name', help='Server name')

    args = parser.parse_args()

    # Debug mode pre troubleshooting
    if hasattr(args, 'debug') and args.debug:
        import logging
        logging.basicConfig(level=logging.DEBUG)

    if args.command == 'add':
        add_server(args.name, args.script_path, args.description, args.auto_start, args.transport)
    elif args.command == 'list':
        list_servers()
    elif args.command == 'start':
        start_server(args.name)
    elif args.command == 'stop':
        stop_server(args.name)
    elif args.command == 'remove':
        remove_server(args.name)
    elif args.command == 'status':
        show_server_status(args.name)
    elif args.command == 'processes':
        processes = get_server_processes()
        if processes:
            print("🔄 Running MCP Server Processes:")
            print(f"{'PID':<8} {'Server':<20} {'Transport':<12} {'Started':<20}")
            print("-" * 62)
            for proc in processes:
                import datetime
                started = datetime.datetime.fromtimestamp(proc['started']).strftime('%Y-%m-%d %H:%M:%S')
                print(f"{proc['pid']:<8} {proc['server_name']:<20} {proc['transport']:<12} {started:<20}")
        else:
            print("No running MCP server processes found")
    elif args.command == 'debug-processes':  # NOVÝ debug príkaz
        debug_all_processes()
    elif args.command == 'start-debug':
        start_server_debug(args.name)
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
