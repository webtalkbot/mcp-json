#!/usr/bin/env python3
"""
mcp_database.py - Complete database class for MCP Server Manager
With automatic recovery and improved state management
"""

import sqlite3
import os
import json
from typing import List, Dict, Optional, Tuple, Any
from datetime import datetime
import logging
import threading
import time
import random
from functools import wraps

# Logging configuration
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# Retry decorator for SQLite operations
def sqlite_retry(max_retries: int = 5, base_delay: float = 0.1):
    """Decorator for automatic retry of SQLite operations on lock conflicts"""
    def decorator(func):
        @wraps(func)
        def wrapper(*args, **kwargs):
            last_exception = None
            
            for attempt in range(max_retries + 1):
                try:
                    return func(*args, **kwargs)
                    
                except sqlite3.OperationalError as e:
                    last_exception = e
                    error_msg = str(e).lower()
                    
                    # Retry for database lock errors
                    if any(keyword in error_msg for keyword in ['database is locked', 'database locked', 'locked']):
                        if attempt < max_retries:
                            # Exponential backoff with jitter
                            delay = base_delay * (2 ** attempt) + random.uniform(0, 0.1)
                            logger.warning(f"SQLite database locked (attempt {attempt + 1}/{max_retries + 1}), "
                                         f"retrying in {delay:.3f}s: {e}")
                            time.sleep(delay)
                            continue
                    
                    # For other OperationalError, don't continue
                    raise
                    
                except (sqlite3.IntegrityError, sqlite3.DatabaseError) as e:
                    # For integrity and database errors, don't continue (not lock problems)
                    raise
                    
                except Exception as e:
                    # For other exceptions, don't continue
                    raise
            
            # If we exhausted all attempts
            logger.error(f"SQLite retry failed after {max_retries + 1} attempts. Last error: {last_exception}")
            raise last_exception
        
        return wrapper
    return decorator

# Docker-friendly database path
def get_database_path():
    """Get database path - priority to local ./data directory"""
    # If environment variable is set, use it
    if 'MCP_DATA_DIR' in os.environ:
        data_dir = os.environ['MCP_DATA_DIR']
        os.makedirs(data_dir, exist_ok=True)
        return os.path.join(data_dir, 'mcp_servers.db')
    
    # Try local data directory
    local_data_dir = './data'
    if os.path.exists(local_data_dir) or True:  # Create if doesn't exist
        os.makedirs(local_data_dir, exist_ok=True)
        return os.path.join(local_data_dir, 'mcp_servers.db')
    
    # Fallback to current directory (only if data/ directory cannot be created)
    return "mcp_servers.db"

class MCPDatabase:
    """Enhanced database class with automatic recovery"""
    
    def __init__(self, db_path: Optional[str] = None):
        if db_path is None:
            self.db_path = get_database_path()
        else:
            self.db_path = db_path
            
        self._lock = threading.Lock()
        self._initialized = False
        
        # Ensure directory exists
        db_dir = os.path.dirname(self.db_path)
        if db_dir and not os.path.exists(db_dir):
            os.makedirs(db_dir, exist_ok=True)
            logger.info(f"Created database directory: {db_dir}")
        
        self.init_database()
    
    def init_database(self):
        """Database initialization and table creation"""
        try:
            with sqlite3.connect(self.db_path) as conn:
                cursor = conn.cursor()
                
                # Create table for MCP servers (without ports - communication via stdin/stdout)
                cursor.execute('''
                    CREATE TABLE IF NOT EXISTS mcp_servers (
                        id INTEGER PRIMARY KEY AUTOINCREMENT,
                        name TEXT UNIQUE NOT NULL,
                        script_path TEXT NOT NULL,
                        description TEXT DEFAULT '',
                        status TEXT DEFAULT 'stopped',
                        pid INTEGER DEFAULT NULL,
                        created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                        updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                        last_started TIMESTAMP DEFAULT NULL,
                        last_stopped TIMESTAMP DEFAULT NULL,
                        auto_start BOOLEAN DEFAULT 0,
                        startup_count INTEGER DEFAULT 0,
                        error_count INTEGER DEFAULT 0,
                        last_error TEXT DEFAULT NULL,
                        config_data TEXT DEFAULT NULL
                    )
                ''')
                
                # Create table for logs
                cursor.execute('''
                    CREATE TABLE IF NOT EXISTS server_logs (
                        id INTEGER PRIMARY KEY AUTOINCREMENT,
                        server_name TEXT NOT NULL,
                        timestamp TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                        level TEXT NOT NULL,
                        message TEXT NOT NULL,
                        FOREIGN KEY (server_name) REFERENCES mcp_servers(name) ON DELETE CASCADE
                    )
                ''')
                
                # Database migration - remove port column and add new columns
                cursor.execute("PRAGMA table_info(mcp_servers)")
                existing_columns = [row[1] for row in cursor.fetchall()]
                
                # Check if port column exists - if yes, migration is needed
                if 'port' in existing_columns:
                    logger.info("Migrating database - removing port column...")
                    
                    # Create new table without port
                    cursor.execute('''
                        CREATE TABLE IF NOT EXISTS mcp_servers_new (
                            id INTEGER PRIMARY KEY AUTOINCREMENT,
                            name TEXT UNIQUE NOT NULL,
                            script_path TEXT NOT NULL,
                            description TEXT DEFAULT '',
                            status TEXT DEFAULT 'stopped',
                            pid INTEGER DEFAULT NULL,
                            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                            updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                            last_started TIMESTAMP DEFAULT NULL,
                            last_stopped TIMESTAMP DEFAULT NULL,
                            auto_start BOOLEAN DEFAULT 0,
                            startup_count INTEGER DEFAULT 0,
                            error_count INTEGER DEFAULT 0,
                            last_error TEXT DEFAULT NULL,
                            config_data TEXT DEFAULT NULL
                        )
                    ''')
                    
                    # Copy data from old table (without port column)
                    cursor.execute('''
                        INSERT INTO mcp_servers_new 
                        (id, name, script_path, description, status, pid, created_at, updated_at, 
                         last_started, last_stopped, auto_start, startup_count, error_count, last_error, config_data)
                        SELECT id, name, script_path, description, status, pid, created_at, updated_at,
                               last_started, last_stopped, auto_start, startup_count, error_count, last_error, config_data
                        FROM mcp_servers
                    ''')
                    
                    # Remove old table and rename new one
                    cursor.execute('DROP TABLE mcp_servers')
                    cursor.execute('ALTER TABLE mcp_servers_new RENAME TO mcp_servers')
                    
                    logger.info("Database migration completed - port column removed")
                
                # Add new columns if they don't exist
                cursor.execute("PRAGMA table_info(mcp_servers)")
                existing_columns = [row[1] for row in cursor.fetchall()]
                
                new_columns = [
                    ('last_stopped', 'TIMESTAMP DEFAULT NULL'),
                    ('startup_count', 'INTEGER DEFAULT 0'),
                    ('error_count', 'INTEGER DEFAULT 0'),
                    ('last_error', 'TEXT DEFAULT NULL'),
                    ('config_data', 'TEXT DEFAULT NULL')
                ]
                
                for column_name, column_def in new_columns:
                    if column_name not in existing_columns:
                        cursor.execute(f'ALTER TABLE mcp_servers ADD COLUMN {column_name} {column_def}')
                        logger.info(f"Added new column: {column_name}")
                
                # Indexes for faster searches
                cursor.execute('CREATE INDEX IF NOT EXISTS idx_name ON mcp_servers(name)')
                cursor.execute('CREATE INDEX IF NOT EXISTS idx_status ON mcp_servers(status)')
                cursor.execute('CREATE INDEX IF NOT EXISTS idx_auto_start ON mcp_servers(auto_start)')
                cursor.execute('CREATE INDEX IF NOT EXISTS idx_server_logs_name ON server_logs(server_name)')
                cursor.execute('CREATE INDEX IF NOT EXISTS idx_server_logs_timestamp ON server_logs(timestamp)')
                
                conn.commit()
                
                if not self._initialized:
                    logger.info(f"Database initialized: {self.db_path}")
                    self._initialized = True
                
        except sqlite3.Error as e:
            logger.error(f"Error initializing database: {e}")
            raise
    
    @sqlite_retry(max_retries=3)
    def add_server(self, name: str, script_path: str, 
                   description: str = "", auto_start: bool = False,
                   config_data: Optional[Dict[str, Any]] = None) -> bool:
        """Add new MCP server (without port - communication via stdin/stdout)"""
        with self._lock:
            try:
                with sqlite3.connect(self.db_path) as conn:
                    cursor = conn.cursor()
                    
                    # Check if file exists
                    if not os.path.exists(script_path):
                        logger.error(f"Script file does not exist: {script_path}")
                        return False
                    
                    # Absolute path
                    script_path = os.path.abspath(script_path)
                    
                    # Convert config_data to JSON
                    config_json = None
                    if config_data:
                        config_json = json.dumps(config_data)
                    
                    cursor.execute('''
                        INSERT INTO mcp_servers 
                        (name, script_path, description, auto_start, config_data, updated_at)
                        VALUES (?, ?, ?, ?, ?, ?)
                    ''', (name, script_path, description, auto_start, config_json, datetime.now()))
                    
                    conn.commit()
                    
                    # Log
                    self.add_log(name, "INFO", f"Server added")
                    logger.info(f"MCP server added: {name}")
                    return True
                    
            except sqlite3.IntegrityError as e:
                if "name" in str(e):
                    logger.error(f"Server with name '{name}' already exists")
                else:
                    logger.error(f"Integrity error: {e}")
                return False
            except sqlite3.Error as e:
                logger.error(f"Error adding server: {e}")
                return False
    
    @sqlite_retry(max_retries=3)
    def remove_server(self, name: str) -> bool:
        """Remove MCP server"""
        with self._lock:
            try:
                with sqlite3.connect(self.db_path) as conn:
                    cursor = conn.cursor()
                    
                    # First check if server exists
                    cursor.execute('SELECT * FROM mcp_servers WHERE name = ?', (name,))
                    server = cursor.fetchone()
                    
                    if not server:
                        logger.warning(f"Server with name '{name}' not found")
                        return False
                    
                    # Remove server
                    cursor.execute('DELETE FROM mcp_servers WHERE name = ?', (name,))
                    
                    # Remove logs
                    cursor.execute('DELETE FROM server_logs WHERE server_name = ?', (name,))
                    
                    conn.commit()
                    logger.info(f"MCP server removed: {name}")
                    return True
                    
            except sqlite3.Error as e:
                logger.error(f"Error removing server: {e}")
                return False
    
    def get_server(self, name: str) -> Optional[Dict]:
        """Get server information"""
        try:
            with sqlite3.connect(self.db_path) as conn:
                conn.row_factory = sqlite3.Row
                cursor = conn.cursor()
                
                cursor.execute('SELECT * FROM mcp_servers WHERE name = ?', (name,))
                row = cursor.fetchone()
                
                if row:
                    server_dict = dict(row)
                    # Convert config_data from JSON
                    if server_dict.get('config_data'):
                        try:
                            server_dict['config_data'] = json.loads(server_dict['config_data'])
                        except json.JSONDecodeError:
                            server_dict['config_data'] = None
                    
                    return server_dict
                return None
                
        except sqlite3.Error as e:
            logger.error(f"Error getting server: {e}")
            return None
    
    def list_servers(self, status_filter: Optional[str] = None, 
                    auto_start_only: bool = False) -> List[Dict]:
        """List all MCP servers"""
        try:
            with sqlite3.connect(self.db_path) as conn:
                conn.row_factory = sqlite3.Row
                cursor = conn.cursor()
                
                query = 'SELECT * FROM mcp_servers'
                params = []
                conditions = []
                
                if status_filter:
                    conditions.append('status = ?')
                    params.append(status_filter)
                
                if auto_start_only:
                    conditions.append('auto_start = 1')
                
                if conditions:
                    query += ' WHERE ' + ' AND '.join(conditions)
                
                query += ' ORDER BY name'
                
                cursor.execute(query, params)
                rows = cursor.fetchall()
                
                servers = []
                for row in rows:
                    server_dict = dict(row)
                    # Convert config_data from JSON
                    if server_dict.get('config_data'):
                        try:
                            server_dict['config_data'] = json.loads(server_dict['config_data'])
                        except json.JSONDecodeError:
                            server_dict['config_data'] = None
                    servers.append(server_dict)
                
                return servers
                
        except sqlite3.Error as e:
            logger.error(f"Error getting server list: {e}")
            return []
    
    @sqlite_retry(max_retries=3)
    def update_server_status(self, name: str, status: str, pid: Optional[int] = None,
                           error_message: Optional[str] = None) -> bool:
        """Update server status"""
        with self._lock:
            try:
                with sqlite3.connect(self.db_path) as conn:
                    cursor = conn.cursor()
                    
                    # Basic fields to update
                    update_fields = ['status = ?', 'updated_at = ?']
                    values = [status, datetime.now()]
                    
                    # PID
                    if pid is not None:
                        update_fields.append('pid = ?')
                        values.append(pid)
                    
                    # Special processing by status
                    if status == 'running':
                        update_fields.extend(['last_started = ?', 'startup_count = startup_count + 1'])
                        values.append(datetime.now())
                        
                        if error_message is None:  # Only if no error
                            update_fields.append('error_count = 0')
                            update_fields.append('last_error = NULL')
                    
                    elif status == 'stopped':
                        update_fields.extend(['pid = NULL', 'last_stopped = ?'])
                        values.append(datetime.now())
                    
                    elif status == 'error':
                        update_fields.append('error_count = error_count + 1')
                        if error_message:
                            update_fields.append('last_error = ?')
                            values.append(error_message)
                    
                    # Update
                    query = f'UPDATE mcp_servers SET {", ".join(update_fields)} WHERE name = ?'
                    values.append(name)
                    
                    cursor.execute(query, values)
                    
                    if cursor.rowcount > 0:
                        conn.commit()
                        
                        # Add log
                        log_message = f"Status changed to: {status}"
                        if pid:
                            log_message += f" (PID: {pid})"
                        if error_message:
                            log_message += f" - Error: {error_message}"
                        
                        self.add_log(name, "INFO" if status != "error" else "ERROR", log_message)
                        
                        logger.info(f"Server '{name}' status updated to: {status}")
                        return True
                    else:
                        logger.warning(f"Server with name '{name}' not found")
                        return False
                        
            except sqlite3.Error as e:
                logger.error(f"Error updating server status: {e}")
                return False
    
    @sqlite_retry(max_retries=3)
    def update_server_config(self, name: str, config_data: Dict[str, Any]) -> bool:
        """Update server configuration"""
        with self._lock:
            try:
                with sqlite3.connect(self.db_path) as conn:
                    cursor = conn.cursor()
                    
                    config_json = json.dumps(config_data)
                    
                    cursor.execute('''
                        UPDATE mcp_servers 
                        SET config_data = ?, updated_at = ?
                        WHERE name = ?
                    ''', (config_json, datetime.now(), name))
                    
                    if cursor.rowcount > 0:
                        conn.commit()
                        self.add_log(name, "INFO", "Configuration updated")
                        return True
                    return False
                    
            except sqlite3.Error as e:
                logger.error(f"Error updating configuration: {e}")
                return False
    
    def name_exists(self, name: str) -> bool:
        """Check if name already exists"""
        try:
            with sqlite3.connect(self.db_path) as conn:
                cursor = conn.cursor()
                
                cursor.execute('SELECT COUNT(*) FROM mcp_servers WHERE name = ?', (name,))
                count = cursor.fetchone()[0]
                
                return count > 0
                
        except sqlite3.Error as e:
            logger.error(f"Error checking name: {e}")
            return True  # Return True for safety
    
    def cleanup_dead_processes(self) -> int:
        """Clean up dead processes"""
        try:
            import psutil
        except ImportError:
            logger.warning("psutil not installed, cannot check processes")
            return 0
        
        cleaned = 0
        
        try:
            servers = self.list_servers(status_filter='running')
            
            for server in servers:
                pid = server.get('pid')
                if pid and not psutil.pid_exists(pid):
                    self.update_server_status(server['name'], 'stopped')
                    self.add_log(server['name'], "WARNING", f"Dead process cleaned (PID: {pid})")
                    cleaned += 1
                    logger.info(f"Cleaned dead process: {server['name']} (PID: {pid})")
            
            return cleaned
            
        except Exception as e:
            logger.error(f"Error cleaning dead processes: {e}")
            return 0
    
    def add_log(self, server_name: str, level: str, message: str):
        """Add log entry"""
        try:
            with sqlite3.connect(self.db_path) as conn:
                cursor = conn.cursor()
                
                cursor.execute('''
                    INSERT INTO server_logs (server_name, level, message)
                    VALUES (?, ?, ?)
                ''', (server_name, level, message))
                
                conn.commit()
                
        except sqlite3.Error as e:
            logger.error(f"Error adding log: {e}")
    
    def get_logs(self, server_name: Optional[str] = None, 
                 level_filter: Optional[str] = None, 
                 limit: int = 100) -> List[Dict]:
        """Get logs"""
        try:
            with sqlite3.connect(self.db_path) as conn:
                conn.row_factory = sqlite3.Row
                cursor = conn.cursor()
                
                query = 'SELECT * FROM server_logs'
                params = []
                conditions = []
                
                if server_name:
                    conditions.append('server_name = ?')
                    params.append(server_name)
                
                if level_filter:
                    conditions.append('level = ?')
                    params.append(level_filter)
                
                if conditions:
                    query += ' WHERE ' + ' AND '.join(conditions)
                
                query += ' ORDER BY timestamp DESC LIMIT ?'
                params.append(limit)
                
                cursor.execute(query, params)
                rows = cursor.fetchall()
                
                return [dict(row) for row in rows]
                
        except sqlite3.Error as e:
            logger.error(f"Error getting logs: {e}")
            return []
    
    def clear_old_logs(self, days_to_keep: int = 30):
        """Clear old logs"""
        try:
            with sqlite3.connect(self.db_path) as conn:
                cursor = conn.cursor()
                
                from datetime import timedelta
                cutoff_date = datetime.now() - timedelta(days=days_to_keep)
                
                cursor.execute('DELETE FROM server_logs WHERE timestamp < ?', (cutoff_date,))
                deleted_count = cursor.rowcount
                
                conn.commit()
                
                if deleted_count > 0:
                    logger.info(f"Deleted {deleted_count} old log entries")
                
                return deleted_count
                
        except sqlite3.Error as e:
            logger.error(f"Error clearing old logs: {e}")
            return 0
    
    def get_server_statistics(self, name: str) -> Optional[Dict]:
        """Get server statistics"""
        server = self.get_server(name)
        if not server:
            return None
        
        try:
            with sqlite3.connect(self.db_path) as conn:
                conn.row_factory = sqlite3.Row
                cursor = conn.cursor()
                
                # Log count by level
                cursor.execute('''
                    SELECT level, COUNT(*) as count 
                    FROM server_logs 
                    WHERE server_name = ? 
                    GROUP BY level
                ''', (name,))
                
                log_counts = {row[0]: row[1] for row in cursor.fetchall()}
                
                # Last log
                cursor.execute('''
                    SELECT * FROM server_logs 
                    WHERE server_name = ? 
                    ORDER BY timestamp DESC 
                    LIMIT 1
                ''', (name,))
                
                last_log_row = cursor.fetchone()
                last_log = dict(last_log_row) if last_log_row else None
                
                stats = {
                    'name': name,
                    'startup_count': server.get('startup_count', 0),
                    'error_count': server.get('error_count', 0),
                    'last_error': server.get('last_error'),
                    'last_started': server.get('last_started'),
                    'last_stopped': server.get('last_stopped'),
                    'log_counts': log_counts,
                    'last_log': last_log
                }
                
                return stats
                
        except sqlite3.Error as e:
            logger.error(f"Error getting statistics: {e}")
            return None
    
    def backup_database(self, backup_path: str) -> bool:
        """Backup database"""
        try:
            import shutil
            
            shutil.copy2(self.db_path, backup_path)
            logger.info(f"Database backed up to: {backup_path}")
            return True
            
        except Exception as e:
            logger.error(f"Error backing up database: {e}")
            return False
    
    def get_database_info(self) -> Dict:
        """Database information"""
        try:
            with sqlite3.connect(self.db_path) as conn:
                cursor = conn.cursor()
                
                # Server count
                cursor.execute('SELECT COUNT(*) FROM mcp_servers')
                server_count = cursor.fetchone()[0]
                
                # Log count
                cursor.execute('SELECT COUNT(*) FROM server_logs')
                log_count = cursor.fetchone()[0]
                
                # File size
                file_size = os.path.getsize(self.db_path) if os.path.exists(self.db_path) else 0
                
                # Last modification
                last_modified = datetime.fromtimestamp(os.path.getmtime(self.db_path)) if os.path.exists(self.db_path) else None
                
                return {
                    'database_path': self.db_path,
                    'server_count': server_count,
                    'log_count': log_count,
                    'file_size': file_size,
                    'last_modified': last_modified.isoformat() if last_modified else None
                }
                
        except Exception as e:
            logger.error(f"Error getting database info: {e}")
            return {}


# CLI for database testing
if __name__ == "__main__":
    import sys
    
    db = MCPDatabase()
    
    if len(sys.argv) < 2:
        print("Usage: python mcp_database.py <action> [arguments]")
        print("Actions:")
        print("  add <name> <script_path> [description]")
        print("  remove <name>")
        print("  list [status_filter]")
        print("  status <name>")
        print("  logs [server_name] [limit]")
        print("  stats <name>")
        print("  cleanup")
        print("  info")
        sys.exit(1)
    
    action = sys.argv[1]
    
    if action == "add":
        if len(sys.argv) < 4:
            print("Usage: add <name> <script_path> [description]")
            sys.exit(1)
        
        name = sys.argv[2]
        script_path = sys.argv[3]
        description = sys.argv[4] if len(sys.argv) > 4 else ""
        
        success = db.add_server(name, script_path, description)
        print("✅ Server added" if success else "❌ Error adding server")
    
    elif action == "remove":
        if len(sys.argv) < 3:
            print("Usage: remove <name>")
            sys.exit(1)
        
        name = sys.argv[2]
        success = db.remove_server(name)
        print("✅ Server removed" if success else "❌ Server not found")
    
    elif action == "list":
        status_filter = sys.argv[2] if len(sys.argv) > 2 else None
        servers = db.list_servers(status_filter=status_filter)
        
        if servers:
            print(f"{'Name':<20} {'Status':<10} {'PID':<8} {'Starts':<8} {'Errors':<6}")
            print("-" * 60)
            for server in servers:
                pid_display = str(server.get('pid', 'N/A'))
                startup_count = server.get('startup_count', 0)
                error_count = server.get('error_count', 0)
                
                print(f"{server['name']:<20} {server['status']:<10} "
                      f"{pid_display:<8} {startup_count:<8} {error_count:<6}")
        else:
            print("No servers found")
    
    elif action == "logs":
        server_name = sys.argv[2] if len(sys.argv) > 2 else None
        limit = int(sys.argv[3]) if len(sys.argv) > 3 else 20
        
        logs = db.get_logs(server_name=server_name, limit=limit)
        
        if logs:
            print(f"{'Time':<20} {'Server':<15} {'Level':<8} {'Message'}")
            print("-" * 80)
            for log in logs:
                timestamp = log['timestamp'][:19] if log['timestamp'] else 'N/A'
                print(f"{timestamp:<20} {log['server_name']:<15} {log['level']:<8} {log['message']}")
        else:
            print("No logs found")
    
    elif action == "stats":
        if len(sys.argv) < 3:
            print("Usage: stats <name>")
            sys.exit(1)
        
        name = sys.argv[2]
        stats = db.get_server_statistics(name)
        
        if stats:
            print(f"📊 Server statistics: {name}")
            print(f"Starts: {stats['startup_count']}")
            print(f"Errors: {stats['error_count']}")
            print(f"Last started: {stats['last_started'] or 'Never'}")
            print(f"Last stopped: {stats['last_stopped'] or 'Never'}")
            if stats['last_error']:
                print(f"Last error: {stats['last_error']}")
            
            print("\nLogs by level:")
            for level, count in stats['log_counts'].items():
                print(f"  {level}: {count}")
        else:
            print(f"❌ Server '{name}' not found")
    
    elif action == "cleanup":
        cleaned = db.cleanup_dead_processes()
        print(f"✅ Cleaned {cleaned} dead processes")
    
    elif action == "info":
        info = db.get_database_info()
        print("📊 Database information:")
        print(f"Path: {info.get('database_path')}")
        print(f"Servers: {info.get('server_count')}")
        print(f"Logs: {info.get('log_count')}")
        print(f"Size: {info.get('file_size', 0)} bytes")
        print(f"Last modified: {info.get('last_modified', 'N/A')}")
    
    else:
        print(f"Unknown action: {action}")
        sys.exit(1)