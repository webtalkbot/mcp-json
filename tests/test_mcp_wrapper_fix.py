#!/usr/bin/env python3
"""
Test script to verify MCP notification handling fix via mcp_wrapper.py
This tests the actual architecture used by MCP Inspector
"""

import asyncio
import json
import subprocess
import time
import sys
import os

async def test_mcp_wrapper_fix():
    """Test MCP notification handling via the actual mcp_wrapper.py architecture"""
    
    print("🧪 Testing MCP wrapper notification handling fix...")
    
    try:
        # Start the mcp_wrapper.py HTTP server
        print("🚀 Starting mcp_wrapper.py HTTP server...")
        
        process = subprocess.Popen(
            ['python3', 'mcp_wrapper.py', '--port', '9999'],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            cwd=os.getcwd()
        )
        
        # Give it time to start
        print("⏳ Waiting for HTTP server to start...")
        await asyncio.sleep(3.0)
        
        if process.poll() is not None:
            print(f"❌ mcp_wrapper.py failed to start (exit code: {process.poll()})")
            stderr_output = process.stderr.read()
            if stderr_output:
                print(f"❌ STDERR: {stderr_output}")
            return False
        
        print("✅ HTTP server started, testing MCP endpoint...")
        
        # Test the MCP endpoint that was failing
        import aiohttp
        
        async with aiohttp.ClientSession() as session:
            # Test tools/list endpoint that was causing crashes
            print("📤 Testing tools/list endpoint...")
            
            try:
                async with session.get('http://localhost:9999/mcp/tools/list') as response:
                    if response.status == 200:
                        data = await response.json()
                        tools_count = len(data.get('tools', []))
                        print(f"✅ Tools/list successful - returned {tools_count} tools")
                        
                        # Test a specific server endpoint
                        print("📤 Testing server-specific tools/list...")
                        async with session.get('http://localhost:9999/servers/opensubtitles/mcp/tools/list') as server_response:
                            if server_response.status == 200:
                                server_data = await server_response.json()
                                server_tools_count = len(server_data.get('tools', []))
                                print(f"✅ Server-specific tools/list successful - returned {server_tools_count} tools")
                                return True
                            else:
                                print(f"❌ Server-specific tools/list failed with status {server_response.status}")
                                error_text = await server_response.text()
                                print(f"❌ Error: {error_text}")
                                return False
                    else:
                        print(f"❌ Tools/list failed with status {response.status}")
                        error_text = await response.text()
                        print(f"❌ Error: {error_text}")
                        return False
                        
            except Exception as e:
                print(f"❌ HTTP request failed: {e}")
                return False
        
    except Exception as e:
        print(f"❌ Test failed with exception: {e}")
        return False
    
    finally:
        # Clean shutdown
        if 'process' in locals() and process.poll() is None:
            print("🛑 Shutting down HTTP server...")
            process.terminate()
            try:
                process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait()

async def main():
    """Main test function"""
    print("🔧 Testing MCP notification handling fix via mcp_wrapper.py...")
    
    success = await test_mcp_wrapper_fix()
    
    if success:
        print("\n🎉 SUCCESS: MCP notification fix is working correctly!")
        print("   - HTTP server starts properly")
        print("   - tools/list endpoints work without crashes") 
        print("   - MCP Inspector should now work without server restarts")
        sys.exit(0)
    else:
        print("\n❌ FAILURE: MCP notification fix needs more work")
        sys.exit(1)

if __name__ == "__main__":
    asyncio.run(main())
