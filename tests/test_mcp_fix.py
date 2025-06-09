#!/usr/bin/env python3
"""
Test script to verify MCP notification handling fix
"""

import asyncio
import json
import subprocess
import time
import sys
import os

async def test_mcp_notification_fix():
    """Test that MCP server properly handles notifications/initialized"""
    
    print("🧪 Testing MCP notification handling fix...")
    
    try:
        # Start the concurrent_mcp_server process
        print("🚀 Starting concurrent_mcp_server...")
        
        process = subprocess.Popen(
            ['python3', 'concurrent_mcp_server.py'],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            cwd=os.getcwd()
        )
        
        # Give it time to start
        await asyncio.sleep(1.0)
        
        # Send initialize request
        print("📤 Sending initialize request...")
        init_request = {
            "jsonrpc": "2.0",
            "id": 0,
            "method": "initialize",
            "params": {
                "protocolVersion": "2024-11-05",
                "capabilities": {
                    "tools": {},
                    "resources": {},
                    "prompts": {},
                    "logging": {}
                },
                "clientInfo": {
                    "name": "test-client",
                    "version": "1.0.0"
                }
            }
        }
        
        process.stdin.write(json.dumps(init_request) + '\n')
        process.stdin.flush()
        
        # Wait for initialize response
        print("⏳ Waiting for initialize response...")
        response_received = False
        
        # Read any debug output first
        print("📋 Reading any startup output...")
        for _ in range(3):
            try:
                # Try to read any startup output that's not JSON
                import select
                if select.select([process.stdout], [], [], 0.1)[0]:
                    line = process.stdout.readline()
                    if line and not line.strip().startswith('{'):
                        print(f"📋 Server debug: {line.strip()}")
            except:
                pass
        
        for attempt in range(20):  # Wait up to 10 seconds
            if process.poll() is not None:
                print(f"❌ Process terminated unexpectedly (exit code: {process.poll()})")
                # Read any error output
                stderr_output = process.stderr.read()
                if stderr_output:
                    print(f"❌ STDERR: {stderr_output}")
                return False
                
            try:
                # Non-blocking read
                import select
                if select.select([process.stdout], [], [], 0.1)[0]:
                    line = process.stdout.readline()
                    if line:
                        line = line.strip()
                        print(f"📨 Server output: {line}")
                        
                        if line.startswith('{'):
                            try:
                                data = json.loads(line)
                                print(f"📨 Parsed JSON: {data}")
                                if data.get("id") == 0 and "result" in data:
                                    print("✅ Initialize response received")
                                    response_received = True
                                    break
                            except json.JSONDecodeError as e:
                                print(f"❌ JSON decode error: {e}")
                                print(f"❌ Raw line: {repr(line)}")
            except Exception as e:
                print(f"❌ Error reading stdout: {e}")
            
            await asyncio.sleep(0.5)
        
        if not response_received:
            print("❌ No initialize response received")
            process.terminate()
            return False
        
        # Send notifications/initialized (this was causing the crash)
        print("📤 Sending notifications/initialized...")
        initialized_notification = {
            "jsonrpc": "2.0",
            "method": "notifications/initialized"
            # NO 'id' field for notifications!
        }
        
        process.stdin.write(json.dumps(initialized_notification) + '\n')
        process.stdin.flush()
        
        # Wait a bit to see if server crashes
        print("⏳ Checking if server handles notification correctly...")
        await asyncio.sleep(2.0)
        
        if process.poll() is not None:
            print("❌ Server crashed after notifications/initialized")
            return False
        
        # Send tools/list request (this was the original failing operation)
        print("📤 Sending tools/list request...")
        tools_request = {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "tools/list"
        }
        
        process.stdin.write(json.dumps(tools_request) + '\n')
        process.stdin.flush()
        
        # Wait for tools/list response
        print("⏳ Waiting for tools/list response...")
        tools_response_received = False
        for _ in range(10):  # Wait up to 5 seconds
            if process.poll() is not None:
                print(f"❌ Process terminated during tools/list")
                return False
                
            line = process.stdout.readline()
            if line:
                try:
                    data = json.loads(line.strip())
                    if data.get("id") == 1 and ("result" in data or "error" in data):
                        print("✅ Tools/list response received")
                        if "result" in data:
                            tools_count = len(data["result"].get("tools", []))
                            print(f"📊 Server returned {tools_count} tools")
                        tools_response_received = True
                        break
                except json.JSONDecodeError:
                    pass
            
            await asyncio.sleep(0.5)
        
        if not tools_response_received:
            print("❌ No tools/list response received")
            process.terminate()
            return False
        
        # Clean shutdown
        print("🛑 Shutting down server...")
        process.terminate()
        try:
            process.wait(timeout=5)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait()
        
        print("✅ Test passed! MCP notification handling fix is working")
        return True
        
    except Exception as e:
        print(f"❌ Test failed with exception: {e}")
        if 'process' in locals():
            try:
                process.terminate()
                process.wait(timeout=2)
            except:
                process.kill()
        return False

async def main():
    """Main test function"""
    print("🔧 Testing MCP notification handling fix...")
    
    success = await test_mcp_notification_fix()
    
    if success:
        print("\n🎉 SUCCESS: MCP notification fix is working correctly!")
        print("   - Server properly handles notifications/initialized")
        print("   - tools/list calls work without crashes")
        sys.exit(0)
    else:
        print("\n❌ FAILURE: MCP notification fix needs more work")
        sys.exit(1)

if __name__ == "__main__":
    asyncio.run(main())
