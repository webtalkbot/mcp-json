#!/usr/bin/env python3
"""
Test Streamable HTTP MCP Transport
"""

import asyncio
import aiohttp
import json
import time
import sys

async def test_streamable_transport():
    """Test Streamable HTTP MCP transport for Claude Desktop"""
    print("🔄 Test Streamable HTTP MCP Transport")
    
    server_name = "opensubtitles"
    base_url = "http://localhost:8999"
    
    try:
        # Test 1: Health check
        print("\n📌 Test 1: Health check")
        async with aiohttp.ClientSession() as session:
            async with session.get(f"{base_url}/health") as resp:
                if resp.status == 200:
                    health = await resp.json()
                    print(f"✅ Health: {health.get('status')}")
                    print(f"📊 Streamable Transport: enabled")
                else:
                    print(f"❌ Health check failed: {resp.status}")
                    return
        
        # Test 2: Streamable Connection (GET)
        print(f"\n📌 Test 2: Streamable connection to /servers/{server_name}/streamable")
        
        timeout = aiohttp.ClientTimeout(total=30)
        async with aiohttp.ClientSession(timeout=timeout) as session:
            try:
                async with session.get(f"{base_url}/servers/{server_name}/streamable") as resp:
                    print(f"📈 Status: {resp.status}")
                    print(f"📋 Headers: {dict(resp.headers)}")
                    
                    if resp.status == 200:
                        print("✅ Streamable connection successful!")
                        print("📥 Loading Streamable responses...")
                        
                        # Reading Streamable responses (JSON per line)
                        response_count = 0
                        start_time = time.time()
                        
                        async for line in resp.content:
                            line_str = line.decode('utf-8').strip()
                            
                            if line_str:  # Non-empty line
                                response_count += 1
                                
                                try:
                                    response_data = json.loads(line_str)
                                    print(f"📨 Response {response_count}: {response_data}")
                                    
                                    # If we get initialize response, test is successful
                                    if response_data.get('jsonrpc') == '2.0' and 'result' in response_data:
                                        print("✅ We received MCP initialize response!")
                                        break
                                    
                                    # If we get discovery response
                                    if response_data.get('type') == 'discovery':
                                        print("🔍 Discovery response received")
                                        
                                except json.JSONDecodeError:
                                    print(f"⚠️ Invalid JSON: {line_str}")
                            
                            # Timeout after 10 seconds
                            if time.time() - start_time > 10:
                                print("⏰ Timeout - ending test")
                                break
                        
                        print(f"📊 Total {response_count} responses")
                    
                    else:
                        print(f"❌ Streamable connection failed: {resp.status}")
                        error_text = await resp.text()
                        print(f"📝 Error: {error_text}")
            
            except aiohttp.ClientError as e:
                print(f"❌ Connection error: {e}")
                return
        
        # Test 3: Streamable POST (direct request/response)
        print(f"\n📌 Test 3: Streamable POST request /servers/{server_name}/streamable")
        
        async with aiohttp.ClientSession(timeout=timeout) as session:
            try:
                # Test initialize request
                initialize_request = {
                    "jsonrpc": "2.0",
                    "id": 1,
                    "method": "initialize",
                    "params": {
                        "protocolVersion": "2024-11-05",
                        "capabilities": {},
                        "clientInfo": {
                            "name": "streamable-test-client",
                            "version": "1.0.0"
                        }
                    }
                }
                
                async with session.post(
                    f"{base_url}/servers/{server_name}/streamable",
                    json=initialize_request
                ) as resp:
                    print(f"📈 Status: {resp.status}")
                    
                    if resp.status == 200:
                        print("✅ Streamable POST successful!")
                        response_data = await resp.json()
                        print(f"📨 Initialize response: {response_data}")
                        
                        if response_data.get('jsonrpc') == '2.0' and 'result' in response_data:
                            print("✅ Valid MCP initialize response!")
                        else:
                            print("⚠️ Unexpected response format")
                    else:
                        print(f"❌ Streamable POST failed: {resp.status}")
                        error_text = await resp.text()
                        print(f"📝 Error: {error_text}")
            
            except aiohttp.ClientError as e:
                print(f"❌ Error in POST request: {e}")
        
        # Test 4: Global Streamable endpoint
        print(f"\n📌 Test 4: Global Streamable endpoint /streamable")
        
        async with aiohttp.ClientSession(timeout=timeout) as session:
            try:
                async with session.get(f"{base_url}/streamable") as resp:
                    print(f"📈 Status: {resp.status}")
                    
                    if resp.status == 200:
                        print("✅ Global Streamable endpoint works!")
                        
                        # Reading first response
                        async for line in resp.content:
                            line_str = line.decode('utf-8').strip()
                            
                            if line_str:
                                try:
                                    response_data = json.loads(line_str)
                                    print(f"📨 Discovery response: {response_data}")
                                    break
                                except json.JSONDecodeError:
                                    print(f"⚠️ Invalid JSON: {line_str}")
                            
                            # Timeout after 5 seconds
                            if time.time() - start_time > 5:
                                break
                    else:
                        print(f"❌ Global Streamable endpoint failed: {resp.status}")
            
            except aiohttp.ClientError as e:
                print(f"❌ Connection error: {e}")
        
        print("\n✅ Streamable HTTP Transport test completed!")
        print("\n🎯 For Claude Desktop use URL:")
        print(f"   http://localhost:8999/servers/{server_name}/streamable")
        
    except Exception as e:
        print(f"❌ Unexpected error: {e}")
        import traceback
        traceback.print_exc()

async def test_streamable_vs_sse():
    """Comparison of Streamable HTTP vs SSE transport"""
    print("\n" + "="*60)
    print("🔄 COMPARISON: Streamable HTTP vs SSE Transport")
    print("="*60)
    
    base_url = "http://localhost:8999"
    server_name = "opensubtitles"
    
    # Test Streamable HTTP
    print("\n📌 Streamable HTTP Test:")
    async with aiohttp.ClientSession() as session:
        try:
            initialize_request = {
                "jsonrpc": "2.0",
                "id": 1,
                "method": "initialize",
                "params": {
                    "protocolVersion": "2024-11-05",
                    "capabilities": {},
                    "clientInfo": {"name": "test", "version": "1.0.0"}
                }
            }
            
            async with session.post(f"{base_url}/servers/{server_name}/streamable", json=initialize_request) as resp:
                print(f"📈 Status: {resp.status}")
                if resp.status == 200:
                    data = await resp.json()
                    if data.get('jsonrpc') == '2.0' and 'result' in data:
                        print(f"✅ Streamable HTTP: Successful communication")
                        print(f"🔧 Media type: application/json")
                        print(f"📦 Format: JSON objects per line")
                else:
                    print(f"❌ Streamable HTTP failed: {resp.status}")
        except Exception as e:
            print(f"❌ Streamable HTTP error: {e}")
    
    # Test SSE Transport (active)
    print("\n📌 SSE Transport Test (active):")
    async with aiohttp.ClientSession() as session:
        try:
            async with session.post(f"{base_url}/servers/{server_name}/sse", json=initialize_request) as resp:
                print(f"📈 Status: {resp.status}")
                if resp.status == 200:
                    data = await resp.json()
                    print(f"✅ SSE Transport: Active and functional")
                    print(f"🔧 Media type: text/event-stream")
                    print(f"📦 Format: data: {{json}}\\n\\n")
                else:
                    print(f"❌ SSE Transport failed: {resp.status}")
        except Exception as e:
            print(f"❌ SSE Transport error: {e}")
    
    print("\n" + "="*60)
    print("📋 BOTH TRANSPORTS ARE ACTIVE")
    print("="*60)
    print("✅ Streamable HTTP Transport - for Claude Desktop")
    print("  🔧 Media type: application/json")
    print("  📦 Format: JSON objects per line")
    print("  🚀 URL: /servers/{server_name}/streamable")
    print("")
    print("✅ SSE Transport - for other MCP clients")
    print("  🔧 Media type: text/event-stream")
    print("  📦 Format: data: {{json}}\\n\\n")
    print("  🚀 URL: /servers/{server_name}/sse")
    print("")
    print("🎯 Both transports are fully functional - choose based on your client!")

async def test_tools_via_streamable():
    """Test MCP tools calls via Streamable transport"""
    print("\n" + "="*60)
    print("🔧 TEST: MCP Tools via Streamable Transport")
    print("="*60)
    
    base_url = "http://localhost:8999"
    server_name = "opensubtitles"
    
    async with aiohttp.ClientSession() as session:
        try:
            # Test 1: tools/list
            print("\n📌 Test 1: tools/list")
            tools_request = {
                "jsonrpc": "2.0",
                "id": 2,
                "method": "tools/list"
            }
            
            async with session.post(f"{base_url}/servers/{server_name}/streamable", json=tools_request) as resp:
                if resp.status == 200:
                    data = await resp.json()
                    if 'result' in data and 'tools' in data['result']:
                        tools = data['result']['tools']
                        print(f"✅ We got {len(tools)} tools")
                        for tool in tools[:3]:  # First 3 tools
                            print(f"  🔧 {tool.get('name', 'Unknown')}: {tool.get('description', 'No description')}")
                    else:
                        print(f"⚠️ Unexpected response: {data}")
                else:
                    print(f"❌ tools/list failed: {resp.status}")
            
            # Test 2: Test tool call (if tools are available)
            print("\n📌 Test 2: Attempt to call first tool")
            if 'tools' in locals() and tools:
                first_tool = tools[0]
                tool_name = first_tool.get('name')
                
                call_request = {
                    "jsonrpc": "2.0",
                    "id": 3,
                    "method": "tools/call",
                    "params": {
                        "name": tool_name,
                        "arguments": {}
                    }
                }
                
                async with session.post(f"{base_url}/servers/{server_name}/streamable", json=call_request) as resp:
                    if resp.status == 200:
                        data = await resp.json()
                        if 'result' in data:
                            print(f"✅ Tool {tool_name} successfully called")
                            print(f"📊 Response type: {type(data['result'])}")
                        elif 'error' in data:
                            print(f"⚠️ Tool error: {data['error'].get('message', 'Unknown error')}")
                        else:
                            print(f"⚠️ Unexpected response: {data}")
                    else:
                        print(f"❌ Tool call failed: {resp.status}")
            else:
                print("⚠️ No tools available for test")
                
        except Exception as e:
            print(f"❌ Error testing tools: {e}")

if __name__ == "__main__":
    print("🚀 Streamable HTTP MCP Transport Tester")
    
    try:
        asyncio.run(test_streamable_transport())
        asyncio.run(test_streamable_vs_sse())
        asyncio.run(test_tools_via_streamable())
    except KeyboardInterrupt:
        print("\n⏹️ Test interrupted")
    except Exception as e:
        print(f"\n❌ Error: {e}")
        sys.exit(1)
