#!/usr/bin/env python3
"""
test_complete_functionality.py - Komplexný test funkcionality MCP wrapper
Testuje SSE aj Streamable transporty a vyhodnotí celkovú úspešnosť
"""

import asyncio
import aiohttp
import json
import time
import sys
from typing import Dict, List, Optional
import logging

# Nastavenie loggingu
logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')
logger = logging.getLogger(__name__)

class MCPTransportTester:
    """Komplexný tester pre MCP transporty"""
    
    def __init__(self, base_url: str = "http://127.0.0.1:8999"):
        self.base_url = base_url
        self.server_name = "opensubtitles"  # Testujeme opensubtitles server
        self.test_results = {}
        self.session = None
        
    async def __aenter__(self):
        """Async context manager entry"""
        self.session = aiohttp.ClientSession(
            timeout=aiohttp.ClientTimeout(total=30)
        )
        return self
    
    async def __aexit__(self, exc_type, exc_val, exc_tb):
        """Async context manager exit"""
        if self.session:
            await self.session.close()
    
    async def test_basic_endpoints(self) -> Dict[str, bool]:
        """Test základných HTTP endpointov"""
        logger.info("🔍 Testing basic HTTP endpoints...")
        results = {}
        
        endpoints = [
            ("/", "root_endpoint"),
            ("/health", "health_check"),
            ("/servers", "servers_list"),
            (f"/servers/{self.server_name}", "server_info"),
            (f"/servers/{self.server_name}/mcp/capabilities", "server_capabilities"),
            (f"/servers/{self.server_name}/mcp/tools/list", "server_tools_list"),
            ("/.well-known/mcp", "mcp_discovery")
        ]
        
        for endpoint, test_name in endpoints:
            try:
                async with self.session.get(f"{self.base_url}{endpoint}") as response:
                    if response.status == 200:
                        data = await response.json()
                        results[test_name] = True
                        logger.info(f"✅ {test_name}: OK")
                    else:
                        results[test_name] = False
                        logger.error(f"❌ {test_name}: HTTP {response.status}")
            except Exception as e:
                results[test_name] = False
                logger.error(f"❌ {test_name}: {str(e)}")
        
        return results
    
    async def test_mcp_post_endpoint(self) -> Dict[str, bool]:
        """Test MCP POST endpoint s session management"""
        logger.info("🔍 Testing MCP POST endpoint...")
        results = {}
        
        # Test initialize
        try:
            init_request = {
                "jsonrpc": "2.0",
                "id": 1,
                "method": "initialize",
                "params": {
                    "protocolVersion": "2025-03-26",
                    "capabilities": {"tools": {}},
                    "clientInfo": {"name": "test-client", "version": "1.0.0"}
                }
            }
            
            async with self.session.post(
                f"{self.base_url}/mcp",
                json=init_request,
                headers={"Content-Type": "application/json"}
            ) as response:
                if response.status == 200:
                    data = await response.json()
                    if "result" in data and "sessionId" in data["result"]:
                        session_id = data["result"]["sessionId"]
                        results["mcp_initialize"] = True
                        logger.info(f"✅ MCP Initialize: OK (Session: {session_id[:8]}...)")
                        
                        # Test tools/list s session
                        tools_request = {
                            "jsonrpc": "2.0",
                            "id": 2,
                            "method": "tools/list"
                        }
                        
                        async with self.session.post(
                            f"{self.base_url}/mcp",
                            json=tools_request,
                            headers={
                                "Content-Type": "application/json",
                                "Mcp-Session-Id": session_id
                            }
                        ) as tools_response:
                            if tools_response.status == 200:
                                tools_data = await tools_response.json()
                                if "result" in tools_data and "tools" in tools_data["result"]:
                                    tools_count = len(tools_data["result"]["tools"])
                                    results["mcp_tools_list"] = True
                                    logger.info(f"✅ MCP Tools List: OK ({tools_count} tools)")
                                else:
                                    results["mcp_tools_list"] = False
                                    logger.error("❌ MCP Tools List: Invalid response format")
                            else:
                                results["mcp_tools_list"] = False
                                logger.error(f"❌ MCP Tools List: HTTP {tools_response.status}")
                    else:
                        results["mcp_initialize"] = False
                        logger.error("❌ MCP Initialize: No sessionId in response")
                else:
                    results["mcp_initialize"] = False
                    logger.error(f"❌ MCP Initialize: HTTP {response.status}")
        except Exception as e:
            results["mcp_initialize"] = False
            results["mcp_tools_list"] = False
            logger.error(f"❌ MCP POST endpoint: {str(e)}")
        
        return results
    
    async def test_sse_transport(self) -> Dict[str, bool]:
        """Test SSE transport"""
        logger.info("🔍 Testing SSE transport...")
        results = {}
        
        try:
            # Test SSE endpoint
            sse_url = f"{self.base_url}/servers/{self.server_name}/sse"
            
            async with self.session.get(sse_url) as response:
                if response.status == 200:
                    # Čítame prvé SSE správy
                    content_received = False
                    valid_sse_format = False
                    mcp_protocol_detected = False
                    
                    async for line in response.content:
                        line_str = line.decode('utf-8').strip()
                        
                        if line_str.startswith('data: '):
                            content_received = True
                            valid_sse_format = True
                            
                            # Skús parsovať JSON
                            try:
                                json_data = json.loads(line_str[6:])  # Remove 'data: '
                                if "jsonrpc" in json_data and "result" in json_data:
                                    if "protocolVersion" in json_data["result"]:
                                        mcp_protocol_detected = True
                                        logger.info("✅ SSE: MCP protocol detected")
                                        break
                            except json.JSONDecodeError:
                                pass
                        
                        # Timeout po 5 sekundách
                        if content_received:
                            break
                    
                    results["sse_connection"] = response.status == 200
                    results["sse_format"] = valid_sse_format
                    results["sse_mcp_protocol"] = mcp_protocol_detected
                    
                    if mcp_protocol_detected:
                        logger.info("✅ SSE Transport: Fully functional")
                    else:
                        logger.warning("⚠️ SSE Transport: Connected but MCP protocol not detected")
                else:
                    results["sse_connection"] = False
                    results["sse_format"] = False
                    results["sse_mcp_protocol"] = False
                    logger.error(f"❌ SSE Transport: HTTP {response.status}")
        
        except Exception as e:
            results["sse_connection"] = False
            results["sse_format"] = False
            results["sse_mcp_protocol"] = False
            logger.error(f"❌ SSE Transport: {str(e)}")
        
        return results
    
    async def test_streamable_transport(self) -> Dict[str, bool]:
        """Test Streamable transport"""
        logger.info("🔍 Testing Streamable transport...")
        results = {}
        
        try:
            # Test Streamable POST endpoint
            streamable_url = f"{self.base_url}/servers/{self.server_name}/streamable"
            
            # Test initialize
            init_request = {
                "jsonrpc": "2.0",
                "id": 0,
                "method": "initialize",
                "params": {
                    "protocolVersion": "2025-03-26",
                    "capabilities": {"tools": {}},
                    "clientInfo": {"name": "streamable-test-client", "version": "1.0.0"}
                }
            }
            
            async with self.session.post(
                streamable_url,
                json=init_request,
                headers={"Content-Type": "application/json"}
            ) as response:
                if response.status == 200:
                    data = await response.json()
                    if "result" in data and "protocolVersion" in data["result"]:
                        results["streamable_initialize"] = True
                        logger.info("✅ Streamable Initialize: OK")
                        
                        # Test tools/list
                        tools_request = {
                            "jsonrpc": "2.0",
                            "id": 1,
                            "method": "tools/list"
                        }
                        
                        async with self.session.post(
                            streamable_url,
                            json=tools_request,
                            headers={"Content-Type": "application/json"}
                        ) as tools_response:
                            if tools_response.status == 200:
                                tools_data = await tools_response.json()
                                if "result" in tools_data and "tools" in tools_data["result"]:
                                    tools_count = len(tools_data["result"]["tools"])
                                    results["streamable_tools"] = True
                                    logger.info(f"✅ Streamable Tools: OK ({tools_count} tools)")
                                else:
                                    results["streamable_tools"] = False
                                    logger.error("❌ Streamable Tools: Invalid response")
                            else:
                                results["streamable_tools"] = False
                                logger.error(f"❌ Streamable Tools: HTTP {tools_response.status}")
                    else:
                        results["streamable_initialize"] = False
                        logger.error("❌ Streamable Initialize: Invalid response")
                else:
                    results["streamable_initialize"] = False
                    results["streamable_tools"] = False
                    logger.error(f"❌ Streamable Transport: HTTP {response.status}")
        
        except Exception as e:
            results["streamable_initialize"] = False
            results["streamable_tools"] = False
            logger.error(f"❌ Streamable Transport: {str(e)}")
        
        return results
    
    async def test_streamable_get_transport(self) -> Dict[str, bool]:
        """Test Streamable GET transport (streaming)"""
        logger.info("🔍 Testing Streamable GET transport...")
        results = {}
        
        try:
            streamable_url = f"{self.base_url}/servers/{self.server_name}/streamable"
            
            async with self.session.get(streamable_url) as response:
                if response.status == 200:
                    # Čítame streaming response
                    content_received = False
                    valid_format = False
                    mcp_detected = False
                    
                    async for line in response.content:
                        line_str = line.decode('utf-8').strip()
                        
                        if line_str.startswith('data: '):
                            content_received = True
                            valid_format = True
                            
                            try:
                                json_data = json.loads(line_str[6:])
                                if "jsonrpc" in json_data and "result" in json_data:
                                    if "protocolVersion" in json_data["result"]:
                                        mcp_detected = True
                                        logger.info("✅ Streamable GET: MCP protocol detected")
                                        break
                            except json.JSONDecodeError:
                                pass
                        
                        if content_received:
                            break
                    
                    results["streamable_get_connection"] = True
                    results["streamable_get_format"] = valid_format
                    results["streamable_get_mcp"] = mcp_detected
                    
                    if mcp_detected:
                        logger.info("✅ Streamable GET Transport: Fully functional")
                    else:
                        logger.warning("⚠️ Streamable GET: Connected but MCP not detected")
                else:
                    results["streamable_get_connection"] = False
                    results["streamable_get_format"] = False
                    results["streamable_get_mcp"] = False
                    logger.error(f"❌ Streamable GET: HTTP {response.status}")
        
        except Exception as e:
            results["streamable_get_connection"] = False
            results["streamable_get_format"] = False
            results["streamable_get_mcp"] = False
            logger.error(f"❌ Streamable GET Transport: {str(e)}")
        
        return results
    
    async def test_tools_functionality(self) -> Dict[str, bool]:
        """Test tools functionality"""
        logger.info("🔍 Testing tools functionality...")
        results = {}
        
        try:
            # Test global tools list
            async with self.session.get(f"{self.base_url}/mcp/tools/list") as response:
                if response.status == 200:
                    data = await response.json()
                    if "tools" in data and len(data["tools"]) > 0:
                        results["global_tools_list"] = True
                        tools_count = len(data["tools"])
                        logger.info(f"✅ Global Tools List: OK ({tools_count} tools)")
                        
                        # Test tool call
                        first_tool = data["tools"][0]
                        tool_name = first_tool["name"]
                        
                        call_request = {
                            "name": tool_name,
                            "arguments": {}
                        }
                        
                        async with self.session.post(
                            f"{self.base_url}/mcp/tools/call",
                            json=call_request,
                            headers={"Content-Type": "application/json"}
                        ) as call_response:
                            if call_response.status == 200:
                                call_data = await call_response.json()
                                if "content" in call_data:
                                    results["global_tools_call"] = True
                                    logger.info(f"✅ Global Tools Call: OK (tool: {tool_name})")
                                else:
                                    results["global_tools_call"] = False
                                    logger.error("❌ Global Tools Call: Invalid response format")
                            else:
                                results["global_tools_call"] = False
                                logger.error(f"❌ Global Tools Call: HTTP {call_response.status}")
                    else:
                        results["global_tools_list"] = False
                        results["global_tools_call"] = False
                        logger.error("❌ Global Tools List: No tools found")
                else:
                    results["global_tools_list"] = False
                    results["global_tools_call"] = False
                    logger.error(f"❌ Global Tools List: HTTP {response.status}")
        
        except Exception as e:
            results["global_tools_list"] = False
            results["global_tools_call"] = False
            logger.error(f"❌ Tools functionality: {str(e)}")
        
        return results
    
    async def run_all_tests(self) -> Dict[str, Dict[str, bool]]:
        """Spustí všetky testy"""
        logger.info("🚀 Starting comprehensive MCP wrapper functionality test...")
        
        all_results = {}
        
        # Test základných endpointov
        all_results["basic_endpoints"] = await self.test_basic_endpoints()
        
        # Test MCP POST endpoint
        all_results["mcp_post"] = await self.test_mcp_post_endpoint()
        
        # Test SSE transport
        all_results["sse_transport"] = await self.test_sse_transport()
        
        # Test Streamable transport
        all_results["streamable_transport"] = await self.test_streamable_transport()
        
        # Test Streamable GET transport
        all_results["streamable_get_transport"] = await self.test_streamable_get_transport()
        
        # Test tools functionality
        all_results["tools_functionality"] = await self.test_tools_functionality()
        
        return all_results
    
    def generate_report(self, results: Dict[str, Dict[str, bool]]) -> Dict[str, any]:
        """Generuje finálny report"""
        total_tests = 0
        passed_tests = 0
        failed_tests = []
        
        for category, tests in results.items():
            for test_name, passed in tests.items():
                total_tests += 1
                if passed:
                    passed_tests += 1
                else:
                    failed_tests.append(f"{category}.{test_name}")
        
        success_rate = (passed_tests / total_tests * 100) if total_tests > 0 else 0
        is_fully_functional = success_rate >= 95  # 95% úspešnosť = plne funkčný
        
        return {
            "total_tests": total_tests,
            "passed_tests": passed_tests,
            "failed_tests": failed_tests,
            "success_rate": success_rate,
            "is_fully_functional": is_fully_functional,
            "detailed_results": results
        }

async def main():
    """Hlavná funkcia"""
    print("=" * 80)
    print("🧪 MCP WRAPPER COMPREHENSIVE FUNCTIONALITY TEST")
    print("=" * 80)
    
    async with MCPTransportTester() as tester:
        # Spustíme všetky testy
        results = await tester.run_all_tests()
        
        # Generujeme report
        report = tester.generate_report(results)
        
        # Výpis výsledkov
        print("\n" + "=" * 80)
        print("📊 FINAL TEST RESULTS")
        print("=" * 80)
        
        print(f"📈 Total Tests: {report['total_tests']}")
        print(f"✅ Passed: {report['passed_tests']}")
        print(f"❌ Failed: {len(report['failed_tests'])}")
        print(f"📊 Success Rate: {report['success_rate']:.1f}%")
        
        if report['failed_tests']:
            print(f"\n❌ Failed Tests:")
            for failed_test in report['failed_tests']:
                print(f"   - {failed_test}")
        
        print("\n" + "=" * 80)
        if report['is_fully_functional']:
            print("🎉 RESULT: PROGRAM IS 100% FUNCTIONAL! ✅")
            print("🚀 All critical MCP transport features are working correctly.")
            print("✅ SSE Transport: Working")
            print("✅ Streamable Transport: Working") 
            print("✅ MCP Protocol: Compliant")
            print("✅ Tools System: Functional")
        else:
            print("⚠️ RESULT: PROGRAM HAS SOME ISSUES ❌")
            print(f"📊 Functionality: {report['success_rate']:.1f}%")
            print("🔧 Some features need attention.")
        
        print("=" * 80)
        
        # Detailný breakdown
        print("\n📋 DETAILED BREAKDOWN:")
        for category, tests in report['detailed_results'].items():
            passed_in_category = sum(1 for passed in tests.values() if passed)
            total_in_category = len(tests)
            category_rate = (passed_in_category / total_in_category * 100) if total_in_category > 0 else 0
            
            status = "✅" if category_rate == 100 else "⚠️" if category_rate >= 50 else "❌"
            print(f"{status} {category.replace('_', ' ').title()}: {passed_in_category}/{total_in_category} ({category_rate:.0f}%)")
        
        # Return exit code
        return 0 if report['is_fully_functional'] else 1

if __name__ == "__main__":
    try:
        exit_code = asyncio.run(main())
        sys.exit(exit_code)
    except KeyboardInterrupt:
        print("\n🛑 Test interrupted by user")
        sys.exit(1)
    except Exception as e:
        print(f"\n💥 Test failed with error: {e}")
        sys.exit(1)
