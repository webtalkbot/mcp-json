#!/usr/bin/env python3
"""
error_logger.py - Centralized error logging system for MCP server crashes and restarts
"""

import os
import logging
import json
import traceback
import time
from datetime import datetime
from typing import Dict, Any, Optional
from pathlib import Path
import threading
import psutil

class MCPErrorLogger:
    """Centralized error logger for MCP server monitoring"""
    
    def __init__(self, log_dir: str = "logs"):
        self.log_dir = Path(log_dir)
        self.log_dir.mkdir(exist_ok=True)
        
        # Error log file
        self.error_log_file = self.log_dir / "error.log"
        
        # Setup error logger
        self.error_logger = logging.getLogger("mcp_error")
        self.error_logger.setLevel(logging.INFO)
        
        # Remove existing handlers to avoid duplication
        for handler in self.error_logger.handlers[:]:
            self.error_logger.removeHandler(handler)
        
        # File handler for error.log
        error_handler = logging.FileHandler(self.error_log_file, encoding='utf-8')
        error_formatter = logging.Formatter(
            '%(asctime)s - %(levelname)s - %(message)s',
            datefmt='%Y-%m-%d %H:%M:%S'
        )
        error_handler.setFormatter(error_formatter)
        self.error_logger.addHandler(error_handler)
        
        # Console handler for critical errors
        console_handler = logging.StreamHandler()
        console_formatter = logging.Formatter(
            '🚨 %(asctime)s - %(levelname)s - %(message)s',
            datefmt='%H:%M:%S'
        )
        console_handler.setFormatter(console_formatter)
        console_handler.setLevel(logging.WARNING)
        self.error_logger.addHandler(console_handler)
        
        # Thread lock for thread-safe logging
        self.lock = threading.Lock()
        
        # System info cache
        self._system_info = self._get_system_info()
        
        # Log startup
        self.log_system_startup()
    
    def _get_system_info(self) -> Dict[str, Any]:
        """Get system information for context"""
        try:
            return {
                "cpu_count": psutil.cpu_count(),
                "memory_total": psutil.virtual_memory().total,
                "memory_available": psutil.virtual_memory().available,
                "disk_usage": psutil.disk_usage('/').percent,
                "python_version": os.sys.version.split()[0],
                "platform": os.name
            }
        except Exception:
            return {"error": "Unable to get system info"}
    
    def _format_log_entry(self, level: str, category: str, server_name: str, 
                         message: str, details: Optional[Dict] = None) -> str:
        """Format log entry with structured data"""
        entry = {
            "timestamp": datetime.now().isoformat(),
            "level": level,
            "category": category,
            "server_name": server_name,
            "message": message
        }
        
        if details:
            entry.update(details)
        
        return json.dumps(entry, ensure_ascii=False)
    
    def log_system_startup(self):
        """Log system startup"""
        with self.lock:
            self.error_logger.info(self._format_log_entry(
                level="INFO",
                category="SYSTEM",
                server_name="system",
                message="MCP Error Logger initialized",
                details={
                    "system_info": self._system_info,
                    "log_file": str(self.error_log_file)
                }
            ))
    
    def log_server_crash(self, server_name: str, error: Exception, 
                        context: Optional[Dict] = None):
        """Log server crash with full details"""
        with self.lock:
            crash_details = {
                "error_type": type(error).__name__,
                "error_message": str(error),
                "traceback": traceback.format_exc(),
                "context": context or {},
                "system_state": {
                    "memory_usage": psutil.virtual_memory().percent,
                    "cpu_usage": psutil.cpu_percent(interval=1),
                    "load_average": os.getloadavg() if hasattr(os, 'getloadavg') else None
                }
            }
            
            self.error_logger.error(self._format_log_entry(
                level="ERROR",
                category="SERVER_CRASH",
                server_name=server_name,
                message=f"Server {server_name} crashed: {str(error)}",
                details=crash_details
            ))
    
    def log_server_restart(self, server_name: str, attempt: int, 
                          success: bool, details: Optional[Dict] = None):
        """Log server restart attempt"""
        with self.lock:
            restart_details = {
                "attempt_number": attempt,
                "restart_success": success,
                "details": details or {}
            }
            
            level = "INFO" if success else "WARNING"
            message = f"Server {server_name} restart attempt {attempt}: {'SUCCESS' if success else 'FAILED'}"
            
            self.error_logger.log(
                logging.INFO if success else logging.WARNING,
                self._format_log_entry(
                    level=level,
                    category="SERVER_RESTART",
                    server_name=server_name,
                    message=message,
                    details=restart_details
                )
            )
    
    def log_streamable_error(self, server_name: str, session_id: str, 
                           error: Exception, context: Optional[Dict] = None):
        """Log streamable/SSE transport errors"""
        with self.lock:
            streamable_details = {
                "session_id": session_id,
                "error_type": type(error).__name__,
                "error_message": str(error),
                "context": context or {},
                "transport_type": context.get("transport", "unknown") if context else "unknown"
            }
            
            self.error_logger.warning(self._format_log_entry(
                level="WARNING",
                category="STREAMABLE_ERROR",
                server_name=server_name,
                message=f"Streamable error for {server_name} session {session_id}: {str(error)}",
                details=streamable_details
            ))
    
    def log_tools_list_error(self, server_name: str, error: Exception, 
                           context: Optional[Dict] = None):
        """Log tools/list call errors"""
        with self.lock:
            tools_details = {
                "error_type": type(error).__name__,
                "error_message": str(error),
                "context": context or {},
                "operation": "tools/list"
            }
            
            self.error_logger.warning(self._format_log_entry(
                level="WARNING",
                category="TOOLS_ERROR",
                server_name=server_name,
                message=f"Tools/list error for {server_name}: {str(error)}",
                details=tools_details
            ))
    
    def log_tools_call_error(self, server_name: str, tool_name: str, 
                           error: Exception, context: Optional[Dict] = None):
        """Log tools/call errors"""
        with self.lock:
            call_details = {
                "tool_name": tool_name,
                "error_type": type(error).__name__,
                "error_message": str(error),
                "context": context or {},
                "operation": "tools/call"
            }
            
            self.error_logger.warning(self._format_log_entry(
                level="WARNING",
                category="TOOLS_CALL_ERROR",
                server_name=server_name,
                message=f"Tools/call error for {server_name}.{tool_name}: {str(error)}",
                details=call_details
            ))
    
    def log_initialization_error(self, server_name: str, error: Exception, 
                                context: Optional[Dict] = None):
        """Log MCP initialization errors"""
        with self.lock:
            init_details = {
                "error_type": type(error).__name__,
                "error_message": str(error),
                "context": context or {},
                "phase": context.get("phase", "unknown") if context else "unknown"
            }
            
            self.error_logger.error(self._format_log_entry(
                level="ERROR",
                category="INIT_ERROR",
                server_name=server_name,
                message=f"Initialization error for {server_name}: {str(error)}",
                details=init_details
            ))
    
    def log_process_monitoring_event(self, event_type: str, details: Dict):
        """Log process monitoring events"""
        with self.lock:
            self.error_logger.info(self._format_log_entry(
                level="INFO",
                category="MONITORING",
                server_name="monitor",
                message=f"Process monitoring: {event_type}",
                details=details
            ))
    
    def log_custom_error(self, level: str, category: str, server_name: str, 
                        message: str, error: Optional[Exception] = None, 
                        context: Optional[Dict] = None):
        """Log custom error with flexible parameters"""
        with self.lock:
            error_details = {
                "context": context or {}
            }
            
            if error:
                error_details.update({
                    "error_type": type(error).__name__,
                    "error_message": str(error),
                    "traceback": traceback.format_exc()
                })
            
            log_level = getattr(logging, level.upper(), logging.INFO)
            
            self.error_logger.log(
                log_level,
                self._format_log_entry(
                    level=level.upper(),
                    category=category,
                    server_name=server_name,
                    message=message,
                    details=error_details
                )
            )
    
    def get_recent_errors(self, hours: int = 24, server_name: Optional[str] = None) -> list:
        """Get recent errors from log file"""
        try:
            cutoff_time = time.time() - (hours * 3600)
            recent_errors = []
            
            if not self.error_log_file.exists():
                return recent_errors
            
            with open(self.error_log_file, 'r', encoding='utf-8') as f:
                for line in f:
                    try:
                        # Try to parse as JSON (structured log)
                        if line.strip().startswith('{'):
                            entry = json.loads(line.strip())
                            timestamp = datetime.fromisoformat(entry.get('timestamp', '')).timestamp()
                            
                            if timestamp >= cutoff_time:
                                if server_name is None or entry.get('server_name') == server_name:
                                    recent_errors.append(entry)
                        else:
                            # Parse regular log format
                            if 'ERROR' in line or 'WARNING' in line:
                                recent_errors.append({"raw_log": line.strip()})
                                
                    except (json.JSONDecodeError, ValueError):
                        continue
            
            return recent_errors[-100:]  # Return last 100 entries
            
        except Exception as e:
            self.log_custom_error(
                "ERROR", "LOGGER", "error_logger", 
                f"Error reading recent errors: {str(e)}", e
            )
            return []
    
    def get_error_stats(self) -> Dict[str, Any]:
        """Get error statistics"""
        try:
            stats = {
                "log_file_size": self.error_log_file.stat().st_size if self.error_log_file.exists() else 0,
                "recent_errors_24h": len(self.get_recent_errors(24)),
                "recent_errors_1h": len(self.get_recent_errors(1)),
                "system_info": self._system_info
            }
            
            # Count errors by category
            recent_errors = self.get_recent_errors(24)
            category_counts = {}
            server_counts = {}
            
            for error in recent_errors:
                category = error.get('category', 'UNKNOWN')
                server = error.get('server_name', 'unknown')
                
                category_counts[category] = category_counts.get(category, 0) + 1
                server_counts[server] = server_counts.get(server, 0) + 1
            
            stats["errors_by_category"] = category_counts
            stats["errors_by_server"] = server_counts
            
            return stats
            
        except Exception as e:
            return {"error": f"Unable to get stats: {str(e)}"}

# Global error logger instance
_error_logger = None

def get_error_logger() -> MCPErrorLogger:
    """Get global error logger instance"""
    global _error_logger
    if _error_logger is None:
        _error_logger = MCPErrorLogger()
    return _error_logger

def log_server_crash(server_name: str, error: Exception, context: Optional[Dict] = None):
    """Convenience function for logging server crashes"""
    get_error_logger().log_server_crash(server_name, error, context)

def log_server_restart(server_name: str, attempt: int, success: bool, details: Optional[Dict] = None):
    """Convenience function for logging server restarts"""
    get_error_logger().log_server_restart(server_name, attempt, success, details)

def log_streamable_error(server_name: str, session_id: str, error: Exception, context: Optional[Dict] = None):
    """Convenience function for logging streamable errors"""
    get_error_logger().log_streamable_error(server_name, session_id, error, context)

def log_tools_list_error(server_name: str, error: Exception, context: Optional[Dict] = None):
    """Convenience function for logging tools/list errors"""
    get_error_logger().log_tools_list_error(server_name, error, context)

def log_tools_call_error(server_name: str, tool_name: str, error: Exception, context: Optional[Dict] = None):
    """Convenience function for logging tools/call errors"""
    get_error_logger().log_tools_call_error(server_name, tool_name, error, context)

def log_initialization_error(server_name: str, error: Exception, context: Optional[Dict] = None):
    """Convenience function for logging initialization errors"""
    get_error_logger().log_initialization_error(server_name, error, context)

def log_custom_error(level: str, category: str, server_name: str, message: str, 
                    error: Optional[Exception] = None, context: Optional[Dict] = None):
    """Convenience function for logging custom errors"""
    get_error_logger().log_custom_error(level, category, server_name, message, error, context)
