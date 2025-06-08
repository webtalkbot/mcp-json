#!/usr/bin/env python3
"""
Test script for path parameter functionality
"""

import asyncio
import json
import sys
import os
import aiohttp
from concurrent_mcp_server import ThreadSafeConfigManager, ConcurrentRESTClient

async def test_path_params():
    """Test path parameter substitution"""
    
    # Initialize config manager
    config_manager = ThreadSafeConfigManager(config_dir=".")
    
    # Test loading coingecko server
    config = await config_manager.load_server("coingecko")
    if not config:
        print("❌ Failed to load coingecko server config")
        return False
    
    print("✅ Loaded coingecko server config")
    print(f"   📋 Found {len(config.endpoints)} endpoints")
    
    # Test endpoint with path params
    test_endpoint = "get_coin_details"
    if test_endpoint not in config.endpoints:
        print(f"❌ Endpoint {test_endpoint} not found")
        return False
    
    endpoint = config.endpoints[test_endpoint]
    print(f"\n🔍 Testing endpoint: {test_endpoint}")
    print(f"   URL: {endpoint['url']}")
    print(f"   Path params: {endpoint.get('path_params', {})}")
    print(f"   Query params: {endpoint.get('query_params', {})}")
    
    # Create test HTTP session and client
    async with aiohttp.ClientSession() as session:
        client = ConcurrentRESTClient(session, config_manager)
        
        # Test URL preparation directly
        url = endpoint['url']
        path_params = endpoint.get('path_params', {})
        query_params = endpoint.get('query_params', {})
        user_params = {"id": "ethereum"}  # Override the default bitcoin
        
        final_url, final_params = client._prepare_url_and_params(
            url, path_params, query_params, user_params
        )
        
        print(f"\n🔧 URL Processing Test:")
        print(f"   Original URL: {url}")
        print(f"   Final URL: {final_url}")
        print(f"   Final query params: {final_params}")
        
        # Check if path substitution worked
        if "{id}" in final_url:
            print("❌ FAIL: Path parameter {id} was not substituted!")
            return False
        
        if "ethereum" not in final_url:
            print("❌ FAIL: Expected 'ethereum' not found in URL!")
            return False
        
        print("✅ PASS: Path parameter substitution worked correctly")
        
        # Test with multiple endpoints
        test_cases = [
            ("get_coin_details", {"id": "bitcoin"}),
            ("get_token_price", {"id": "ethereum"}),
            ("get_exchange_details", {"id": "binance"}),
            ("get_companies_holdings", {"coin_id": "bitcoin"})
        ]
        
        print(f"\n🧪 Testing multiple endpoints:")
        for endpoint_name, params in test_cases:
            if endpoint_name in config.endpoints:
                ep_config = config.endpoints[endpoint_name]
                url = ep_config['url']
                path_params = ep_config.get('path_params', {})
                query_params = ep_config.get('query_params', {})
                
                final_url, final_params = client._prepare_url_and_params(
                    url, path_params, query_params, params
                )
                
                has_placeholders = "{" in final_url and "}" in final_url
                print(f"   {endpoint_name}: {'❌ FAIL' if has_placeholders else '✅ PASS'}")
                print(f"      {final_url}")
                
                if has_placeholders:
                    return False
        
        return True

async def main():
    print("🚀 Testing Path Parameter Functionality\n")
    
    success = await test_path_params()
    
    if success:
        print("\n🎉 All tests passed! Path parameter functionality is working correctly.")
    else:
        print("\n💥 Tests failed! There are still issues with path parameter handling.")
        sys.exit(1)

if __name__ == "__main__":
    asyncio.run(main())
