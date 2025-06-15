#!/bin/bash

# Load environment variables from .env file
if [ -f .env ]; then
    export $(grep -v '^#' .env | xargs)
else
    echo "Error: .env file not found"
    exit 1
fi

# Check if NGROK_URL is set
if [ -z "$NGROK_URL" ]; then
    echo "Error: NGROK_URL not found in .env file"
    exit 1
fi

# Check if PROXY_PORT is set
if [ -z "$PROXY_PORT" ]; then
    echo "Error: PROXY_PORT not found in .env file"
    exit 1
fi

# Extract domain from the full URL (remove http:// or https://)
NGROK_DOMAIN=$(echo "$NGROK_URL" | sed 's|https\?://||')

echo "Starting ngrok tunnel with domain: $NGROK_DOMAIN"
echo "Tunneling to local port $PROXY_PORT..."

# Start ngrok tunnel
ngrok http --url="$NGROK_DOMAIN" "$PROXY_PORT"