#!/bin/bash
set -e

# Load environment variables from .env file if it exists
if [ -f .env ]; then
    echo "📝 Loading environment variables from .env file..."
    source .env
else
    echo "⚠️  .env file not found, using default values"
fi

# Set default values if not defined in .env
PORT=${PORT:-8999}
PROXY_PORT=${PROXY_PORT:-9000}
NGROK_URL=${NGROK_URL:-"http://localhost:$PROXY_PORT"}

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

# 5. Validate environment settings
echo "📝 Environment configured:"
echo "   NGROK_URL: $NGROK_URL"
echo "   PROXY_PORT: $PROXY_PORT"

# 6. Generate TBXark/mcp-proxy config from database
echo "🔄 Generating TBXark MCP proxy config..."
python generate_config.py --env-file .env --show-endpoints

if [ $? -ne 0 ]; then
    echo "❌ Config generation failed!"
    exit 1
fi

# 7. Start TBXark/mcp-proxy with correct path
echo "🔄 Starting TBXark MCP proxy on port $PROXY_PORT..."
$HOME/go/bin/mcp-proxy --config config.json &
TBXARK_PROXY_PID=$!

# 8. Wait for proxy to initialize
sleep 8

echo ""
echo "✅ MCP System ready"
echo "📡 REST API:        http://localhost:$PORT"
echo "🎯 TBXark Proxy:    $NGROK_URL"
echo "🏠 Local Proxy:     http://localhost:$PROXY_PORT"
echo ""

# Show available endpoints from generated config
if [ -f config.json ]; then
    echo "🔗 Available TBXark server endpoints:"
    python -c "
import json
try:
    with open('config.json', 'r') as f:
        config = json.load(f)
    base_url = config.get('mcpProxy', {}).get('baseURL', 'http://localhost:$PROXY_PORT')
    servers = config.get('mcpServers', {}).keys()
    for server in servers:
        print(f'   ✨ {server}: {base_url}/{server}/sse')
except Exception as e:
    print(f'   Error reading config: {e}')
"
fi

echo ""
echo "🔗 Quick test:"
echo "   curl http://localhost:$PORT/servers"
echo "   curl http://localhost:$PROXY_PORT/health"
echo ""

# Cleanup function
cleanup() {
    echo "🔄 Shutting down..."
    kill $WRAPPER_PID 2>/dev/null || true
    kill $TBXARK_PROXY_PID 2>/dev/null || true
    exit 0
}

trap cleanup SIGTERM SIGINT

# Keep the container running - wait for main wrapper
wait $WRAPPER_PID