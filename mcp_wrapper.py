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
from fastapi.responses import StreamingResponse, JSONResponse
from mcp_database import MCPDatabase
from contextlib import asynccontextmanager
import psutil
import queue
import uuid
from dotenv import load_dotenv
from error_logger import (
    get_error_logger, log_server_crash, log_server_restart, 
    log_streamable_error, log_tools_list_error, log_tools_call_error,
    log_initialization_error, log_custom_error
)


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

def check_concurrent_server_dependencies():
    """Check if concurrent_mcp_server.py can be imported"""
    try:
        import concurrent_mcp_server
        logger.info("✅ concurrent_mcp_server.py imports successfully")
        return True
    except ImportError as e:
        logger.error(f"❌ concurrent_mcp_server.py import failed: {e}")
        return False
    except Exception as e:
        logger.error(f"❌ concurrent_mcp_server.py error: {e}")
        return False

# Log warning messages for missing Unix functions
if not HAS_FCNTL or not HAS_SELECT:
    if not HAS_FCNTL:
        logger.warning("fcntl is not available on this system (Windows). Non-blocking IO will be bypassed.")
    if not HAS_SELECT:
        logger.warning("select is not available on this system (Windows). IO operations will be synchronous.")

@asynccontextmanager
async def lifespan(app: FastAPI):
    # Startup logic
    logger.info("MCP Server Manager starting...")

    try:
        # Check dependencies
        if not check_concurrent_server_dependencies():
            logger.error("Cannot start - concurrent_mcp_server.py has issues")

        # Cleanup + auto-start + monitoring
        cleaned = db.cleanup_dead_processes()
        if cleaned > 0:
            logger.info(f"Cleaned up {cleaned} dead processes")

        auto_start_servers = db.list_servers(auto_start_only=True)
        for server in auto_start_servers:
            if server['status'] != 'running':
                logger.info(f"Auto-starting server: {server['name']}")
                success = await process_manager.start_server_async(server['name'])
                if success:
                    logger.info(f"✅ Auto-started: {server['name']}")
                else:
                    logger.error(f"❌ Failed to auto-start: {server['name']}")

        await process_manager.start_monitoring()
        logger.info("🔍 Automatic process monitoring enabled")

    except Exception as e:
        logger.error(f"Critical error during startup: {e}")

    yield

    # Shutdown logic
    logger.info("MCP Server Manager shutting down...")
    await process_manager.stop_monitoring()
    process_manager.cleanup()

# Update FastAPI app creation
app = FastAPI(
    title="MCP Server Manager with Individual HTTP MCP endpoints", 
    version="1.0.0",
    lifespan=lifespan  # Add this
)

# Global database
db = MCPDatabase()

# Import tools proxy
from tools_proxy import ToolsProxy

class MCPProcess:
    """Wrapper for MCP server process with async communication (no ports - stdin/stdout)"""

    def __init__(self, name: str, script_path: str):
        self.name = name
        # ✅ FIX: Convert to absolute path to avoid path issues
        self.script_path = os.path.abspath(script_path)
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

            # ✅ ADD: Check if script file exists
            if not os.path.exists(self.script_path):
                logger.error(f"Script file does not exist: {self.script_path}")
                return False

            # ✅ ADD: Check if script is executable
            if not os.access(self.script_path, os.R_OK):
                logger.error(f"Script file is not readable: {self.script_path}")
                return False

            env = os.environ.copy()

            # ✅ BETTER: Add current directory to Python path
            env['PYTHONPATH'] = os.getcwd() + ':' + env.get('PYTHONPATH', '')

            self.process = subprocess.Popen(
                ['python3', self.script_path],
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                env=env,
                bufsize=0,
                cwd=os.getcwd()  # ✅ ADD: Set working directory
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

            # ✅ ADD: Read any immediate stderr after process start
            await asyncio.sleep(0.5)  # Give process time to fail

            if self.process.poll() is not None:
                exit_code = self.process.poll()
                # Read stderr synchronously since process is dead
                stderr_output = ""
                try:
                    stderr_output = self.process.stderr.read()
                except:
                    pass

                logger.error(f"Process {self.name} failed immediately (exit {exit_code})")
                logger.error(f"Stderr: {stderr_output}")
                return False

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

            # Log server crash with context
            log_server_crash(self.name, e, {
                "phase": "startup",
                "script_path": self.script_path,
                "pid": self.process.pid if self.process else None,
                "initialized": self.initialized
            })

            # Cleanup on error
            self.running = False
            if self.process:
                try:
                    self.process.terminate()
                except Exception:
                    pass
                self.process = None
            return False

    async def _wait_for_initialization_async(self, timeout: int = 60):
        """FIXED: Proper MCP initialization handshake with capability negotiation"""
        start_time = time.time()
        init_sent = False
        initialized_sent = False
        initialization_response_received = False
        server_capabilities = None

        logger.info(f"Starting MCP initialization for {self.name}")

        # ✅ ADD: Check process is still alive before starting
        if self.process.poll() is not None:
            exit_code = self.process.poll()
            stderr_output = self.process.stderr.read() if self.process.stderr else "No stderr"
            raise Exception(f"Server process {self.name} terminated immediately (exit code: {exit_code}). Stderr: {stderr_output}")

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
                            "protocolVersion": "2024-11-05",
            "capabilities": {
                "tools": {"version": "1.0.0"},
                "resources": {"version": "1.0.0"},
                "prompts": {"version": "1.0.0"},
                "logging": {"version": "1.0.0"}
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
                    # FIXED: Notifications MUST NOT have 'id' field according to MCP protocol
                    initialized_notification = {
                        "jsonrpc": "2.0",
                        "method": "notifications/initialized"
                        # NO 'id' field for notifications!
                    }

                    self.process.stdin.write(json.dumps(initialized_notification) + '\n')
                    self.process.stdin.flush()
                    initialized_sent = True
                    logger.info(f"✅ Sent MCP initialized notification for server {self.name} (without ID - correct protocol)")

                    # FIXED: Server is now fully ready for tool calls
                    logger.info(f"🎉 MCP handshake complete for server {self.name} - ready for tool calls")
                    await asyncio.sleep(0.5)  # Stabilization wait
                    break

            except Exception as e:
                logger.debug(f"MCP initialization {self.name} in progress... ({e})")

            await asyncio.sleep(0.5)

        # FIXED: Strict validation of initialization state
        if not self.initialized:
            error = Exception(f"❌ Server {self.name} did not complete MCP initialization within {timeout}s - handshake failed")
            log_initialization_error(self.name, error, {
                "phase": "initialization_timeout",
                "timeout": timeout,
                "init_sent": init_sent,
                "response_received": initialization_response_received,
                "initialized_sent": initialized_sent
            })
            raise error
        elif not initialization_response_received:
            error = Exception(f"❌ Server {self.name} never responded to initialize request - protocol violation")
            log_initialization_error(self.name, error, {
                "phase": "no_response",
                "timeout": timeout,
                "init_sent": init_sent
            })
            raise error
        elif not initialized_sent:
            error = Exception(f"❌ Server {self.name} initialized but notifications/initialized was not sent - incomplete handshake")
            log_initialization_error(self.name, error, {
                "phase": "incomplete_handshake",
                "init_sent": init_sent,
                "response_received": initialization_response_received
            })
            raise error
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

        # Get server info for logging context
        server_config = db.get_server(server_name)
        process_info = None
        exit_code = None

        # Get process details if available
        if server_name in self.processes:
            mcp_process = self.processes[server_name]
            if mcp_process.process:
                exit_code = mcp_process.process.poll()
                process_info = {
                    "pid": mcp_process.process.pid if mcp_process.process else None,
                    "exit_code": exit_code,
                    "was_initialized": mcp_process.initialized,
                    "was_running": mcp_process.running
                }

        logger.warning(f"🔄 Server {server_name} is dead (exit_code: {exit_code}), attempting restart (attempt {restart_count + 1}/{self.max_restart_attempts})")

        # Log the process failure detection
        log_custom_error("WARNING", "PROCESS_FAILURE_DETECTED", server_name, 
                         f"Process monitoring detected server {server_name} failure", 
                         context={
                             "detection_reason": "process_not_running",
                             "exit_code": exit_code,
                             "process_info": process_info,
                             "server_config": {
                                 "script_path": server_config.get('script_path') if server_config else None,
                                 "auto_start": server_config.get('auto_start') if server_config else None
                             },
                             "restart_attempt": restart_count + 1,
                             "max_attempts": self.max_restart_attempts
                         })

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

                # Log successful restart with detailed context
                log_server_restart(server_name, restart_count + 1, True, {
                    "restart_reason": "process_monitoring_detected_failure",
                    "monitoring_interval": "30_seconds",
                    "max_attempts": self.max_restart_attempts
                })

                # Update database
                db.add_log(server_name, "INFO", f"Automatically restarted (attempt {restart_count + 1})")
            else:
                self.restart_attempts[server_name] = restart_count + 1
                logger.error(f"❌ Failed to restart {server_name} (attempt {restart_count + 1})")

                # Log failed restart attempt
                log_server_restart(server_name, restart_count + 1, False, {
                    "restart_reason": "process_monitoring_detected_failure", 
                    "failure_reason": "start_server_returned_false",
                    "remaining_attempts": self.max_restart_attempts - (restart_count + 1)
                })

                # Update database
                db.add_log(server_name, "ERROR", f"Auto-restart failed (attempt {restart_count + 1})")

        except Exception as e:
            self.restart_attempts[server_name] = restart_count + 1
            logger.error(f"❌ Exception during restart of {server_name}: {e}")

            # Log restart exception with full context
            log_server_restart(server_name, restart_count + 1, False, {
                "restart_reason": "process_monitoring_detected_failure",
                "failure_reason": "exception_during_restart",
                "exception_type": type(e).__name__,
                "exception_message": str(e),
                "remaining_attempts": self.max_restart_attempts - (restart_count + 1)
            })

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


# FastAPI Events are now handled by the lifespan context manager

# Root endpoint
@app.get("/")
async def root():
    """Basic information"""
    servers = db.list_servers()
    running_servers = [s for s in servers if s['status'] == 'running']

    # Create a list of available MCP endpoints
    mcp_endpoints = {}
    for server in running_servers:
        server_name = server['name']
        mcp_endpoints[server_name] = {
            "base_url": f"/servers/{server_name}",
            "capabilities": f"/servers/{server_name}/mcp/capabilities",
            "tools_list": f"/servers/{server_name}/mcp/tools/list",
            "tools_call": f"/servers/{server_name}/mcp/tools/call",
            "resources_list": f"/servers/{server_name}/mcp/resources/list"
        }

    return {
        "message": "🚀 MCP Server Manager with HTTP REST API",
        "description": "Universal MCP wrapper supporting REST API for server management",
        "servers_total": len(servers),
        "servers_running": len(running_servers),
        "version": "1.0.0",
        "transports": {
            "http_rest": "Standard HTTP REST API endpoints"
        },
        "endpoints": {
            "rest_api_endpoints": mcp_endpoints,
            "discovery": "/.well-known/mcp",
            "global_tools_proxy": {
                "tools_list": "/mcp/tools/list",
                "tools_call": "/mcp/tools/call", 
                "tools_stats": "/mcp/tools/stats",
                "tools_refresh": "/mcp/tools/refresh"
            }
        },
        "usage": {
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
                "tools": {"version": "1.0.0"},
                "resources": {"version": "1.0.0"},
                "prompts": {"version": "1.0.0"},
                "logging": {"version": "1.0.0"}
            },
            "transports": [
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
                "resources_list": f"/servers/{server_name}/mcp/resources/list"
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
            "description": "Universal MCP wrapper supporting REST API"
        },
        "capabilities": {
            "transports": ["http-rest"],
            "features": ["auto-discovery", "multi-server", "process-monitoring"],
            "authentication": False,
            "encryption": False
        },
        "servers": mcp_servers,
        "transports": {
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
    Standard MCP endpoint - REST API for MCP protocol
    MCP Protocol 2025-03-26 compliant
    """
    logger.info("MCP Standard: New GET request on /mcp")

    # Send capabilities and discovery
    servers = db.list_servers()
    running_servers = [s for s in servers if s['status'] == 'running']

    # MCP 2025-03-26 compliant response
    return {
        "jsonrpc": "2.0",
        "id": 0,
        "result": {
            "protocolVersion": "2025-03-26",
            "capabilities": {
                "tools": {"version": "1.0.0"},
                "resources": {"version": "1.0.0"},
                "prompts": {"version": "1.0.0"},
                "logging": {"version": "1.0.0"}
            },
            "serverInfo": {
                "name": "mcp-server-manager",
                "version": "1.0.0"
            },
            "implementation": {
                "name": "mcp-server-manager",
                "version": "1.0.0",
                "description": "Universal MCP wrapper supporting REST API"
            },
            "servers": [
                {
                    "name": server['name'],
                    "description": server.get('description', f"MCP Server {server['name']}"),
                    "status": "running",
                    "endpoints": {
                        "tools_list": f"/servers/{server['name']}/mcp/tools/list",
                        "tools_call": f"/servers/{server['name']}/mcp/tools/call"
                    }
                }
                for server in running_servers
            ]
        }
    }

@app.post("/mcp")
async def mcp_standard_post(request_data: dict, request: Request):
    """
    Standard MCP endpoint for REST API
    MCP Protocol 2025-03-26 compliant
    """
    logger.info("MCP Standard: New POST request on /mcp")

    try:
        # Validate JSON-RPC request structure
        method = request_data.get("method")
        params = request_data.get("params")
        request_id = request_data.get("id")
        jsonrpc = request_data.get("jsonrpc")

        logger.info(f"MCP Standard POST: {method} with ID {request_id}")

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
            # MCP initialize handshake
            client_capabilities = params.get("capabilities", {}) if params else {}
            client_info = params.get("clientInfo", {}) if params else {}

            client_name = client_info.get('name', 'unknown') if client_info else 'unknown'
            logger.info(f"MCP Standard: Initialize from client {client_name}")

            return {
                "jsonrpc": "2.0",
                "id": request_id,
                "result": {
                    "protocolVersion": "2025-03-26",
                    "capabilities": {
                        "tools": {"version": "1.0.0"},
                        "resources": {"version": "1.0.0"},
                        "prompts": {"version": "1.0.0"},
                        "logging": {"version": "1.0.0"}
                    },
                    "serverInfo": {
                        "name": "mcp-server-manager",
                        "version": "1.0.0"
                    }
                }
            }

        if method == "capabilities":
            # Return aggregated capabilities from all servers
            return {
                "jsonrpc": "2.0",
                "id": request_id,
                "result": {
                    "capabilities": {
                        "tools": {"version": "1.0.0"},
                        "resources": {"version": "1.0.0"},
                        "prompts": {"version": "1.0.0"},
                        "logging": {"version": "1.0.0"}
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
                "tools": {"version": "1.0.0"},
                "resources": {"version": "1.0.0"},
                "prompts": {"version": "1.0.0"},
                "logging": {"version": "1.0.0"}
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

@app.get("/servers/{server_name}/warmup")
async def warmup_server(server_name: str):
    """Pre-warm server to ensure it's ready for tools/list"""
    try:
        # Check if server is running
        mcp_process = process_manager.processes.get(server_name)
        if not mcp_process or not mcp_process.is_running():
            return {"status": "not_running", "message": f"Server {server_name} is not running"}

        if not mcp_process.initialized:
            return {"status": "initializing", "message": f"Server {server_name} is still initializing"}

        # Test tools/list call
        try:
            response = await process_manager.send_request_to_server(server_name, "tools/list")
            if "result" in response:
                tools_count = len(response["result"].get("tools", []))
                return {
                    "status": "ready", 
                    "message": f"Server {server_name} is ready",
                    "tools_count": tools_count
                }
            else:
                return {"status": "error", "message": f"Server {server_name} tools/list failed"}
        except Exception as e:
            return {"status": "error", "message": f"Server {server_name} warmup failed: {str(e)}"}

    except Exception as e:
        return {"status": "error", "message": f"Warmup check failed: {str(e)}"}

# ===============================
# 🌊 MCP-PROXY TRANSPORT ENDPOINTS
# ===============================

@app.get("/servers/{server_name}/sse")
async def server_sse_transport(server_name: str, request: Request):
    """SSE transport endpoint for specific server via mcp-proxy"""
    return await _handle_proxy_transport(server_name, "sse", request)

@app.post("/servers/{server_name}/sse") 
async def server_sse_transport_post(server_name: str, request: Request):
    """SSE transport POST endpoint for specific server via mcp-proxy"""
    return await _handle_proxy_transport(server_name, "sse", request)

@app.get("/servers/{server_name}/streamable")
async def server_streamable_transport(server_name: str, request: Request):
    """Streamable HTTP transport endpoint for specific server via mcp-proxy"""
    return await _handle_proxy_transport(server_name, "streamable", request)

@app.post("/servers/{server_name}/streamable")
async def server_streamable_transport_post(server_name: str, request: Request):
    """Streamable HTTP transport POST endpoint for specific server via mcp-proxy"""
    return await _handle_proxy_transport(server_name, "streamable", request)

async def _handle_proxy_transport(server_name: str, transport: str, request: Request):
    """Handle proxy transport requests"""
    try:
        # Check if server exists and is running
        server_status = process_manager.get_server_status(server_name)
        if server_status.get("status") != "running":
            raise HTTPException(status_code=404, detail=f"Server {server_name} is not running")
        
        # Get server config to check transport type
        server_config = db.get_server(server_name)
        if not server_config:
            raise HTTPException(status_code=404, detail=f"Server {server_name} not found")
        
        config_data = server_config.get('config_data', {})
        if isinstance(config_data, dict):
            server_transport = config_data.get('transport', 'sse')
        else:
            server_transport = 'sse'
        
        # Check if requested transport matches server configuration
        if transport != server_transport:
            raise HTTPException(
                status_code=400, 
                detail=f"Server {server_name} is configured for {server_transport}, not {transport}"
            )
        
        # Forward to internal mcp-proxy (to be implemented)
        # For now, return information about the endpoint
        return {
            "server_name": server_name,
            "transport": transport,
            "status": "available",
            "proxy_endpoint": f"/servers/{server_name}/{transport}",
            "message": f"MCP {transport} transport endpoint for {server_name}",
            "note": "Connect via mcp-proxy client for full MCP protocol support"
        }
        
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Error handling proxy transport for {server_name}: {e}")
        raise HTTPException(status_code=500, detail=str(e))
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

        # Log tools/list error with context
        log_tools_list_error(server_name, e, {
            "operation": "mcp_endpoint_tools_list",
            "endpoint": f"/servers/{server_name}/mcp/tools/list",
            "server_status": process_manager.get_server_status(server_name).get("status", "unknown")
        })

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

        # Log tools/call error with context
        log_tools_call_error(server_name, tool_name, e, {
            "operation": "mcp_endpoint_tools_call",
            "endpoint": f"/servers/{server_name}/mcp/tools/call",
            "tool_name": tool_name,
            "arguments": arguments,
            "server_status": process_manager.get_server_status(server_name).get("status", "unknown")
        })

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
        # Simplified health check for debugging
        return {
            "status": "healthy",
            "timestamp": time.time(),
            "debug": "basic_health_check"
        }

        # Original code commented out for debugging
        # servers = db.list_servers()
        # running_count = len([s for s in servers if s['status'] == 'running'])

    except Exception as e:
        logger.error(f"Health check failed: {e}")
        raise HTTPException(status_code=500, detail=f"Health check failed: {str(e)}")


# ===============================
# 🚨 ERROR LOGGING ENDPOINTS
# ===============================

@app.get("/logs/errors")
async def get_error_logs(hours: int = 24, server_name: Optional[str] = None):
    """Get recent error logs"""
    try:
        error_logger = get_error_logger()
        recent_errors = error_logger.get_recent_errors(hours=hours, server_name=server_name)

        return {
            "errors": recent_errors,
            "total_count": len(recent_errors),
            "hours_filter": hours,
            "server_filter": server_name,
            "description": f"Recent errors from last {hours} hours"
        }
    except Exception as e:
        logger.error(f"Error getting error logs: {e}")
        raise HTTPException(status_code=500, detail=str(e))

@app.get("/logs/errors/stats")
async def get_error_stats():
    """Get error statistics and system info"""
    try:
        error_logger = get_error_logger()
        stats = error_logger.get_error_stats()

        return {
            "error_stats": stats,
            "description": "Error logging statistics and system information"
        }
    except Exception as e:
        logger.error(f"Error getting error stats: {e}")
        raise HTTPException(status_code=500, detail=str(e))

@app.get("/logs/errors/{server_name}")
async def get_server_error_logs(server_name: str, hours: int = 24):
    """Get error logs for specific server"""
    try:
        error_logger = get_error_logger()
        server_errors = error_logger.get_recent_errors(hours=hours, server_name=server_name)

        # Get server status for context
        server_status = process_manager.get_server_status(server_name)

        return {
            "server_name": server_name,
            "server_status": server_status,
            "errors": server_errors,
            "total_count": len(server_errors),
            "hours_filter": hours,
            "description": f"Recent errors for server {server_name} from last {hours} hours"
        }
    except Exception as e:
        logger.error(f"Error getting server error logs: {e}")
        raise HTTPException(status_code=500, detail=str(e))

@app.post("/logs/test")
async def test_error_logging():
    """Test endpoint to generate sample error logs"""
    try:
        # Test different types of error logging
        test_server = "test_server"

        # Test server crash logging
        test_error = Exception("Test server crash for logging demonstration")
        log_server_crash(test_server, test_error, {
            "phase": "test",
            "test_type": "manual_test",
            "timestamp": time.time()
        })

        # Test restart logging
        log_server_restart(test_server, 1, True, {
            "restart_reason": "manual_test",
            "test": True
        })


        # Test tools error logging
        log_tools_list_error(test_server, test_error, {
            "operation": "test",
            "test": True
        })

        # Test custom error logging
        log_custom_error("INFO", "TEST", test_server, "Test custom error log", test_error, {
            "test": True,
            "manual_trigger": True
        })

        return {
            "message": "Test error logs generated successfully",
            "server_name": test_server,
            "logs_generated": 4,
            "types": ["server_crash", "server_restart", "tools_error", "custom_error"]
        }
    except Exception as e:
        logger.error(f"Error testing error logging: {e}")
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

# ===============================
# 🔗 PROXY MANAGEMENT ENDPOINTS  
# ===============================

@app.get("/proxy/status")
async def get_proxy_status():
    """Get status of mcp-proxy instances"""
    try:
        # Try to get status from proxy manager if it's running
        # This would need to be implemented in proxy manager
        return {
            "proxy_manager": "not_implemented",
            "message": "Use mcp_proxy_manager.py to manage proxy instances",
            "instructions": {
                "start_proxy_manager": "python mcp_proxy_manager.py",
                "view_endpoints": "Check console output for proxy endpoints"
            }
        }
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

@app.get("/proxy/endpoints")
async def list_proxy_endpoints():
    """List all available proxy endpoints for running servers"""
    try:
        servers = db.list_servers()
        running_servers = [s for s in servers if s['status'] == 'running']
        
        endpoints = {}
        base_port = 9000
        
        for i, server in enumerate(running_servers):
            server_name = server['name']
            
            # Get server config
            config_data = server.get('config_data', {})
            if isinstance(config_data, dict):
                transport = config_data.get('transport', 'sse')
                mode = config_data.get('mode', 'unrestricted')
            else:
                transport = 'sse'
                mode = 'unrestricted'
            
            port = base_port + i
            
            if transport == 'sse':
                endpoint = f"http://localhost:{port}/servers/{server_name}/sse"
            else:
                endpoint = f"http://localhost:{port}/servers/{server_name}/mcp"
            
            endpoints[server_name] = {
                "transport": transport,
                "mode": mode,
                "port": port,
                "endpoint": endpoint,
                "status": "estimated"  # Actual status would need proxy manager integration
            }
        
        return {
            "proxy_endpoints": endpoints,
            "total_servers": len(running_servers),
            "base_port": base_port,
            "note": "Run 'python mcp_proxy_manager.py' to start actual proxy instances"
        }
        
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

if __name__ == "__main__":
    import uvicorn
    import argparse

    # Argument parsing
    parser = argparse.ArgumentParser(description="MCP Server Manager")
    parser.add_argument("--host", default="0.0.0.0", help="Host to bind to")
    parser.add_argument("--port", type=int, default=int(os.getenv("PORT", 8999)), help="Port to bind to")
    args = parser.parse_args()


    logger.info(f"🚀 Starting MCP Server Manager with individual HTTP MCP endpoints on {args.host}:{args.port}")
    logger.info(f"📡 Read-only API: http://{args.host}:{args.port}")
    logger.info(f"🔗 Individual MCP endpoints: http://{args.host}:{args.port}/servers/{{server_name}}/mcp/*")
    logger.info(f"🔧 Server management: use CLI 'python3 mcp_manager.py'")
    logger.info(f"📖 Documentation: http://{args.host}:{args.port}/docs")

    uvicorn.run(app, host=args.host, port=args.port)
