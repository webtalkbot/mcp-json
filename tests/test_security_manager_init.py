#!/usr/bin/env python3
"""
Test for Security Manager initialization fix
"""

import asyncio
import aiohttp
import tempfile
import os
import json
import sys
from pathlib import Path

# Add parent directory to Python path to import modules
current_dir = Path(__file__).parent
parent_dir = current_dir.parent
if str(parent_dir) not in sys.path:
    sys.path.insert(0, str(parent_dir))

from security_manager import SecurityManager, get_security_manager


def test_constructor_no_async_objects():
    """Test that constructor doesn't create async objects immediately"""
    print("🧪 Testing constructor doesn't create async objects...")
    
    manager = SecurityManager()
    
    # Should not have created async objects yet
    assert manager._lock is None, "Lock should be None initially"
    assert manager._session is None, "Session should be None initially"
    assert manager._initialized is False, "Should not be initialized initially"
    
    print("✅ Constructor test passed")


def test_get_security_manager_singleton():
    """Test singleton behavior of get_security_manager"""
    print("🧪 Testing singleton behavior...")
    
    manager1 = get_security_manager()
    manager2 = get_security_manager()
    
    assert manager1 is manager2, "Should return same instance"
    assert manager1._lock is None, "Should not create lock until initialize()"
    
    print("✅ Singleton test passed")


async def test_initialize_creates_async_objects():
    """Test that initialize() properly creates async objects"""
    print("🧪 Testing initialize creates async objects...")
    
    manager = SecurityManager()
    
    async with aiohttp.ClientSession() as session:
        await manager.initialize(session)
        
        # After initialize, async objects should be created
        assert manager._lock is not None, "Lock should be created"
        assert manager._session is session, "Session should be set"
        assert manager._initialized is True, "Should be marked as initialized"
        
    print("✅ Initialize test passed")


async def test_ensure_initialized_before_lock_usage():
    """Test that methods check initialization before using lock"""
    print("🧪 Testing methods check initialization...")
    
    manager = SecurityManager()
    
    # Should raise error when not initialized
    try:
        await manager.get_security_context("test")
        assert False, "Should have raised RuntimeError"
    except RuntimeError as e:
        assert "not initialized" in str(e), f"Wrong error message: {e}"
    
    # refresh_authentication catches exceptions and returns False
    result = await manager.refresh_authentication("test")
    assert result is False, "Should return False when not initialized"
    
    # list_servers_auth_status catches exceptions and returns empty list
    status_list = await manager.list_servers_auth_status()
    assert status_list == [], "Should return empty list when not initialized"
    
    print("✅ Initialization check test passed")


async def test_proper_initialization_flow():
    """Test the complete initialization flow works correctly"""
    print("🧪 Testing complete initialization flow...")
    
    # Create temporary directory structure
    with tempfile.TemporaryDirectory() as temp_dir:
        servers_dir = Path(temp_dir) / "servers" / "test_server"
        servers_dir.mkdir(parents=True)
        
        # Create test security config
        security_file = servers_dir / "test_server_security.json"
        security_config = {
            "provider_type": "none",
            "config": {}
        }
        with open(security_file, 'w') as f:
            json.dump(security_config, f)
        
        manager = SecurityManager(temp_dir)
        
        async with aiohttp.ClientSession() as session:
            # Initialize should work without errors
            await manager.initialize(session)
            
            # Should be able to get security context
            context = await manager.get_security_context("test_server")
            assert context is not None, "Should get security context"
            
            # Should be able to get auth status
            status_list = await manager.list_servers_auth_status()
            assert isinstance(status_list, list), "Should return list"
    
    print("✅ Complete flow test passed")


async def test_multiple_initialize_calls_safe():
    """Test that multiple initialize calls are safe"""
    print("🧪 Testing multiple initialize calls...")
    
    manager = SecurityManager()
    
    async with aiohttp.ClientSession() as session:
        # First initialize
        await manager.initialize(session)
        first_lock = manager._lock
        
        # Second initialize should not break anything
        await manager.initialize(session)
        
        # Lock should still be the same object
        assert manager._lock is first_lock, "Lock should remain the same"
        assert manager._initialized is True, "Should still be initialized"

    print("✅ Multiple initialize test passed")


def test_event_loop_not_required_for_construction():
    """Test that SecurityManager can be created without event loop"""
    print("🧪 Testing construction without event loop...")
    
    # This should work even outside async context
    manager = SecurityManager()
    assert manager._lock is None, "Lock should be None"
    
    # get_security_manager should also work
    singleton_manager = get_security_manager()
    assert singleton_manager._lock is None, "Singleton lock should be None"
    
    print("✅ Construction without event loop test passed")


async def main():
    """Run all tests"""
    print("🚀 Starting Security Manager initialization tests...")
    print()
    
    # Synchronous tests
    test_constructor_no_async_objects()
    test_get_security_manager_singleton()
    test_event_loop_not_required_for_construction()
    
    # Asynchronous tests
    await test_initialize_creates_async_objects()
    await test_ensure_initialized_before_lock_usage()
    await test_proper_initialization_flow()
    await test_multiple_initialize_calls_safe()
    
    print()
    print("🎉 All SecurityManager tests passed!")
    print()
    print("✅ SECURITY MANAGER FIXES VERIFIED:")
    print("   - Constructor doesn't create asyncio objects immediately")
    print("   - Async objects created only in initialize() method") 
    print("   - All methods check initialization before using async objects")
    print("   - Singleton pattern works correctly")
    print("   - Multiple initialize calls are safe")


if __name__ == "__main__":
    asyncio.run(main())
