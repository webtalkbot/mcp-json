#!/bin/bash
set -e

# Load environment variables
PORT=${PORT:-8999}
PROXY_PORT=${PROXY_PORT:-9000}

echo "🚀 Starting MCP Server container..."

# 1. Start main wrapper
echo "🔄 Starting MCP wrapper..."
python mcp_wrapper.py --host 0.0.0.0 --port $PORT &
WRAPPER_PID=$!

# 2. Wait for wrapper to initialize
sleep 5

# 3. Auto-start servers
echo "🔄 Starting auto-restart servers..."
python auto_restart.py

# 4. Wait for servers to be ready
sleep 3

# 5. Start single mcp-proxy manager
echo "🔄 Starting single MCP proxy manager..."
python mcp_proxy_manager.py &
PROXY_PID=$!

# 6. Wait for proxy to initialize
sleep 8

echo ""
echo "✅ MCP System ready"
echo "📡 REST API:    http://localhost:$PORT"
echo "🎯 Proxy (SSE): http://localhost:$PROXY_PORT"
echo ""
echo "🔗 Quick test:"
echo "   curl http://localhost:$PORT/servers"
echo "   curl http://localhost:$PROXY_PORT/opensubtitles/sse"
echo ""
echo "💡 Check proxy manager output above for all server endpoints"

# Keep the container running
wait $WRAPPER_PID