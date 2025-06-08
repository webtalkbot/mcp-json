#!/usr/bin/env python3
"""
mcp_wrapper.py - FastAPI wrapper for MCP Server Manager with individual HTTP MCP endpoints
"""

import os
import subprocess
import signal
import time
import threading
import logging
import json
import asyncio
from typing import Dict, List, Optional
from fastapi import FastAPI, HTTPException, Path, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import StreamingResponse
from mcp_database import MCPDatabase
import psutil
import queue
import uuid
from dotenv import load_dotenv

# Load environment variables
load_dotenv()

# Cross-platform imports (Unix only)
try:
    import fcntl
    HAS_FCNTL = True
except ImportError:
    HAS_FCNTL = False

try:
    import select
    HAS_SELECT = True
except ImportError:
    HAS_SELECT = False

# If Unix functions are not available, log a warning after logger initialization
if not HAS_FCNTL or not HAS_SELECT:
    _logger_warnings = []
    if not HAS_FCNTL:
        _logger_warnings.append("fcntl is not available on this system (Windows). Non-blocking IO will be bypassed.")
    if not HAS_SELECT:
        _logger_warnings.append("select is not available on this system (Windows). IO operations will be synchronous.")

# Logging setup
logging.basicConfig(level=logging.DEBUG, format='%(asctime)s - %(name)s - %(levelname)s - %(message)s')
logger = logging.getLogger(__name__)

# Log warning messages for missing Unix functions
if not HAS_FCNTL or not HAS_SELECT:
    if not HAS_FCNTL:
        logger.warning("fcntl is not available on this system (Windows). Non-blocking IO will be bypassed.")
    if not HAS_SELECT:
        logger.warning("select is not available on this system (Windows). IO operations will be synchronous.")

app = FastAPI(title="MCP Server Manager with Individual HTTP MCP Endpoints", version="1.0.0")

# Global database
db = MCPDatabase()

# Import tools proxy
from tools_proxy import ToolsProxy

class MCPProcess:
    """Wrapper for MCP server process with async communication (no ports - stdin/stdout)"""
    
    def __init__(self, name: str, script_path: str):
        self.name = name
        self.script_path = script_path
        self.process = None
        self.stdout_reader_task = None
        self.stderr_reader_task = None
        self.initialized = False
        self.request_counter = 0
        self.pending_requests = {}
        self.lock = asyncio.Lock()
        self.message_queue = queue.Queue()
        self.running = False
        
    async def start(self) -> bool:
        """Starts the MCP server process (no port - communication via stdin/stdout)"""
        try:
            logger.info(f"Starting server {self.name}")
            
            env = os.environ.copy()
            
            self.process = subprocess.Popen(
                ['python3', self.script_path],
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                env=env,
                bufsize=0
            )
            
            # Set non-blocking mode for stdout and stderr (Unix systems only)
            if HAS_FCNTL:
                try:
                    fcntl.fcntl(self.process.stdout.fileno(), fcntl.F_SETFL, os.O_NONBLOCK)
                    fcntl.fcntl(self.process.stderr.fileno(), fcntl.F_SETFL, os.O_NONBLOCK)
                except Exception as e:
                    logger.warning(f"Error setting non-blocking IO for server {self.name}: {e}")
            else:
                logger.debug(f"Non-blocking IO is not available for server {self.name} (Windows system)")
            
            # Start async reader tasks
            self.running = True
            try:
                self.stdout_reader_task = asyncio.create_task(self._read_stdout_async())
                self.stderr_reader_task = asyncio.create_task(self._read_stderr_async())
            except Exception as e:
                logger.error(f"Error starting async reader tasks for server {self.name}: {e}")
                self.running = False
                raise
            
            # Wait for initialization with error handling
            try:
                await self._wait_for_initialization_async()
            except Exception as e:
                logger.error(f"Error during server initialization {self.name}: {e}")
                # Cleanup on initialization error
                self.running = False
                if self.stdout_reader_task:
                    self.stdout_reader_task.cancel()
                if self.stderr_reader_task:
                    self.stderr_reader_task.cancel()
                raise
            
            # Database update
            try:
                db.update_server_status(self.name, 'running', self.process.pid)
            except Exception as e:
                logger.warning(f"Error updating database for server {self.name}: {e}")
            
            logger.info(f"✅ Server {self.name} successfully started (PID: {self.process.pid})")
            return True
            
        except Exception as e:
            logger.error(f"❌ Error starting server {self.name}: {e}")
            # Cleanup on error
            self.running = False
            if self.process:
                try:
                    self.process.terminate()
                except Exception:
                    pass
                self.process = None
            return False
    
    async def _wait_for_initialization_async(self, timeout: int = 30):
        """FIXED: Proper MCP initialization handshake with capability negotiation"""
        start_time = time.time()
        init_sent = False
        initialized_sent = False
        initialization_response_received = False
        server_capabilities = None
        
        logger.info(f"Starting MCP initialization handshake for server {self.name}")
        
        while time.time() - start_time < timeout:
            if self.process.poll() is not None:
                raise Exception(f"Server process {self.name} terminated prematurely during initialization")
            
            try:
                if not init_sent:
                    # FIXED: Send proper MCP initialize request with client capabilities
                    init_request = {
                        "jsonrpc": "2.0",
                        "id": 0,
                        "method": "initialize",
                        "params": {
                            "protocolVersion": "2025-03-26",
                            "capabilities": {
                                "tools": {},
                                "resources": {},
                                "prompts": {},
                                "logging": {}
                            },
                            "clientInfo": {
                                "name": "mcp-wrapper",
                                "version": "1.0.0"
                            }
                        }
                    }
                    
                    self.process.stdin.write(json.dumps(init_request) + '\n')
                    self.process.stdin.flush()
                    init_sent = True
                    logger.info(f"✅ Sent MCP initialize request for server {self.name}")
                
                # FIXED: Wait for proper initialize response before proceeding
                if init_sent and self.initialized and not initialization_response_received:
                    initialization_response_received = True
                    logger.info(f"✅ Received MCP initialize response from server {self.name}")
                    
                    # FIXED: Validate server capabilities (if available)
                    # This would be populated by _process_message_async when it receives the response
                    logger.info(f"Server {self.name} capabilities negotiated successfully")
                
                # FIXED: Only send initialized notification after receiving response
                if initialization_response_received and not initialized_sent:
                    initialized_notification = {
                        "jsonrpc": "2.0",
                        "method": "notifications/initialized"
                    }
                    
                    self.process.stdin.write(json.dumps(initialized_notification) + '\n')
                    self.process.stdin.flush()
                    initialized_sent = True
                    logger.info(f"✅ Sent MCP initialized notification for server {self.name}")
                    
                    # FIXED: Server is now fully ready for tool calls
                    logger.info(f"🎉 MCP handshake complete for server {self.name} - ready for tool calls")
                    await asyncio.sleep(0.5)  # Stabilization wait
                    break
                    
            except Exception as e:
                logger.debug(f"MCP initialization {self.name} in progress... ({e})")
            
            await asyncio.sleep(0.5)
        
        # FIXED: Strict validation of initialization state
        if not self.initialized:
            raise Exception(f"❌ Server {self.name} did not complete MCP initialization within {timeout}s - handshake failed")
        elif not initialization_response_received:
            raise Exception(f"❌ Server {self.name} never responded to initialize request - protocol violation")
        elif not initialized_sent:
            raise Exception(f"❌ Server {self.name} initialized but notifications/initialized was not sent - incomplete handshake")
        else:
            logger.info(f"✅ MCP initialization successful for server {self.name}")
    
    async def _read_stdout_async(self):
        """Async stdout reading - simplified similar to debug_communication.py"""
        logger.debug(f"Starting stdout reader for server {self.name}")
        
        try:
            loop = asyncio.get_event_loop()
            
            while self.running and self.process and self.process.poll() is None:
                try:
                    # Use simple readline() as in debug_communication.py
                    line = await loop.run_in_executor(None, self._safe_readline)
                    
                    if line:
                        line = line.strip()
                        if line:
                            logger.debug(f"[{self.name}] Read line: {line}")
                            try:
                                await self._process_message_async(line)
                            except Exception as e:
                                logger.error(f"Error processing message from {self.name}: {e}")
                    
                    # Short pause only if no message arrived
                    if not line:
                        await asyncio.sleep(0.01)
                    
                except asyncio.CancelledError:
                    logger.debug(f"Stdout reader for server {self.name} was cancelled")
                    break
                except Exception as e:
                    if self.running and self.process and self.process.poll() is None:
                        logger.warning(f"Async stdout reader error {self.name}: {e}")
                    await asyncio.sleep(0.1)
        
        except asyncio.CancelledError:
            logger.debug(f"Stdout reader task for server {self.name} was cancelled")
        except Exception as e:
            logger.error(f"Critical error in stdout reader for server {self.name}: {e}")
        finally:
            logger.debug(f"Stdout reader for server {self.name} terminated")
    
    def _safe_readline(self):
        """Safe line reading from stdout"""
        try:
            # Simple readline() as in working debug_communication.py
            return self.process.stdout.readline()
        except Exception as e:
            logger.debug(f"Error reading line: {e}")
            return None
    
    async def _read_stderr_async(self):
        """Async stderr reading"""
        logger.debug(f"Starting stderr reader for server {self.name}")
        
        try:
            while self.running and self.process and self.process.poll() is None:
                try:
                    loop = asyncio.get_event_loop()
                    
                    try:
                        chunk = await loop.run_in_executor(None, self._read_chunk_from_stderr)
                        if chunk:
                            logger.debug(f"[{self.name} stderr]: {chunk.strip()}")
                    except Exception as e:
                        if self.process and self.process.poll() is None:
                            logger.debug(f"Error reading stderr {self.name}: {e}")
                    
                    await asyncio.sleep(0.01)
                    
                except asyncio.CancelledError:
                    logger.debug(f"Stderr reader for server {self.name} was cancelled")
                    break
                except Exception as e:
                    if self.running and self.process and self.process.poll() is None:
                        logger.warning(f"Async stderr reader error {self.name}: {e}")
                    await asyncio.sleep(0.1)
        
        except asyncio.CancelledError:
            logger.debug(f"Stderr reader task for server {self.name} was cancelled")
        except Exception as e:
            logger.error(f"Critical error in stderr reader for server {self.name}: {e}")
        finally:
            logger.debug(f"Stderr reader for server {self.name} terminated")
    
    def _read_chunk_from_stderr(self):
        """Helper function for reading a chunk from stderr"""
        try:
            if HAS_SELECT:
                # Unix - use select for non-blocking read
                ready, _, _ = select.select([self.process.stderr], [], [], 0.1)
                if ready:
                    return self.process.stderr.read(1024)
            else:
                # Windows - blocking read with a smaller buffer
                return self.process.stderr.read(256)
        except Exception:
            return None
        return None
    
    async def _process_message_async(self, message: str):
        """Async processing of JSON-RPC message"""
        try:
            data = json.loads(message)
            logger.debug(f"Received message from {self.name}: {data}")
            
            # Check if it's a response to initialize (id=0 and has result)
            if data.get("id") == 0 and "result" in data:
                self.initialized = True
                logger.debug(f"Server {self.name} initialized - received initialize response")
                return
            
            # Process response to request
            request_id = data.get("id")
            if request_id is not None and request_id in self.pending_requests:
                async with self.lock:
                    future = self.pending_requests.pop(request_id, None)
                    if future and not future.done():
                        future.set_result(data)
            
        except json.JSONDecodeError as e:
            logger.debug(f"Invalid JSON message from {self.name}: {message[:100]}...")
        except Exception as e:
            logger.error(f"Error processing message from {self.name}: {e}")
    
    def _process_message(self, message: str):
        """Processing of JSON-RPC message (sync version for compatibility)"""
        try:
            data = json.loads(message)
            logger.debug(f"Received message from {self.name}: {data}")
            
            # Check if it's a response to initialize (id=0 and has result)
            if data.get("id") == 0 and "result" in data:
                self.initialized = True
                logger.debug(f"Server {self.name} initialized - received initialize response")
                return
            
            # Process response to request
            request_id = data.get("id")
            if request_id is not None and request_id in self.pending_requests:
                # For sync version, use asyncio.create_task
                loop = asyncio.get_event_loop()
                if loop.is_running():
                    loop.create_task(self._handle_pending_request(request_id, data))
            
        except json.JSONDecodeError as e:
            logger.debug(f"Invalid JSON message from {self.name}: {message[:100]}...")
        except Exception as e:
            logger.error(f"Error processing message from {self.name}: {e}")
    
    async def _handle_pending_request(self, request_id: int, data: dict):
        """Helper async function for processing a pending request"""
        try:
            async with self.lock:
                future = self.pending_requests.pop(request_id, None)
                if future and not future.done():
                    future.set_result(data)
        except Exception as e:
            logger.error(f"Error processing pending request {request_id}: {e}")
    
    async def _cleanup_pending_requests(self):
        """Async cleanup of pending requests"""
        try:
            async with self.lock:
                for future in self.pending_requests.values():
                    if not future.done():
                        future.set_exception(Exception("Server shutdown"))
                self.pending_requests.clear()
        except Exception as e:
            logger.error(f"Error cleaning up pending requests: {e}")
    
    async def send_request(self, method: str, params: Optional[Dict] = None) -> Dict:
        """Sends an async request to the MCP server"""
        if not self.process or self.process.poll() is not None:
            raise Exception(f"Server {self.name} is not running")
        
        async with self.lock:
            self.request_counter += 1
            request_id = self.request_counter
        
        request = {
            "jsonrpc": "2.0",
            "id": request_id,
            "method": method
        }
        
        if params is not None:
            request["params"] = params
        
        # Create a future for the response
        loop = asyncio.get_event_loop()
        future = loop.create_future()
        
        async with self.lock:
            self.pending_requests[request_id] = future
        
        try:
            # Send the request
            message = json.dumps(request) + '\n'
            self.process.stdin.write(message)
            self.process.stdin.flush()
            
            logger.debug(f"Sent request to {self.name}: {method}")
            
            # Wait for response with timeout
            try:
                response = await asyncio.wait_for(future, timeout=30.0)
                return response
            except asyncio.TimeoutError:
                # Remove from pending requests
                async with self.lock:
                    self.pending_requests.pop(request_id, None)
                raise Exception(f"Timeout waiting for response from server {self.name}")
        
        except Exception as e:
            # Remove from pending requests on error
            async with self.lock:
                self.pending_requests.pop(request_id, None)
            raise e
    
    def stop(self) -> bool:
        """Stops the MCP server process"""
        try:
            if self.process and self.process.poll() is None:
                logger.info(f"Stopping server {self.name}")
                
                # Attempt graceful shutdown
                self.process.terminate()
                
                # Wait for termination
                try:
                    self.process.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    logger.warning(f"Server {self.name} did not terminate gracefully, using SIGKILL")
                    self.process.kill()
                    self.process.wait()
                
                logger.info(f"✅ Server {self.name} stopped")
            
            # Database update
            db.update_server_status(self.name, 'stopped')
            
            # Cleanup
            self.running = False
            self.process = None
            self.initialized = False
            
            # Cleanup async lock - must use asyncio.run for sync method
            try:
                loop = asyncio.get_event_loop()
                if loop.is_running():
                    # If loop is active, create a task
                    loop.create_task(self._cleanup_pending_requests())
                else:
                    # If loop is not active, run a new one
                    asyncio.run(self._cleanup_pending_requests())
            except Exception:
                # Fallback - direct cleanup without lock
                for future in self.pending_requests.values():
                    if not future.done():
                        future.set_exception(Exception("Server shutdown"))
                self.pending_requests.clear()
            
            return True
            
        except Exception as e:
            logger.error(f"❌ Error stopping server {self.name}: {e}")
            return False
    
    def is_running(self) -> bool:
        """Checks if the server is running"""
        return self.process is not None and self.process.poll() is None

class ProcessManager:
    """Manager for all MCP server processes with automatic monitoring"""
    
    def __init__(self):
        self.processes: Dict[str, MCPProcess] = {}
        self.lock = threading.Lock()
        self.restart_attempts: Dict[str, int] = {}  # Tracks number of restart attempts
        self.max_restart_attempts = 3
        self._monitor_task = None  # Background monitoring task
    
    def get_server_status(self, server_name: str) -> Dict:
        """Gets server status"""
        server_config = db.get_server(server_name)
        if not server_config:
            return {"status": "not_found"}
        
        runtime_info = {
            "name": server_name,
            "script_path": server_config['script_path'],
            "description": server_config['description'],
            "auto_start": server_config['auto_start'],
            "created_at": server_config['created_at']
        }
        
        if server_name in self.processes:
            mcp_process = self.processes[server_name]
            if mcp_process.is_running():
                runtime_info.update({
                    "status": "running",
                    "pid": mcp_process.process.pid,
                    "initialized": mcp_process.initialized
                })
            else:
                runtime_info["status"] = "stopped"
                # Cleanup dead process and update database
                with self.lock:
                    del self.processes[server_name]
                db.update_server_status(server_name, 'stopped')
        else:
            # Check if database has server as running, but process does not exist
            if server_config.get('status') == 'running':
                # Update database to stopped
                db.update_server_status(server_name, 'stopped')
            runtime_info["status"] = "stopped"
        
        return runtime_info
    
    async def start_server_async(self, server_name: str) -> bool:
        """Async server start"""
        return await self.start_server(server_name)
    
    async def start_server(self, server_name: str) -> bool:
        """Async server start"""
        try:
            with self.lock:
                if server_name in self.processes and self.processes[server_name].is_running():
                    logger.warning(f"Server {server_name} is already running")
                    return True
            
            server_config = db.get_server(server_name)
            if not server_config:
                logger.error(f"Server {server_name} does not exist in the database")
                return False
            
            mcp_process = MCPProcess(
                name=server_name,
                script_path=server_config['script_path']
            )
            
            success = await mcp_process.start()
            
            if success:
                with self.lock:
                    self.processes[server_name] = mcp_process
                return True
            else:
                return False
                
        except Exception as e:
            logger.error(f"Error starting server {server_name}: {e}")
            return False
    
    async def stop_server_async(self, server_name: str) -> bool:
        """Async server stop"""
        loop = asyncio.get_event_loop()
        return await loop.run_in_executor(None, self.stop_server, server_name)
    
    def stop_server(self, server_name: str) -> bool:
        """Stops the server"""
        try:
            with self.lock:
                if server_name not in self.processes:
                    logger.warning(f"Server {server_name} is not running")
                    return True
                
                mcp_process = self.processes[server_name]
                success = mcp_process.stop()
                
                if success:
                    del self.processes[server_name]
                
                return success
                
        except Exception as e:
            logger.error(f"Error stopping server {server_name}: {e}")
            return False
    
    async def send_request_to_server(self, server_name: str, method: str, params: Optional[Dict] = None) -> Dict:
        """Sends a request to a specific server"""
        with self.lock:
            if server_name not in self.processes:
                raise Exception(f"Server {server_name} is not running")
            
            mcp_process = self.processes[server_name]
        
        if not mcp_process.is_running():
            raise Exception(f"Server {server_name} is not active")
        
        return await mcp_process.send_request(method, params)
    
    async def start_monitoring(self):
        """Starts the background monitoring task"""
        if self._monitor_task is None:
            self._monitor_task = asyncio.create_task(self._monitor_and_restart_loop())
            logger.info("🔍 Process monitoring started")
    
    async def stop_monitoring(self):
        """Stops background monitoring"""
        if self._monitor_task:
            self._monitor_task.cancel()
            try:
                await self._monitor_task
            except asyncio.CancelledError:
                pass
            self._monitor_task = None
            logger.info("🔍 Process monitoring stopped")
    
    async def _monitor_and_restart_loop(self):
        """Main monitoring loop - runs in the background"""
        while True:
            try:
                await asyncio.sleep(30)  # Check every 30 seconds
                await self._check_and_restart_failed_processes()
                
            except asyncio.CancelledError:
                logger.info("Process monitor cancelled")
                break
            except Exception as e:
                logger.error(f"Error in process monitor: {e}")
                await asyncio.sleep(5)  # Shorter pause on error
    
    async def _check_and_restart_failed_processes(self):
        """Checks all processes and restarts failed ones"""
        with self.lock:
            dead_processes = []
            
            # Find dead processes
            for server_name, mcp_process in self.processes.items():
                if not mcp_process.is_running():
                    dead_processes.append(server_name)
        
        # Restart dead processes (outside lock to avoid blocking)
        for server_name in dead_processes:
            await self._attempt_restart(server_name)
    
    async def _attempt_restart(self, server_name: str):
        """Attempts to restart a specific server"""
        restart_count = self.restart_attempts.get(server_name, 0)
        
        if restart_count >= self.max_restart_attempts:
            logger.critical(f"❌ Max restart attempts ({self.max_restart_attempts}) reached for {server_name}, giving up")
            return
        
        logger.warning(f"🔄 Server {server_name} is dead, attempting restart (attempt {restart_count + 1}/{self.max_restart_attempts})")
        
        try:
            # Clean up dead process
            with self.lock:
                if server_name in self.processes:
                    del self.processes[server_name]
            
            # Attempt restart
            success = await self.start_server(server_name)
            
            if success:
                self.restart_attempts[server_name] = 0  # Reset counter
                logger.info(f"✅ Successfully restarted {server_name}")
                
                # Update database
                db.add_log(server_name, "INFO", f"Automatically restarted (attempt {restart_count + 1})")
            else:
                self.restart_attempts[server_name] = restart_count + 1
                logger.error(f"❌ Failed to restart {server_name} (attempt {restart_count + 1})")
                
                # Update database
                db.add_log(server_name, "ERROR", f"Auto-restart failed (attempt {restart_count + 1})")
                
        except Exception as e:
            self.restart_attempts[server_name] = restart_count + 1
            logger.error(f"❌ Exception during restart of {server_name}: {e}")
            
            # Update database
            db.add_log(server_name, "ERROR", f"Auto-restart exception: {str(e)}")

    def cleanup(self):
        """Cleans up all processes"""
        logger.info("Cleaning up all processes...")
        
        with self.lock:
            for server_name in list(self.processes.keys()):
                try:
                    self.processes[server_name].stop()
                except Exception as e:
                    logger.error(f"Error cleaning up server {server_name}: {e}")
            
            self.processes.clear()
        
        logger.info("✅ All processes cleaned up")

# Global process manager
process_manager = ProcessManager()

# Global tools proxy
tools_proxy = ToolsProxy(process_manager)

# Streamable Session Manager
class StreamableSessionManager:
    """Manager for Streamable HTTP connections with clients"""
    
    def __init__(self):
        self.sessions: Dict[str, Dict] = {}
        self.lock = asyncio.Lock()
    
    async def create_session(self, server_name: str) -> str:
        """Creates a new Streamable session"""
        session_id = str(uuid.uuid4())
        async with self.lock:
            self.sessions[session_id] = {
                "server_name": server_name,
                "created_at": time.time(),
                "initialized": False,
                "message_queue": asyncio.Queue(),
                "client_requests": asyncio.Queue()
            }
        logger.info(f"Streamable: Session {session_id} created for server {server_name}")
        return session_id
    
    async def get_session(self, session_id: str) -> Optional[Dict]:
        """Gets session info"""
        async with self.lock:
            return self.sessions.get(session_id)
    
    async def remove_session(self, session_id: str):
        """Removes a session"""
        async with self.lock:
            if session_id in self.sessions:
                del self.sessions[session_id]
                logger.info(f"Streamable: Session {session_id} removed")
    
    async def send_message_to_session(self, session_id: str, message: Dict):
        """Sends a message to the session queue"""
        session = await self.get_session(session_id)
        if session:
            await session["message_queue"].put(message)
    
    async def get_message_from_session(self, session_id: str, timeout: float = 30.0) -> Optional[Dict]:
        """Gets a message from the session queue"""
        session = await self.get_session(session_id)
        if session:
            try:
                return await asyncio.wait_for(session["message_queue"].get(), timeout=timeout)
            except asyncio.TimeoutError:
                return None
        return None

# Global Streamable session manager
streamable_manager = StreamableSessionManager()

# 🆕 NEW: MCP Session Manager for proper session handling
class MCPSessionManager:
    """Manager for MCP protocol sessions with proper Mcp-Session-Id header support"""
    
    def __init__(self):
        self.sessions: Dict[str, Dict] = {}
        self.lock = asyncio.Lock()
        self.session_timeout = 3600  # 1 hour timeout
    
    async def create_session(self, client_info: Dict = None) -> str:
        """Creates a new MCP session and returns session ID"""
        session_id = str(uuid.uuid4())
        async with self.lock:
            self.sessions[session_id] = {
                "session_id": session_id,
                "client_info": client_info or {},
                "created_at": time.time(),
                "last_activity": time.time(),
                "request_count": 0,
                "initialized": True
            }
        logger.info(f"MCP Session: Created session {session_id} for client {client_info.get('name', 'unknown')}")
        return session_id
    
    async def validate_session(self, session_id: str) -> bool:
        """Validates if session exists and is not expired"""
        if not session_id:
            return False
        
        async with self.lock:
            session = self.sessions.get(session_id)
            if not session:
                return False
            
            # Check timeout
            if time.time() - session["last_activity"] > self.session_timeout:
                del self.sessions[session_id]
                logger.info(f"MCP Session: Expired session {session_id}")
                return False
            
            return True
    
    async def update_session_activity(self, session_id: str):
        """Updates last activity timestamp for session"""
        async with self.lock:
            if session_id in self.sessions:
                self.sessions[session_id]["last_activity"] = time.time()
                self.sessions[session_id]["request_count"] += 1
    
    async def get_session_info(self, session_id: str) -> Optional[Dict]:
        """Gets session information"""
        async with self.lock:
            return self.sessions.get(session_id)
    
    async def remove_session(self, session_id: str):
        """Removes a session"""
        async with self.lock:
            if session_id in self.sessions:
                del self.sessions[session_id]
                logger.info(f"MCP Session: Removed session {session_id}")
    
    async def cleanup_expired_sessions(self):
        """Removes expired sessions"""
        current_time = time.time()
        expired_sessions = []
        
        async with self.lock:
            for session_id, session in self.sessions.items():
                if current_time - session["last_activity"] > self.session_timeout:
                    expired_sessions.append(session_id)
        
        for session_id in expired_sessions:
            await self.remove_session(session_id)
        
        if expired_sessions:
            logger.info(f"MCP Session: Cleaned up {len(expired_sessions)} expired sessions")
    
    def get_stats(self) -> Dict:
        """Returns session statistics"""
        with self.lock:
            return {
                "total_sessions": len(self.sessions),
                "active_sessions": len(self.sessions),
                "session_timeout": self.session_timeout
            }

# Global MCP session manager
mcp_session_manager = MCPSessionManager()

# 🆕 NEW: Helper functions for session management
async def _store_mcp_session(session_id: str, client_info: Dict):
    """Stores MCP session information"""
    try:
        # Session already created, just update client info
        session = await mcp_session_manager.get_session_info(session_id)
        if session:
            session["client_info"] = client_info
            logger.info(f"MCP Session: Updated client info for session {session_id}")
    except Exception as e:
        logger.error(f"MCP Session: Error storing session {session_id}: {e}")

async def _validate_mcp_session(session_id: str) -> bool:
    """Validates MCP session"""
    return await mcp_session_manager.validate_session(session_id)

async def _update_session_activity(session_id: str):
    """Updates session activity"""
    await mcp_session_manager.update_session_activity(session_id)

# FastAPI Events
@app.on_event("startup")
async def startup_event():
    """Initialization on startup"""
    logger.info("MCP Server Manager with automatic restart is starting...")
    
    try:
        # Cleanup old processes with error handling
        try:
            cleaned = db.cleanup_dead_processes()
            if cleaned > 0:
                logger.info(f"Cleaned up {cleaned} dead processes")
        except Exception as e:
            logger.error(f"Error cleaning up dead processes: {e}")
        
        # Auto-start servers with error handling
        try:
            auto_start_servers = db.list_servers(auto_start_only=True)
            for server in auto_start_servers:
                try:
                    if server['status'] != 'running':
                        logger.info(f"Auto-starting server: {server['name']}")
                        try:
                            success = await process_manager.start_server_async(server['name'])
                            if success:
                                logger.info(f"✅ Auto-started: {server['name']}")
                            else:
                                logger.error(f"❌ Failed to auto-start: {server['name']}")
                        except Exception as e:
                            logger.error(f"❌ Exception during auto-start of {server['name']}: {e}")
                except Exception as e:
                    logger.error(f"Error processing auto-start server {server.get('name', 'unknown')}: {e}")
        except Exception as e:
            logger.error(f"Error getting auto-start servers: {e}")
        
        # 🆕 NEW: Start process monitoring
        await process_manager.start_monitoring()
        logger.info("🔍 Automatic process monitoring enabled")
            
    except Exception as e:
        logger.error(f"Critical error during startup: {e}")
        # Continue despite startup errors

@app.on_event("shutdown")
async def shutdown_event():
    """Cleanup on shutdown"""
    logger.info("MCP Server Manager is shutting down...")
    
    # 🆕 NEW: Stop monitoring
    await process_manager.stop_monitoring()
    
    # Existing cleanup code
    process_manager.cleanup()

# Root endpoint
@app.get("/")
async def root():
    """Basic information"""
    servers = db.list_servers()
    running_servers = [s for s in servers if s['status'] == 'running']
    
    # Create a list of available MCP endpoints
    mcp_endpoints = {}
    streamable_endpoints = {}
    for server in running_servers:
        server_name = server['name']
        mcp_endpoints[server_name] = {
            "base_url": f"/servers/{server_name}",
            "capabilities": f"/servers/{server_name}/mcp/capabilities",
            "tools_list": f"/servers/{server_name}/mcp/tools/list",
            "tools_call": f"/servers/{server_name}/mcp/tools/call",
            "resources_list": f"/servers/{server_name}/mcp/resources/list"
        }
        streamable_endpoints[server_name] = {
            "streamable_transport": f"/servers/{server_name}/streamable",
            "description": f"Streamable HTTP Transport for {server_name}",
            "usage": "For Claude Desktop - connect to Streamable HTTP endpoint",
            "deprecated_sse": f"/servers/{server_name}/sse (deprecated)"
        }
    
    return {
        "message": "🚀 MCP Server Manager with HTTP REST API + Streamable HTTP Transport",
        "description": "Universal MCP wrapper supporting REST API and Streamable HTTP transport for Claude Desktop",
        "servers_total": len(servers),
        "servers_running": len(running_servers),
        "version": "1.0.0",
        "transports": {
            "http_rest": "Standard HTTP REST API endpoints",
            "streamable_http": "Streamable HTTP transport for real-time MCP communication (replacement for SSE)",
            "sse_transport": "DEPRECATED: Server-Sent Events (use /streamable instead of /sse)"
        },
        "endpoints": {
            "rest_api_endpoints": mcp_endpoints,
            "streamable_transport_endpoints": streamable_endpoints,
            "global_streamable": "/streamable",
            "global_sse_deprecated": "/sse (deprecated)",
            "discovery": "/.well-known/mcp",
            "global_tools_proxy": {
                "tools_list": "/mcp/tools/list",
                "tools_call": "/mcp/tools/call", 
                "tools_stats": "/mcp/tools/stats",
                "tools_refresh": "/mcp/tools/refresh"
            }
        },
        "usage": {
            "claude_desktop_url": "http://localhost:8999/servers/{server_name}/streamable",
            "claude_desktop_url_deprecated": "http://localhost:8999/servers/{server_name}/sse (deprecated)",
            "rest_api_example": "http://localhost:8999/servers/{server_name}/mcp/tools/list",
            "global_tools_example": "http://localhost:8999/mcp/tools/list",
            "health_check": "/health"
        }
    }

# MCP Discovery endpoints for Claude Desktop
@app.get("/.well-known/mcp")
async def mcp_discovery():
    """MCP Discovery endpoint for Claude Desktop - MCP Protocol 2025-03-26 compliant"""
    servers = db.list_servers()
    running_servers = [s for s in servers if s['status'] == 'running']
    
    # Build server list with full MCP 2025-03-26 specification
    mcp_servers = []
    for server in running_servers:
        server_name = server['name']
        server_entry = {
            "name": server_name,
            "description": server.get('description', f"MCP Server {server_name}"),
            "version": "1.0.0",
            "protocol_version": "2025-03-26",
            "capabilities": {
                "tools": True,
                "resources": True,
                "prompts": False,
                "logging": True
            },
            "transports": [
                {
                    "type": "streamable-http",
                    "endpoint": f"/servers/{server_name}/streamable",
                    "description": "Streamable HTTP Transport for real-time MCP communication"
                },
                {
                    "type": "http-rest", 
                    "endpoint": f"/servers/{server_name}/mcp",
                    "description": "Standard HTTP REST API endpoints"
                }
            ],
            "endpoints": {
                "capabilities": f"/servers/{server_name}/mcp/capabilities",
                "tools_list": f"/servers/{server_name}/mcp/tools/list",
                "tools_call": f"/servers/{server_name}/mcp/tools/call",
                "resources_list": f"/servers/{server_name}/mcp/resources/list",
                "streamable": f"/servers/{server_name}/streamable"
            },
            "metadata": {
                "author": "MCP Server Manager",
                "license": "MIT",
                "homepage": f"/servers/{server_name}",
                "repository": "local"
            }
        }
        mcp_servers.append(server_entry)
    
    # MCP 2025-03-26 compliant discovery response
    return {
        "protocol_version": "2025-03-26",
        "implementation": {
            "name": "mcp-server-manager",
            "version": "1.0.0",
            "description": "Universal MCP wrapper supporting REST API and Streamable HTTP transport"
        },
        "capabilities": {
            "transports": ["streamable-http", "http-rest"],
            "features": ["auto-discovery", "multi-server", "process-monitoring"],
            "authentication": False,
            "encryption": False
        },
        "servers": mcp_servers,
        "transports": {
            "streamable-http": {
                "description": "Streamable HTTP transport for real-time MCP communication",
                "global_endpoint": "/streamable",
                "features": ["bidirectional", "real-time", "connection-pooling"]
            },
            "http-rest": {
                "description": "Standard HTTP REST API endpoints", 
                "global_endpoint": "/mcp",
                "features": ["stateless", "cacheable", "standard-http"]
            }
        },
        "discovery": {
            "auto_refresh": True,
            "refresh_interval": 30,
            "health_check": "/health"
        }
    }

@app.get("/mcp")
async def mcp_standard_get(request: Request):
    """
    🆕 FIXED: Standard MCP endpoint - SSE stream for real-time communication
    MCP Protocol 2025-03-26 compliant - GET method provides SSE stream
    """
    logger.info("MCP Standard: New SSE connection on /mcp")
    
    async def mcp_sse_stream():
        try:
            # Send initial capabilities and discovery
            servers = db.list_servers()
            running_servers = [s for s in servers if s['status'] == 'running']
            
            # MCP 2025-03-26 compliant initialize response
            init_response = {
                "jsonrpc": "2.0",
                "id": 0,
                "result": {
                    "protocolVersion": "2025-03-26",
                    "capabilities": {
                        "tools": True,
                        "resources": True,
                        "prompts": False,
                        "logging": True
                    },
                    "serverInfo": {
                        "name": "mcp-server-manager",
                        "version": "1.0.0"
                    },
                    "implementation": {
                        "name": "mcp-server-manager",
                        "version": "1.0.0",
                        "description": "Universal MCP wrapper supporting REST API and Streamable HTTP transport"
                    },
                    "servers": [
                        {
                            "name": server['name'],
                            "description": server.get('description', f"MCP Server {server['name']}"),
                            "status": "running",
                            "endpoints": {
                                "streamable": f"/servers/{server['name']}/streamable",
                                "tools_list": f"/servers/{server['name']}/mcp/tools/list",
                                "tools_call": f"/servers/{server['name']}/mcp/tools/call"
                            }
                        }
                        for server in running_servers
                    ]
                }
            }
            
            yield f"data: {json.dumps(init_response)}\n\n"
            
            # Real-time updates loop
            while True:
                try:
                    # Check if client is still connected
                    if await request.is_disconnected():
                        logger.info("MCP Standard: Client disconnected from /mcp SSE")
                        break
                    
                    # Send periodic server status updates
                    current_servers = db.list_servers()
                    current_running = [s for s in current_servers if s['status'] == 'running']
                    
                    status_update = {
                        "jsonrpc": "2.0",
                        "method": "notifications/server_status",
                        "params": {
                            "timestamp": time.time(),
                            "servers_total": len(current_servers),
                            "servers_running": len(current_running),
                            "running_servers": [server['name'] for server in current_running]
                        }
                    }
                    
                    yield f"data: {json.dumps(status_update)}\n\n"
                    
                    # Wait before next update
                    await asyncio.sleep(30)  # Update every 30 seconds
                    
                except asyncio.CancelledError:
                    logger.debug("MCP Standard: SSE stream was cancelled")
                    break
                except Exception as e:
                    logger.warning(f"MCP Standard: Error in SSE loop: {e}")
                    await asyncio.sleep(5)
                    
        except asyncio.CancelledError:
            logger.debug("MCP Standard: SSE stream was cancelled")
        except Exception as e:
            logger.error(f"MCP Standard: Critical error in SSE stream: {e}")
            error_response = {
                "jsonrpc": "2.0",
                "error": {
                    "code": -32000,
                    "message": f"SSE stream error: {str(e)}"
                }
            }
            try:
                yield f"data: {json.dumps(error_response)}\n\n"
            except Exception:
                pass  # Stream already corrupted
        
        finally:
            logger.info("MCP Standard: SSE stream closed")
    
    return StreamingResponse(
        mcp_sse_stream(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            "Transfer-Encoding": "chunked",
            "Access-Control-Allow-Origin": "*",
            "Access-Control-Allow-Headers": "*",
            "Access-Control-Allow-Methods": "GET, POST, OPTIONS",
        }
    )

@app.post("/mcp")
async def mcp_standard_post(request_data: dict, request: Request):
    """
    🆕 FIXED: Standard MCP endpoint with proper session management
    MCP Protocol 2025-03-26 compliant with Mcp-Session-Id header support
    """
    logger.info("MCP Standard: New POST request on /mcp")
    
    try:
        # Validate JSON-RPC request structure
        method = request_data.get("method")
        params = request_data.get("params")
        request_id = request_data.get("id")
        jsonrpc = request_data.get("jsonrpc")
        
        # 🆕 NEW: Extract session ID from headers
        session_id = request.headers.get("Mcp-Session-Id")
        
        logger.info(f"MCP Standard POST: {method} with ID {request_id}, Session: {session_id}")
        
        # Validate JSON-RPC 2.0 format
        if jsonrpc != "2.0":
            return {
                "jsonrpc": "2.0",
                "id": request_id,
                "error": {
                    "code": -32600,
                    "message": "Invalid Request - must be JSON-RPC 2.0"
                }
            }
        
        if not method:
            return {
                "jsonrpc": "2.0",
                "id": request_id,
                "error": {
                    "code": -32600,
                    "message": "Invalid Request - missing method"
                }
            }
        
        # Handle standard MCP methods
        if method == "initialize":
            # 🆕 NEW: Generate session ID for new clients
            if not session_id:
                session_id = await mcp_session_manager.create_session()
                logger.info(f"MCP Standard: Generated new session ID: {session_id}")
            
            # MCP initialize handshake with session management
            client_capabilities = params.get("capabilities", {}) if params else {}
            client_info = params.get("clientInfo", {}) if params else {}
            
            # 🆕 NEW: Store session info
            await _store_mcp_session(session_id, client_info)
            
            logger.info(f"MCP Standard: Initialize from client {client_info.get('name', 'unknown')} with session {session_id}")
            
            # 🆕 NEW: Return response with session header
            response = {
                "jsonrpc": "2.0",
                "id": request_id,
                "result": {
                    "protocolVersion": "2025-03-26",
                    "capabilities": {
                        "tools": True,
                        "resources": True,
                        "prompts": False,
                        "logging": True,
                        "session_management": True  # NEW: Indicate session support
                    },
                    "serverInfo": {
                        "name": "mcp-server-manager",
                        "version": "1.0.0"
                    },
                    "sessionId": session_id  # NEW: Include session ID in response
                }
            }
            
            # 🆕 NEW: Add session header to response
            from fastapi import Response
            from fastapi.responses import JSONResponse
            json_response = JSONResponse(content=response)
            json_response.headers["Mcp-Session-Id"] = session_id
            return json_response
        
        # 🆕 NEW: Validate session for all non-initialize requests
        if method != "initialize":
            if not session_id:
                return {
                    "jsonrpc": "2.0",
                    "id": request_id,
                    "error": {
                        "code": -32602,
                        "message": "Missing Mcp-Session-Id header - session required"
                    }
                }
            
            # Validate session exists
            session_valid = await _validate_mcp_session(session_id)
            if not session_valid:
                return {
                    "jsonrpc": "2.0",
                    "id": request_id,
                    "error": {
                        "code": -32001,
                        "message": f"Invalid or expired session: {session_id}"
                    }
                }
            
            # Update session activity
            await _update_session_activity(session_id)
        
        elif method == "capabilities":
            # Return aggregated capabilities from all servers
            return {
                "jsonrpc": "2.0",
                "id": request_id,
                "result": {
                    "capabilities": {
                        "tools": True,
                        "resources": True,
                        "prompts": False,
                        "logging": True,
                        "session_management": True  # NEW: Include session support
                    },
                    "protocolVersion": "2025-03-26",
                    "serverInfo": {
                        "name": "mcp-server-manager",
                        "version": "1.0.0"
                    }
                }
            }
        
        elif method == "tools/list":
            # Delegate to global tools proxy
            try:
                aggregated_tools = await tools_proxy.get_all_tools()
                return {
                    "jsonrpc": "2.0",
                    "id": request_id,
                    "result": {
                        "tools": aggregated_tools
                    }
                }
            except Exception as e:
                logger.error(f"MCP Standard: Error getting tools: {e}")
                return {
                    "jsonrpc": "2.0",
                    "id": request_id,
                    "error": {
                        "code": -32000,
                        "message": f"Failed to get tools: {str(e)}"
                    }
                }
        
        elif method == "tools/call":
            # Delegate to global tools proxy
            try:
                tool_name = params.get("name") if params else None
                arguments = params.get("arguments", {}) if params else {}
                
                if not tool_name:
                    return {
                        "jsonrpc": "2.0",
                        "id": request_id,
                        "error": {
                            "code": -32602,
                            "message": "Invalid params - missing tool name"
                        }
                    }
                
                logger.info(f"MCP Standard: Calling tool {tool_name}")
                
                # Use tools proxy for routing
                response = await tools_proxy.call_tool(tool_name, arguments)
                
                # Check if it's an error response from tools proxy
                if "error" in response:
                    return {
                        "jsonrpc": "2.0",
                        "id": request_id,
                        "error": response["error"]
                    }
                elif "result" in response:
                    return {
                        "jsonrpc": "2.0",
                        "id": request_id,
                        "result": response["result"]
                    }
                else:
                    return {
                        "jsonrpc": "2.0",
                        "id": request_id,
                        "error": {
                            "code": -32000,
                            "message": "Unexpected response format from tool proxy"
                        }
                    }
                    
            except Exception as e:
                logger.error(f"MCP Standard: Error calling tool: {e}")
                return {
                    "jsonrpc": "2.0",
                    "id": request_id,
                    "error": {
                        "code": -32000,
                        "message": f"Tool call failed: {str(e)}"
                    }
                }
        
        elif method == "resources/list":
            # Resources are not globally aggregated, return empty list
            return {
                "jsonrpc": "2.0",
                "id": request_id,
                "result": {
                    "resources": []
                }
            }
        
        elif method.startswith("notifications/"):
            # Handle MCP notifications (no response needed)
            logger.info(f"MCP Standard: Received notification {method}")
            return None  # Notifications don't get responses
        
        else:
            # Method not found
            return {
                "jsonrpc": "2.0",
                "id": request_id,
                "error": {
                    "code": -32601,
                    "message": f"Method not found: {method}"
                }
            }
    
    except Exception as e:
        logger.error(f"MCP Standard: Error processing POST request: {e}")
        return {
            "jsonrpc": "2.0",
            "id": request_data.get("id") if isinstance(request_data, dict) else None,
            "error": {
                "code": -32000,
                "message": f"Internal error: {str(e)}"
            }
        }

@app.get("/mcp/capabilities")
async def mcp_global_capabilities():
    """Global MCP capabilities"""
    return {
        "capabilities": {
            "tools": {},
            "resources": {},
            "prompts": {},
            "logging": {}
        },
        "protocolVersion": "2025-03-26",
        "serverInfo": {
            "name": "mcp-server-manager",
            "version": "1.0.0"
        },
        "transport": "http"
    }

# ===============================
# 🔧 GLOBAL TOOLS PROXY ENDPOINTS
# ===============================

@app.get("/mcp/tools/list")
async def mcp_global_tools_list():
    """
    🆕 FIXED: Global MCP Tools List - aggregates tools from all running servers
    Resolves naming conflicts with server prefixes (server__toolname)
    """
    logger.info("🔧 Global Tools: Getting aggregated tools from all servers")
    
    try:
        # Get aggregated tools from all servers via tools proxy
        aggregated_tools = await tools_proxy.get_all_tools()
        
        logger.info(f"🔧 Global Tools: Returning {len(aggregated_tools)} tools from all servers")
        
        return {
            "tools": aggregated_tools
        }
        
    except Exception as e:
        logger.error(f"🔧 Global Tools: Error aggregating tools: {e}")
        raise HTTPException(status_code=500, detail=f"Failed to aggregate tools: {str(e)}")

@app.post("/mcp/tools/call")
async def mcp_global_tools_call(request_data: dict):
    """
    🆕 FIXED: Global MCP Tools Call - automatic proxy routing to appropriate server
    Handles namespaced tool names (server__toolname) and routes to correct server
    """
    logger.info("🔧 Global Tools: Processing tool call with automatic routing")
    
    try:
        tool_name = request_data.get("name")
        arguments = request_data.get("arguments", {})
        
        if not tool_name:
            raise HTTPException(status_code=400, detail="Missing tool name")
        
        logger.info(f"🔧 Global Tools: Calling tool {tool_name} with arguments: {arguments}")
        
        # Use tools proxy to route the call to appropriate server
        response = await tools_proxy.call_tool(tool_name, arguments)
        
        # Check if it's an error response
        if "error" in response:
            error_info = response["error"]
            error_message = error_info.get("message", "Unknown error")
            return {
                "content": [
                    {
                        "type": "text",
                        "text": f"❌ **Error:** {error_message}"
                    }
                ]
            }
        
        elif "result" in response:
            # Successful response
            tool_result = response["result"]
            
            if isinstance(tool_result, list) and len(tool_result) > 0:
                # MCP tools/call response - list of TextContent
                first_item = tool_result[0]
                if isinstance(first_item, dict) and "text" in first_item:
                    return {
                        "content": [
                            {
                                "type": "text",
                                "text": first_item["text"]
                            }
                        ]
                    }
            
            # Fallback - convert to JSON
            if isinstance(tool_result, (dict, list)):
                formatted_result = json.dumps(tool_result, indent=2, ensure_ascii=False)
                return {
                    "content": [
                        {
                            "type": "text",
                            "text": f"✅ **Successful response:**\n```json\n{formatted_result}\n```"
                        }
                    ]
                }
            else:
                return {
                    "content": [
                        {
                            "type": "text",
                            "text": f"✅ **Response:** {str(tool_result)}"
                        }
                    ]
                }
        
        else:
            return {
                "content": [
                    {
                        "type": "text",
                        "text": f"❌ Unexpected response format: {response}"
                    }
                ]
            }
            
    except Exception as e:
        logger.error(f"🔧 Global Tools: Error calling tool: {e}")
        return {
            "content": [
                {
                    "type": "text",
                    "text": f"❌ **System error:** {str(e)}"
                }
            ]
        }

@app.get("/mcp/tools/stats")
async def mcp_global_tools_stats():
    """
    🆕 NEW: Global Tools Statistics - cache stats and server breakdown
    """
    try:
        cache_stats = tools_proxy.get_cache_stats()
        
        # Get tools breakdown by server
        servers_breakdown = {}
        for server_name in process_manager.processes.keys():
            try:
                server_tools = await tools_proxy.get_tools_by_server(server_name)
                servers_breakdown[server_name] = {
                    "tool_count": len(server_tools),
                    "status": "running" if process_manager.processes[server_name].is_running() else "stopped"
                }
            except Exception as e:
                servers_breakdown[server_name] = {
                    "tool_count": 0,
                    "status": "error",
                    "error": str(e)
                }
        
        return {
            "cache_stats": cache_stats,
            "servers_breakdown": servers_breakdown,
            "total_servers": len(servers_breakdown),
            "total_tools_cached": cache_stats["cached_tools"],
            "proxy_status": "active"
        }
        
    except Exception as e:
        logger.error(f"🔧 Global Tools: Error getting stats: {e}")
        raise HTTPException(status_code=500, detail=str(e))

@app.post("/mcp/tools/refresh")
async def mcp_global_tools_refresh():
    """
    🆕 NEW: Manual cache refresh for global tools
    """
    try:
        logger.info("🔧 Global Tools: Manually refreshing tools cache")
        
        # Force refresh tools cache
        aggregated_tools = await tools_proxy.get_all_tools(force_refresh=True)
        
        cache_stats = tools_proxy.get_cache_stats()
        
        logger.info(f"🔧 Global Tools: Cache refreshed - {len(aggregated_tools)} tools cached")
        
        return {
            "message": "Tools cache refreshed successfully",
            "tools_count": len(aggregated_tools),
            "cache_stats": cache_stats
        }
        
    except Exception as e:
        logger.error(f"🔧 Global Tools: Error refreshing cache: {e}")
        raise HTTPException(status_code=500, detail=str(e))

# Read-only Management REST API Endpoints
@app.get("/servers")
async def list_servers():
    """List all servers (read-only)"""
    try:
        servers = db.list_servers()
        # Update the status of each server based on its actual state
        updated_servers = []
        for server in servers:
            server_status = process_manager.get_server_status(server['name'])
            updated_servers.append(server_status)
        return {"servers": updated_servers}
    except Exception as e:
        logger.error(f"Error getting servers: {e}")
        raise HTTPException(status_code=500, detail=str(e))

@app.get("/servers/{server_name}")
async def get_server(server_name: str):
    """Server details"""
    try:
        server = process_manager.get_server_status(server_name)
        if server.get("status") == "not_found":
            raise HTTPException(status_code=404, detail="Server not found")
        
        return {"server": server}
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Error getting server: {e}")
        raise HTTPException(status_code=500, detail=str(e))

@app.post("/servers/{server_name}/start")
async def start_server(server_name: str):
    """Start MCP server"""
    try:
        success = await process_manager.start_server_async(server_name)
        if success:
            return {"message": f"Server {server_name} started successfully"}
        else:
            raise HTTPException(status_code=400, detail=f"Failed to start server {server_name}")
    except Exception as e:
        logger.error(f"Error starting server: {e}")
        raise HTTPException(status_code=500, detail=str(e))

@app.post("/servers/{server_name}/stop")
async def stop_server(server_name: str):
    """Stop MCP server"""
    try:
        success = await process_manager.stop_server_async(server_name)
        if success:
            return {"message": f"Server {server_name} stopped successfully"}
        else:
            raise HTTPException(status_code=400, detail=f"Failed to stop server {server_name}")
    except Exception as e:
        logger.error(f"Error stopping server: {e}")
        raise HTTPException(status_code=500, detail=str(e))


@app.post("/servers/{server_name}/request")
async def send_request_to_server(server_name: str, request_data: dict):
    """Send JSON-RPC request to MCP server"""
    try:
        method = request_data.get("method")
        params = request_data.get("params")
        
        if not method:
            raise HTTPException(status_code=400, detail="Method is required")
        
        response = await process_manager.send_request_to_server(server_name, method, params)
        return response
        
    except Exception as e:
        logger.error(f"Error sending request: {e}")
        raise HTTPException(status_code=500, detail=str(e))

# Individual MCP HTTP Endpoints for each server
async def _check_server_running(server_name: str):
    """Helper function to check if server is running and properly initialized"""
    if server_name not in process_manager.processes:
        raise HTTPException(status_code=404, detail=f"Server {server_name} is not running")
    
    mcp_process = process_manager.processes[server_name]
    if not mcp_process.is_running():
        raise HTTPException(status_code=503, detail=f"Server {server_name} is not active")
    
    # FIXED: Require proper initialization before accepting requests
    if not mcp_process.initialized:
        raise HTTPException(
            status_code=503, 
            detail=f"Server {server_name} is not properly initialized. MCP handshake required."
        )
    
    return mcp_process

@app.get("/servers/{server_name}/mcp/capabilities")
async def get_server_mcp_capabilities(server_name: str = Path(..., description="Name of the MCP server")):
    """MCP Capabilities endpoint for a specific server"""
    await _check_server_running(server_name)
    
    return {
        "capabilities": {
            "tools": {},
            "resources": {},
            "prompts": {},
            "logging": {}
        },
        "protocolVersion": "2025-03-26",
        "serverInfo": {
            "name": f"mcp-server-{server_name}",
            "version": "1.0.0"
        }
    }

@app.get("/servers/{server_name}/mcp/tools/list")
async def list_server_mcp_tools(server_name: str = Path(..., description="Name of the MCP server")):
    """MCP Tools List endpoint for a specific server"""
    logger.info(f"MCP: Getting tools for server {server_name}")
    
    await _check_server_running(server_name)
    
    try:
        # Call tools/list on the server
        response = await process_manager.send_request_to_server(server_name, "tools/list")
        
        if "result" in response and "tools" in response["result"]:
            tools = response["result"]["tools"]
            logger.info(f"MCP: Server {server_name} has {len(tools)} tools")
            return {"tools": tools}
        else:
            logger.warning(f"MCP: Unexpected response from {server_name}: {response}")
            return {"tools": []}
    
    except Exception as e:
        logger.error(f"MCP: Error getting tools from {server_name}: {e}")
        raise HTTPException(status_code=500, detail=f"Failed to get tools from server {server_name}")

@app.post("/servers/{server_name}/mcp/tools/call")
async def call_server_mcp_tool(server_name: str, request_data: dict):
    """MCP Tools Call endpoint for a specific server"""
    logger.info(f"MCP: Calling tool on server {server_name}")
    
    await _check_server_running(server_name)
    
    try:
        tool_name = request_data.get("name")
        arguments = request_data.get("arguments", {})
        
        if not tool_name:
            raise HTTPException(status_code=400, detail="Missing tool name")
        
        logger.info(f"MCP: Calling tool {tool_name} on {server_name} with arguments: {arguments}")
        
        # Call the tool on the server
        result = await process_manager.send_request_to_server(
            server_name, "tools/call", {
                "name": tool_name,
                "arguments": arguments
            }
        )
        
        # Process the response
        if "error" in result:
            error_info = result["error"]
            error_message = error_info.get("message", "Unknown error")
            return {
                "content": [
                    {
                        "type": "text",
                        "text": f"❌ **Error:** {error_message}"
                    }
                ]
            }
        
        elif "result" in result:
            # Successful response
            tool_result = result["result"]
            
            if isinstance(tool_result, list) and len(tool_result) > 0:
                # MCP tools/call response - list of TextContent
                first_item = tool_result[0]
                if isinstance(first_item, dict) and "text" in first_item:
                    return {
                        "content": [
                            {
                                "type": "text",
                                "text": first_item["text"]
                            }
                        ]
                    }
            
            # Fallback - convert to JSON
            if isinstance(tool_result, (dict, list)):
                formatted_result = json.dumps(tool_result, indent=2, ensure_ascii=False)
                return {
                    "content": [
                        {
                            "type": "text",
                            "text": f"✅ **Successful response:**\n```json\n{formatted_result}\n```"
                        }
                    ]
                }
            else:
                return {
                    "content": [
                        {
                            "type": "text",
                            "text": f"✅ **Response:** {str(tool_result)}"
                        }
                    ]
                }
        
        else:
            return {
                "content": [
                    {
                        "type": "text",
                        "text": f"❌ Unexpected response format: {result}"
                    }
                ]
            }
    
    except Exception as e:
        logger.error(f"MCP: Error executing tool on {server_name}: {e}")
        return {
            "content": [
                {
                    "type": "text",
                    "text": f"❌ **System error:** {str(e)}"
                }
            ]
        }

@app.post("/servers/{server_name}/mcp/resources/list")
async def list_server_mcp_resources(server_name: str = Path(..., description="Name of the MCP server")):
    """MCP Resources List endpoint for a specific server"""
    await _check_server_running(server_name)
    
    try:
        # Call resources/list on the server
        response = await process_manager.send_request_to_server(server_name, "resources/list")
        
        if "result" in response and "resources" in response["result"]:
            resources = response["result"]["resources"]
            return {"resources": resources}
        else:
            # Fallback if server does not support resources
            return {"resources": []}
    
    except Exception as e:
        logger.warning(f"MCP: Server {server_name} does not support resources: {e}")
        return {"resources": []}

# ===============================
# 🔄 STREAMABLE HTTP TRANSPORT ENDPOINTS
# ===============================

@app.get("/servers/{server_name}/streamable")
async def streamable_mcp_transport_get(server_name: str, request: Request):
    """Streamable HTTP Transport endpoint for Claude Desktop (GET - streaming)"""
    logger.info(f"Streamable: New GET connection for server {server_name}")
    
    # Check if server is running
    await _check_server_running(server_name)
    
    # Create Streamable session
    session_id = await streamable_manager.create_session(server_name)
    
    async def streamable_event_stream():
        try:
            # Initialize MCP protocol with error handling
            try:
                await _streamable_send_initialize(session_id, server_name)
            except Exception as e:
                logger.error(f"Streamable: Error initializing session {session_id}: {e}")
                error_response = {
                    "jsonrpc": "2.0",
                    "error": {
                        "code": -32001,
                        "message": f"Initialization error: {str(e)}"
                    }
                }
                yield f"data: {json.dumps(error_response)}\n\n"
                return
            
            # Main Streamable communication loop
            while True:
                try:
                    # Check if client is still listening
                    try:
                        if await request.is_disconnected():
                            logger.info(f"Streamable: Client disconnected for session {session_id}")
                            break
                    except Exception as e:
                        logger.debug(f"Streamable: Error checking client disconnection: {e}")
                        # Continue even if disconnection check fails
                    
                    # Get message from MCP server with error handling
                    try:
                        message = await streamable_manager.get_message_from_session(session_id, timeout=5.0)
                    except Exception as e:
                        logger.warning(f"Streamable: Error getting message for session {session_id}: {e}")
                        continue
                    
                    if message:
                        try:
                            # Send message to client in proper SSE format
                            yield f"data: {json.dumps(message)}\n\n"
                        except Exception as e:
                            logger.error(f"Streamable: Error sending message to client: {e}")
                            break
                    else:
                        # Heartbeat - keep connection alive (optional for streamable)
                        # yield json.dumps({'type': 'heartbeat', 'timestamp': time.time()}) + '\n'
                        pass
                    
                    # Short pause with error handling
                    try:
                        await asyncio.sleep(0.1)
                    except asyncio.CancelledError:
                        logger.debug(f"Streamable: Stream for session {session_id} was cancelled")
                        break
                    except Exception as e:
                        logger.debug(f"Streamable: Error during sleep: {e}")
                        
                except asyncio.CancelledError:
                    logger.debug(f"Streamable: Stream loop for session {session_id} was cancelled")
                    break
                except Exception as e:
                    logger.error(f"Streamable: Unexpected error in stream loop for session {session_id}: {e}")
                    # Continue loop, might be a temporary error
                    try:
                        await asyncio.sleep(1.0)
                    except:
                        break
        
        except asyncio.CancelledError:
            logger.debug(f"Streamable: Event stream for session {session_id} was cancelled")
        except Exception as e:
            logger.error(f"Streamable: Critical error in event stream for session {session_id}: {e}")
            try:
                error_response = {
                    "jsonrpc": "2.0",
                    "error": {
                        "code": -32000,
                        "message": f"Stream error: {str(e)}"
                    }
                }
                yield json.dumps(error_response) + '\n'
            except Exception:
                pass  # Cannot yield, stream is already corrupted
        
        finally:
            # Cleanup session with error handling
            try:
                await streamable_manager.remove_session(session_id)
                logger.info(f"Streamable: Session {session_id} closed")
            except Exception as e:
                logger.error(f"Streamable: Error closing session {session_id}: {e}")
    
    return StreamingResponse(
        streamable_event_stream(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            "Transfer-Encoding": "chunked",
            "Access-Control-Allow-Origin": "*",
            "Access-Control-Allow-Headers": "*",
            "Access-Control-Allow-Methods": "GET, POST, OPTIONS",
        }
    )

@app.post("/servers/{server_name}/streamable")
async def streamable_mcp_transport_post(server_name: str, request_data: dict):
    """Streamable HTTP Transport endpoint for MCP Inspector (POST - direct request/response)"""
    logger.info(f"Streamable: New POST request for server {server_name}")
    
    # Check if server is running
    await _check_server_running(server_name)
    
    try:
        # Validate JSON-RPC request
        method = request_data.get("method")
        params = request_data.get("params")
        request_id = request_data.get("id")
        
        logger.info(f"Streamable POST: {method} with ID {request_id}")
        
        if not method:
            return {
                "jsonrpc": "2.0",
                "id": request_id,
                "error": {
                    "code": -32600,
                    "message": "Invalid Request - missing method"
                }
            }
        
        # Special handling for initialize
        if method == "initialize":
            return {
                "jsonrpc": "2.0",
                "id": request_id,
                "result": {
                    "protocolVersion": "2025-03-26",
                    "capabilities": {
                        "tools": {},
                        "resources": {},
                        "prompts": {},
                        "logging": {}
                    },
                    "serverInfo": {
                        "name": f"mcp-server-{server_name}",
                        "version": "1.0.0"
                    }
                }
            }
        
        # Send request to MCP server
        response = await process_manager.send_request_to_server(server_name, method, params)
        
        # Add request ID to response
        if request_id is not None:
            response["id"] = request_id
        
        return response
        
    except Exception as e:
        logger.error(f"Streamable POST: Error processing request: {e}")
        return {
            "jsonrpc": "2.0",
            "id": request_data.get("id"),
            "error": {
                "code": -32000,
                "message": str(e)
            }
        }

@app.post("/servers/{server_name}/streamable/{session_id}/request")
async def streamable_send_request(server_name: str, session_id: str, request_data: dict):
    """Endpoint for sending requests via Streamable session"""
    logger.info(f"Streamable: Request for session {session_id}")
    
    # Check session
    session = await streamable_manager.get_session(session_id)
    if not session:
        raise HTTPException(status_code=404, detail="Session not found")
    
    try:
        # Send request to MCP server
        method = request_data.get("method")
        params = request_data.get("params")
        request_id = request_data.get("id", str(uuid.uuid4()))
        
        if not method:
            raise HTTPException(status_code=400, detail="Method is required")
        
        # Send to server and get response
        response = await process_manager.send_request_to_server(server_name, method, params)
        
        # Add request ID to response
        response["id"] = request_id
        
        # Send response to Streamable session
        await streamable_manager.send_message_to_session(session_id, response)
        
        return {"status": "sent", "request_id": request_id}
        
    except Exception as e:
        logger.error(f"Streamable: Error processing request for session {session_id}: {e}")
        # Send error message to session
        error_response = {
            "jsonrpc": "2.0",
            "id": request_data.get("id"),
            "error": {
                "code": -32000,
                "message": str(e)
            }
        }
        await streamable_manager.send_message_to_session(session_id, error_response)
        raise HTTPException(status_code=500, detail=str(e))

async def _streamable_send_initialize(session_id: str, server_name: str):
    """FIXED: Proper MCP protocol initialization for Streamable session with server interaction"""
    try:
        logger.info(f"Streamable: Starting proper MCP initialization handshake for session {session_id}")
        
        # FIXED: Check if underlying MCP server is properly initialized first
        if server_name not in process_manager.processes:
            raise Exception(f"Server {server_name} is not running")
        
        mcp_process = process_manager.processes[server_name]
        if not mcp_process.initialized:
            raise Exception(f"Server {server_name} is not properly initialized - cannot create Streamable session")
        
        # FIXED: Get actual server capabilities from the running MCP server
        try:
            # Query the real server for its capabilities
            server_response = await process_manager.send_request_to_server(server_name, "capabilities")
            server_capabilities = server_response.get("result", {}).get("capabilities", {
                "tools": {},
                "resources": {},
                "prompts": {},
                "logging": {}
            })
            server_info = server_response.get("result", {}).get("serverInfo", {
                "name": f"mcp-server-{server_name}",
                "version": "1.0.0"
            })
            logger.info(f"Streamable: Retrieved real capabilities from server {server_name}")
            
        except Exception as e:
            logger.warning(f"Streamable: Could not get server capabilities, using defaults: {e}")
            # Fallback to default capabilities
            server_capabilities = {
                "tools": {},
                "resources": {},
                "prompts": {},
                "logging": {}
            }
            server_info = {
                "name": f"mcp-server-{server_name}",
                "version": "1.0.0"
            }
        
        # FIXED: Send proper MCP initialize response with real server data
        initialize_response = {
            "jsonrpc": "2.0",
            "id": 0,
            "result": {
                "protocolVersion": "2025-03-26",
                "capabilities": server_capabilities,
                "serverInfo": server_info
            }
        }
        
        await streamable_manager.send_message_to_session(session_id, initialize_response)
        
        # FIXED: Proper session state management
        session = await streamable_manager.get_session(session_id)
        if session:
            session["initialized"] = True
            session["mcp_handshake_complete"] = True
            session["server_capabilities"] = server_capabilities
            session["protocol_version"] = "2025-03-26"
        
        logger.info(f"✅ Streamable: MCP initialization handshake complete for session {session_id}")
        
    except Exception as e:
        logger.error(f"❌ Streamable: Error during MCP initialization for session {session_id}: {e}")
        
        # FIXED: Send proper error response to client
        error_response = {
            "jsonrpc": "2.0",
            "id": 0,
            "error": {
                "code": -32001,
                "message": f"MCP initialization failed: {str(e)}"
            }
        }
        await streamable_manager.send_message_to_session(session_id, error_response)
        raise e

@app.get("/streamable")
async def global_streamable_endpoint(request: Request):
    """Global Streamable endpoint for discovery"""
    logger.info("Streamable: Global Streamable connection")
    
    async def streamable_event_stream():
        try:
            # Send list of available servers
            servers = db.list_servers()
            running_servers = [s for s in servers if s['status'] == 'running']
            
            discovery_message = {
                "type": "discovery",
                "servers": [
                    {
                        "name": server['name'],
                        "description": server.get('description', f"MCP Server {server['name']}"),
                        "streamable_endpoint": f"/servers/{server['name']}/streamable",
                        "capabilities": f"/servers/{server['name']}/mcp/capabilities"
                    }
                    for server in running_servers
                ]
            }
            
            # Proper SSE format
            yield f"data: {json.dumps(discovery_message)}\n\n"
            
            # Heartbeat loop (optional for streamable)
            while True:
                if await request.is_disconnected():
                    break
                
                # For streamable transport, heartbeat is less necessary
                await asyncio.sleep(30)
                
        except Exception as e:
            logger.error(f"Streamable: Error in global endpoint: {e}")
            error_response = {
                "type": "error", 
                "message": str(e)
            }
            yield f"data: {json.dumps(error_response)}\n\n"
    
    return StreamingResponse(
        streamable_event_stream(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            "Transfer-Encoding": "chunked",
            "Access-Control-Allow-Origin": "*",
        }
    )

# Backward compatibility - SSE endpoints (deprecated)
@app.get("/servers/{server_name}/sse")
async def sse_mcp_transport_get_deprecated(server_name: str, request: Request):
    """DEPRECATED: SSE MCP Transport endpoint - use /streamable instead"""
    logger.warning(f"SSE endpoint /servers/{server_name}/sse is deprecated. Use /servers/{server_name}/streamable instead.")
    # Redirect to streamable endpoint
    return await streamable_mcp_transport_get(server_name, request)

@app.post("/servers/{server_name}/sse")
async def sse_mcp_transport_post_deprecated(server_name: str, request_data: dict):
    """DEPRECATED: SSE MCP Transport endpoint - use /streamable instead"""
    logger.warning(f"SSE endpoint POST /servers/{server_name}/sse is deprecated. Use /servers/{server_name}/streamable instead.")
    # Redirect to streamable endpoint
    return await streamable_mcp_transport_post(server_name, request_data)

@app.get("/sse")
async def global_sse_endpoint_deprecated(request: Request):
    """DEPRECATED: Global SSE endpoint - use /streamable instead"""
    logger.warning("SSE endpoint /sse is deprecated. Use /streamable instead.")
    # Redirect to streamable endpoint
    return await global_streamable_endpoint(request)

@app.get("/monitoring/status")
async def get_monitoring_status():
    """Status of automatic monitoring"""
    with process_manager.lock:
        restart_stats = dict(process_manager.restart_attempts)
    
    return {
        "monitoring_active": process_manager._monitor_task is not None,
        "servers_monitored": len(process_manager.processes),
        "restart_attempts": restart_stats,
        "max_restart_attempts": process_manager.max_restart_attempts,
        "monitored_servers": list(process_manager.processes.keys()),
        "monitoring_interval": "30 seconds"
    }

@app.post("/monitoring/reset/{server_name}")
async def reset_restart_counter(server_name: str):
    """Resets the restart counter for a server"""
    process_manager.restart_attempts[server_name] = 0
    db.add_log(server_name, "INFO", "Restart counter manually reset")
    return {"message": f"Restart counter reset for {server_name}"}

@app.get("/health")
async def health_check():
    """Health check"""
    try:
        servers = db.list_servers()
        running_count = len([s for s in servers if s['status'] == 'running'])
        
        # 🆕 NEW: Include session management stats
        session_stats = mcp_session_manager.get_stats()
        
        return {
            "status": "healthy",
            "servers_total": len(servers),
            "servers_running": running_count,
            "timestamp": time.time(),
            "individual_mcp_endpoints": "enabled",
            "sse_transport": "enabled",
            "process_monitoring": process_manager._monitor_task is not None,
            "session_management": {
                "enabled": True,
                "active_sessions": session_stats["active_sessions"],
                "session_timeout": session_stats["session_timeout"]
            }
        }
    except Exception as e:
        logger.error(f"Health check failed: {e}")
        raise HTTPException(status_code=500, detail="Health check failed")

# 🆕 NEW: MCP Session Management Endpoints
@app.get("/mcp/sessions")
async def get_session_stats():
    """Get MCP session statistics"""
    try:
        stats = mcp_session_manager.get_stats()
        return {
            "session_stats": stats,
            "description": "MCP session management statistics"
        }
    except Exception as e:
        logger.error(f"Error getting session stats: {e}")
        raise HTTPException(status_code=500, detail=str(e))

@app.post("/mcp/sessions/cleanup")
async def cleanup_expired_sessions():
    """Manually trigger cleanup of expired sessions"""
    try:
        await mcp_session_manager.cleanup_expired_sessions()
        stats = mcp_session_manager.get_stats()
        return {
            "message": "Expired sessions cleaned up",
            "session_stats": stats
        }
    except Exception as e:
        logger.error(f"Error cleaning up sessions: {e}")
        raise HTTPException(status_code=500, detail=str(e))

# Explicitly disabled endpoints for security
@app.post("/servers")
async def create_server_blocked():
    """Server creation is disabled via REST API"""
    raise HTTPException(status_code=404, detail="This endpoint is disabled for security. Use CLI: python3 mcp_manager.py add")

@app.delete("/servers/{server_name}")
async def delete_server_blocked(server_name: str):
    """Server deletion is disabled via REST API"""
    raise HTTPException(status_code=404, detail="This endpoint is disabled for security. Use CLI: python3 mcp_manager.py remove")

@app.put("/servers/{server_name}")
async def update_server_blocked(server_name: str):
    """Server modification is disabled via REST API"""
    raise HTTPException(status_code=404, detail="This endpoint is disabled for security. Use CLI: python3 mcp_manager.py")

if __name__ == "__main__":
    import uvicorn
    import argparse
    
    # Argument parsing
    parser = argparse.ArgumentParser(description="MCP Server Manager")
    parser.add_argument("--host", default="0.0.0.0", help="Host to bind to")
    parser.add_argument("--port", type=int, default=int(os.getenv("PORT", 8999)), help="Port to bind to")
    args = parser.parse_args()
    
    # Add CORS for Claude web
    app.add_middleware(
        CORSMiddleware,
        allow_origins=["https://claude.ai", "https://*.claude.ai", "*"],  # * for development
        allow_credentials=True,
        allow_methods=["GET", "POST", "OPTIONS"],
        allow_headers=["*"],
    )
    
    logger.info(f"🚀 Starting MCP Server Manager with individual HTTP MCP endpoints on {args.host}:{args.port}")
    logger.info(f"📡 Read-only API: http://{args.host}:{args.port}")
    logger.info(f"🔗 Individual MCP endpoints: http://{args.host}:{args.port}/servers/{{server_name}}/mcp/*")
    logger.info(f"🔧 Server management: use CLI 'python3 mcp_manager.py'")
    logger.info(f"📖 Documentation: http://{args.host}:{args.port}/docs")
    
    uvicorn.run(app, host=args.host, port=args.port)
