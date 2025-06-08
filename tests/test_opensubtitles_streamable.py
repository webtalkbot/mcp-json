#!/usr/bin/env python3
"""
OpenSubtitles API Test Script using Streamable Transport
Specialized test for OpenSubtitles API via MCP Server
"""

import asyncio
import aiohttp
import json
import time
import sys
import os
from typing import Dict, Any, List, Optional

class OpenSubtitlesStreamableTest:
    """Tester for OpenSubtitles API via Streamable MCP Transport"""
    
    def __init__(self, base_url: str = "http://localhost:8999", server_name: str = "opensubtitles"):
        self.base_url = base_url
        self.server_name = server_name
        self.streamable_url = f"{base_url}/servers/{server_name}/streamable"
        self.session: Optional[aiohttp.ClientSession] = None
        
    async def __aenter__(self):
        timeout = aiohttp.ClientTimeout(total=30)
        self.session = aiohttp.ClientSession(timeout=timeout)
        return self
        
    async def __aexit__(self, exc_type, exc_val, exc_tb):
        if self.session:
            await self.session.close()
    
    async def send_mcp_request(self, method: str, params: Dict[str, Any] = None, request_id: int = 1) -> Dict[str, Any]:
        """Send MCP request via streamable transport"""
        if not self.session:
            raise RuntimeError("Session is not initialized")
            
        request = {
            "jsonrpc": "2.0",
            "id": request_id,
            "method": method
        }
        
        if params:
            request["params"] = params
            
        try:
            async with self.session.post(self.streamable_url, json=request) as resp:
                if resp.status == 200:
                    return await resp.json()
                else:
                    error_text = await resp.text()
                    return {
                        "error": {
                            "code": resp.status,
                            "message": f"HTTP {resp.status}: {error_text}"
                        }
                    }
        except Exception as e:
            return {
                "error": {
                    "code": -1,
                    "message": f"Request failed: {str(e)}"
                }
            }
    
    async def test_environment_setup(self) -> bool:
        """Verify MCP server availability"""
        print("🔧 MCP Server Connection Test")
        print("-" * 50)
        
        # Check server health
        try:
            async with self.session.get(f"{self.base_url}/health") as resp:
                if resp.status == 200:
                    health = await resp.json()
                    print(f"✅ MCP Server: {health.get('status', 'unknown')}")
                    print("✅ Server is available on localhost:8999")
                else:
                    print(f"❌ MCP Server health check failed: {resp.status}")
                    return False
        except Exception as e:
            print(f"❌ Cannot connect to MCP server: {e}")
            print("   Check if running: python concurrent_mcp_server.py")
            return False
            
        return True
    
    async def test_mcp_initialize(self) -> bool:
        """Test MCP initialize"""
        print("\n🚀 MCP Initialize Test")
        print("-" * 50)
        
        response = await self.send_mcp_request("initialize", {
            "protocolVersion": "2024-11-05",
            "capabilities": {},
            "clientInfo": {
                "name": "opensubtitles-test-client",
                "version": "1.0.0"
            }
        })
        
        if "error" in response:
            print(f"❌ Initialize failed: {response['error']['message']}")
            return False
        elif "result" in response:
            print("✅ MCP Initialize successful")
            print(f"📊 Server capabilities: {len(response['result'].get('capabilities', {}))}")
            return True
        else:
            print(f"⚠️ Unexpected response: {response}")
            return False
    
    async def test_list_tools(self) -> List[str]:
        """Test tools/list and return list of available tools"""
        print("\n🔧 Tools List Test")
        print("-" * 50)
        
        response = await self.send_mcp_request("tools/list", request_id=2)
        
        if "error" in response:
            print(f"❌ Tools list failed: {response['error']['message']}")
            return []
        elif "result" in response and "tools" in response["result"]:
            tools = response["result"]["tools"]
            print(f"✅ Available tools: {len(tools)}")
            
            tool_names = []
            for tool in tools:
                name = tool.get("name", "unknown")
                description = tool.get("description", "No description")
                tool_names.append(name)
                
                # Show only OpenSubtitles tools
                if "opensubtitles" in name.lower():
                    print(f"  🎬 {name}: {description}")
            
            return tool_names
        else:
            print(f"⚠️ Unexpected tools response: {response}")
            return []
    
    async def test_get_languages(self) -> List[Dict[str, Any]]:
        """Test get_languages endpoint"""
        print("\n🌍 Get Languages Test")
        print("-" * 50)
        
        response = await self.send_mcp_request("tools/call", {
            "name": "opensubtitles__get_languages",
            "arguments": {}
        }, request_id=3)
        
        if "error" in response:
            print(f"❌ Get languages failed: {response['error']['message']}")
            return []
        elif "result" in response:
            try:
                # Parse response content
                content = response["result"]["content"][0]["text"]
                
                if "✅" in content and "Response:" in content:
                    print("✅ Get languages successful")
                    # Extract languages from response
                    if "```json" in content:
                        json_start = content.find("```json") + 7
                        json_end = content.find("```", json_start)
                        json_str = content[json_start:json_end]
                        
                        try:
                            data = json.loads(json_str)
                            if isinstance(data, dict) and "data" in data:
                                languages = data["data"]
                                print(f"📊 Supported languages: {len(languages)}")
                                
                                # Show some interesting languages
                                interesting = ["sk", "en", "cs", "de", "fr"]
                                for lang_code in interesting:
                                    for lang in languages:
                                        if lang.get("language_code") == lang_code:
                                            print(f"  🗣️ {lang_code.upper()}: {lang.get('language_name', 'Unknown')}")
                                            break
                                
                                return languages
                        except json.JSONDecodeError:
                            print("⚠️ Failed to parse languages from response")
                    else:
                        print("✅ Response received, but without JSON format")
                else:
                    print(f"❌ Unexpected response format: {content[:200]}...")
            except (KeyError, IndexError) as e:
                print(f"⚠️ Error parsing response: {e}")
        
        return []
    
    async def test_search_subtitles(self, query: str = "Avatar", languages: str = "sk,en", page: int = 1) -> List[Dict[str, Any]]:
        """Test search_subtitles endpoint"""
        print(f"\n🔍 Search Subtitles Test: '{query}'")
        print("-" * 50)
        
        response = await self.send_mcp_request("tools/call", {
            "name": "opensubtitles__search_subtitles",
            "arguments": {
                "params": {
                    "query": query,
                    "languages": languages,
                    "page": str(page)
                }
            }
        }, request_id=4)
        
        if "error" in response:
            print(f"❌ Search failed: {response['error']['message']}")
            return []
        elif "result" in response:
            try:
                content = response["result"]["content"][0]["text"]
                
                if "✅" in content and "Response:" in content:
                    print(f"✅ Search successful for '{query}'")
                    
                    # Extract response time
                    if "Response time:" in content:
                        time_line = [line for line in content.split('\n') if 'Response time:' in line][0]
                        print(f"⏱️ {time_line.strip()}")
                    
                    # Try to extract subtitle count
                    if "```json" in content:
                        json_start = content.find("```json") + 7
                        json_end = content.find("```", json_start)
                        json_str = content[json_start:json_end]
                        
                        try:
                            data = json.loads(json_str)
                            if isinstance(data, dict) and "data" in data:
                                subtitles = data["data"]
                                print(f"📊 Found subtitles: {len(subtitles)}")
                                
                                # Show first 3 results
                                for i, subtitle in enumerate(subtitles[:3]):
                                    release_name = subtitle.get("attributes", {}).get("release", "Unknown")
                                    language = subtitle.get("attributes", {}).get("language", "Unknown")
                                    file_id = subtitle.get("attributes", {}).get("files", [{}])[0].get("file_id", "N/A")
                                    
                                    print(f"  📁 {i+1}. {release_name}")
                                    print(f"     🗣️ Language: {language}")
                                    print(f"     🆔 File ID: {file_id}")
                                
                                return subtitles
                        except json.JSONDecodeError:
                            print("⚠️ Failed to parse subtitles from response")
                    else:
                        print("✅ Response received, but without JSON format")
                else:
                    print(f"❌ Search failed or unexpected response")
                    if "❌" in content:
                        # Extract error message
                        error_lines = [line for line in content.split('\n') if '❌' in line]
                        if error_lines:
                            print(f"   Error: {error_lines[0]}")
            except (KeyError, IndexError) as e:
                print(f"⚠️ Error parsing response: {e}")
        
        return []
    
    async def test_download_subtitle(self, file_id: int) -> bool:
        """Test download_subtitle endpoint"""
        print(f"\n📥 Download Subtitle Test: File ID {file_id}")
        print("-" * 50)
        
        response = await self.send_mcp_request("tools/call", {
            "name": "opensubtitles__download_subtitle",
            "arguments": {
                "data": {
                    "file_id": file_id
                }
            }
        }, request_id=5)
        
        if "error" in response:
            print(f"❌ Download failed: {response['error']['message']}")
            return False
        elif "result" in response:
            try:
                content = response["result"]["content"][0]["text"]
                
                if "✅" in content:
                    print(f"✅ Download request successful")
                    
                    # Try to extract download link
                    if "```json" in content:
                        json_start = content.find("```json") + 7
                        json_end = content.find("```", json_start)
                        json_str = content[json_start:json_end]
                        
                        try:
                            data = json.loads(json_str)
                            if isinstance(data, dict) and "link" in data:
                                download_link = data["link"]
                                print(f"🔗 Download link received: {download_link[:50]}...")
                                return True
                        except json.JSONDecodeError:
                            print("⚠️ Failed to parse download response")
                else:
                    print(f"❌ Download failed")
                    if "❌" in content:
                        error_lines = [line for line in content.split('\n') if '❌' in line]
                        if error_lines:
                            print(f"   Error: {error_lines[0]}")
            except (KeyError, IndexError) as e:
                print(f"⚠️ Error parsing response: {e}")
        
        return False
    
    async def test_performance(self, iterations: int = 3) -> Dict[str, float]:
        """Performance test for various endpoints"""
        print(f"\n⚡ Performance Test ({iterations} iterations)")
        print("-" * 50)
        
        results = {}
        
        # Test get_languages performance
        print("Testing get_languages performance...")
        times = []
        for i in range(iterations):
            start_time = time.time()
            await self.test_get_languages()
            elapsed = time.time() - start_time
            times.append(elapsed)
            print(f"  Iteration {i+1}: {elapsed:.2f}s")
        
        results["get_languages"] = {
            "avg": sum(times) / len(times),
            "min": min(times),
            "max": max(times)
        }
        
        # Test search performance
        print("\nTesting search_subtitles performance...")
        times = []
        for i in range(iterations):
            start_time = time.time()
            await self.test_search_subtitles("Matrix", "en")
            elapsed = time.time() - start_time
            times.append(elapsed)
            print(f"  Iteration {i+1}: {elapsed:.2f}s")
        
        results["search_subtitles"] = {
            "avg": sum(times) / len(times),
            "min": min(times),
            "max": max(times)
        }
        
        # Print summary
        print("\n📊 Performance Summary:")
        for endpoint, stats in results.items():
            print(f"  {endpoint}:")
            print(f"    Average: {stats['avg']:.2f}s")
            print(f"    Min: {stats['min']:.2f}s")
            print(f"    Max: {stats['max']:.2f}s")
        
        return results

async def run_comprehensive_test():
    """Run comprehensive OpenSubtitles API test"""
    print("🎬 OpenSubtitles API Streamable Transport Test")
    print("=" * 60)
    
    async with OpenSubtitlesStreamableTest() as tester:
        # Environment setup
        if not await tester.test_environment_setup():
            print("\n❌ Environment setup failed. Terminating test.")
            return
        
        # MCP Initialize
        if not await tester.test_mcp_initialize():
            print("\n❌ MCP initialize failed. Terminating test.")
            return
        
        # List tools
        tools = await tester.test_list_tools()
        if not tools:
            print("\n❌ No tools available. Terminating test.")
            return
        
        # Test individual endpoints
        languages = await tester.test_get_languages()
        
        # Search for subtitles
        subtitles = await tester.test_search_subtitles("Avatar", "sk,en")
        
        # Try to download if we found subtitles
        if subtitles:
            # Get first subtitle's file_id
            try:
                first_subtitle = subtitles[0]
                files = first_subtitle.get("attributes", {}).get("files", [])
                if files:
                    file_id = files[0].get("file_id")
                    if file_id:
                        await tester.test_download_subtitle(file_id)
                    else:
                        print("\n⚠️ No file_id for download test")
                else:
                    print("\n⚠️ No files for download test")
            except (KeyError, IndexError):
                print("\n⚠️ Failed to extract file_id for download test")
        
        # Performance test
        await tester.test_performance(3)
        
        print("\n" + "=" * 60)
        print("✅ OpenSubtitles Streamable Transport Test completed!")
        print("\n🎯 For use in Claude Desktop:")
        print(f"   URL: {tester.streamable_url}")
        print("   Protocol: Streamable HTTP (JSON-per-line)")

async def run_quick_test():
    """Quick test of basic functionality"""
    print("🎬 OpenSubtitles Quick Test")
    print("=" * 40)
    
    async with OpenSubtitlesStreamableTest() as tester:
        if await tester.test_environment_setup():
            if await tester.test_mcp_initialize():
                await tester.test_list_tools()
                await tester.test_get_languages()
                print("\n✅ Quick test completed!")
            else:
                print("\n❌ MCP initialize failed")
        else:
            print("\n❌ Environment setup failed")

if __name__ == "__main__":
    print("🚀 OpenSubtitles Streamable Transport Tester")
    
    # Parse command line arguments
    if len(sys.argv) > 1 and sys.argv[1] == "--quick":
        test_func = run_quick_test()
    else:
        test_func = run_comprehensive_test()
    
    try:
        asyncio.run(test_func)
    except KeyboardInterrupt:
        print("\n⏹️ Test interrupted by user")
    except Exception as e:
        print(f"\n❌ Unexpected error: {e}")
        import traceback
        traceback.print_exc()
        sys.exit(1)
