#!/bin/bash
# startup.sh - Startup script for MCP container

set -e

# Load environment variables
if [ -f .env ]; then
    export $(cat .env | xargs)
fi

# Set default port if not provided
PORT=${PORT:-8999}

echo "🚀 Starting MCP Server container..."

# Start MCP wrapper in background
echo "🔄 Starting MCP wrapper..."
python mcp_wrapper.py --host 0.0.0.0 --port $PORT &

# Wait a moment for initialization
sleep 5

# Start auto-restart script
echo "🔄 Starting auto-restart servers..."
python auto_restart.py

# Wait for MCP wrapper (running in background)
echo "✅ MCP Server is ready"
echo "📡 API available at: http://localhost:$PORT"

# Keep container alive
wait
