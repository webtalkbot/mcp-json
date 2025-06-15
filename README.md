# MCP JSON Server

The MCP JSON Server allows you to create and manage MCP servers using JSON configuration files and the MCP Manager. It uses the MCP Proxy Server for network communication, which configures itself automatically.

## Features

- JSON-Based Server Creation: Build any MCP server that uses an API without coding, thanks to a flexible JSON configuration system.
- Claude Desktop Integration: Connect servers to Claude Desktop via stdio (setup instructions below).
- SSE Support: Use servers directly in the Claude.ai web app with Server-Sent Events (SSE). Requires the mcp-proxy from https://github.com/TBXark/mcp-proxy.git.
- Unique Endpoints: Each server has its own PATH with distinct endpoints for easy identification.
- Single-Port Communication: All servers operate through one port for simplicity.
- Concurrent Operation: Run multiple servers at once with concurrent communication to prevent blocking.
- Remote Management: Control servers remotely via API.
- Future Enhancements: Plans for additional functionality beyond tools and security features to address attacks, potentially using KONG.

## Installation

### Step 1: Install MCP Proxy Server

Refer to https://github.com/TBXark/mcp-proxy for detailed instructions. For a quick setup:

#### Install Go (if not installed). 

On macOS, use Homebrew:

```
brew install go
```

#### Configure Go path 

(if you are using zsh)

```
echo 'export PATH="$PATH:/usr/local/go/bin:$HOME/go/bin"' >> ~/.zshrc && source ~/.zshrc
```

(if you are using bash)

```
echo 'export PATH="$PATH:/usr/local/go/bin:$HOME/go/bin"' >> ~/.bashrc && source ~/.bashrc
```

#### Install MCP Proxy Server:

```
go install github.com/TBXark/mcp-proxy@latest
```

### Step 2: Set Up MCP JSON Server

#### Create a Virtual Environment (recommended). Use Miniconda with Python 3.12.4:

```
conda create -n mcp-json python=3.12.4
conda activate mcp-json
````

#### Clone the Repository:

```
git clone https://github.com/<repository-url>
cd mcp-json
```

#### Install the Required Packages:

```
pip install -r requirements.txt
```

#### Configure Environment Variables:

Copy env.example to .env or create a new .env file.

Edit .env with these variables:

```
NGROK_URL="https://your-ngrok-domain.ngrok-free.app"
SERVERS_DIR=./servers
PORT=8999
PROXY_PORT=9000
YOUR_API_KEY="your_api_key_for_api_server"
```

#### Variable Explanations:

- NGROK_URL: The URL for your server. Use an ngrok tunnel for testing (e.g., https://your-ngrok-domain.ngrok-free.app) or your custom domain for production.
- SERVERS_DIR: Directory for JSON server configurations (keep as ./servers).
- PORT: Local port for mcp_wrapper to manage servers.
- PROXY_PORT: Port for stdio-to-SSE communication.
- YOUR_API_KEY: API key for securing server access (used in <servername>_security.json).

Sample JSON configurations for COINGECKO and OPENSUBTITLES are included, with OpenSubtitles supporting secure API key authentication via headers.

### Step 3: Add and Configure Servers

#### Add a Server using the MCP Manager:

Example for Coingecko MCP Server

```
python mcp_manager.py add coingecko concurrent_mcp_server.py --description "Coingecko Server" --transport sse --mode public --auto-start
```

Command Breakdown:

add <server_name>: Specifies the server folder and name.

- concurrent_mcp_server.py: The server script.
- --description: A brief server description.
- --transport sse: Communication protocol (SSE recommended; streamable untested).
- --mode public: Endpoint access mode (public, unrestricted, or admin).
- --auto-start: Automatically starts the server, including after restarts.

### Verify Server Addition:

```
python mcp_manager.py list
```

### Step 4: Start Servers

#### Run the startup script to configure and launch servers and the MCP Proxy:

```
./startup.sh
```

### Step 5: Connect to Claude.ai

To connect a server to Claude.ai via SSE, use this URL format:

https://<your_domain>:<proxy_port>/<server_name>/sse

With your own domain (set in NGROK_URL), combine it with the PROXY_PORT from .env. 

Example: http://mymcpservers.com:9000/opensubtitles/sse

Alternatively, use a reverse proxy (e.g., Nginx or Apache).

#### Using Ngrok for Testing

All info how to install ngrok: https://ngrok.com/docs/getting-started/

Open a new terminal, install ngrok:

```
brew install ngrok 
```

Sign up for an ngrok account. Copy your ngrok authtoken from your ngrok dashboard.

```
ngrok config add-authtoken <TOKEN>
```

Info about ngrok 

Start a tunnel:
```
ngrok http --url=<your_ngrok_domain> 9000
```

Example for pro-kingfish-constantly.ngrok-free.app:

```
ngrok http --url=pro-kingfish-constantly.ngrok-free.app 9000
```

#### Update .env with your ngrok domain in NGROK_URL.

Connect to Claude.ai using:
https://<your_ngrok_domain>/<server_name>/sse

Example for OpenSubtitles:
https://pro-kingfish-constantly.ngrok-free.app/opensubtitles/sse

### Notes
- Ensure the PROXY_PORT in .env matches the port used in ngrok or your custom domain setup.
- For production, replace ngrok with a dedicated domain and secure hosting.
- Security enhancements and additional features are planned for future releases.
- For further assistance, refer to https://github.com/TBXark/mcp-proxy or the repository's issue tracker.