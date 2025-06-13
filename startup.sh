#!/bin/bash
set -e

PORT=${PORT:-8999}
echo "🚀 Starting MCP Server container..."

# 1. Start main wrapper
echo "🔄 Starting MCP wrapper..."
python mcp_wrapper.py --host 0.0.0.0 --port $PORT &

# 2. Wait and auto-start servers
sleep 5
echo "🔄 Starting auto-restart servers..."
python auto_restart.py

# 3. Start mcp-proxy manager
echo "🔄 Starting MCP proxy manager..."
python mcp_proxy_manager.py &
PROXY_PID=$!

# Wait for proxy manager to initialize
sleep 10

echo "✅ MCP System ready"
echo "📡 REST API: http://localhost:$PORT"
echo "📡 Proxy endpoints: Check proxy manager output for specific ports"

wait