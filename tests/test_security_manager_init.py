#!/usr/bin/env python3
"""
test_security_manager_init.py - Test Security Manager initialization fix
"""

import pytest
import asyncio
import aiohttp
from unittest.mock import patch, MagicMock
import tempfile
import os
import json
from pathlib import Path

# Add parent directory to path to import modules
import sys
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from security_manager import SecurityManager, get_security_manager


class TestSecurityManagerInitialization:
    """Test proper initialization order and async object creation"""
    
    def test_constructor_no_async_objects(self):
        """Test that constructor doesn't create async objects immediately"""
        manager = SecurityManager()
        
        # Should not have created async objects yet
        assert manager._lock is None
        assert manager._session is None
        assert manager._initialized is False
        
    def test_get_security_manager_singleton(self):
        """Test singleton behavior of get_security_manager"""
        manager1 = get_security_manager()
        manager2 = get_security_manager()
        
        assert manager1 is manager2
        assert manager1._lock is None  # Should not create lock until initialize()
        
    @pytest.mark.asyncio
    async def test_initialize_creates_async_objects(self):
        """Test that initialize() properly creates async objects"""
        manager = SecurityManager()
        
        async with aiohttp.ClientSession() as session:
            await manager.initialize(session)
            
            # After initialize, async objects should be created
            assert manager._lock is not None
            assert manager._session is session
            assert manager._initialized is True
            
    @pytest.mark.asyncio
    async def test_ensure_initialized_before_lock_usage(self):
        """Test that methods check initialization before using lock"""
        manager = SecurityManager()
        
        # Should raise error when not initialized
        with pytest.raises(RuntimeError, match="SecurityManager is not initialized"):
            await manager.get_security_context("test")
            
        with pytest.raises(RuntimeError, match="SecurityManager is not initialized"):
            await manager.refresh_authentication("test")
            
        with pytest.raises(RuntimeError, match="SecurityManager is not initialized"):
            await manager.list_servers_auth_status()
    
    @pytest.mark.asyncio
    async def test_proper_initialization_flow(self):
        """Test the complete initialization flow works correctly"""
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
                assert context is not None
                
                # Should be able to get auth status
                status_list = await manager.list_servers_auth_status()
                assert isinstance(status_list, list)
    
    @pytest.mark.asyncio 
    async def test_concurrent_initialization_safe(self):
        """Test that concurrent access during initialization is safe"""
        manager = SecurityManager()
        
        async with aiohttp.ClientSession() as session:
            # Initialize and try to access concurrently
            init_task = asyncio.create_task(manager.initialize(session))
            
            # Wait a bit to let initialization start
            await asyncio.sleep(0.01)
            
            # This should either work (if init finished) or raise proper error
            try:
                await manager.get_security_context("test")
            except RuntimeError as e:
                assert "not initialized" in str(e)
            
            # Make sure initialization completes
            await init_task
            
            # Now it should work
            context = await manager.get_security_context("test")
            assert context is not None

    @pytest.mark.asyncio
    async def test_multiple_initialize_calls_safe(self):
        """Test that multiple initialize calls are safe"""
        manager = SecurityManager()
        
        async with aiohttp.ClientSession() as session:
            # First initialize
            await manager.initialize(session)
            first_lock = manager._lock
            
            # Second initialize should not break anything
            await manager.initialize(session)
            
            # Lock should still be the same object
            assert manager._lock is first_lock
            assert manager._initialized is True

    def test_event_loop_not_required_for_construction(self):
        """Test that SecurityManager can be created without event loop"""
        # This should work even outside async context
        manager = SecurityManager()
        assert manager._lock is None
        
        # get_security_manager should also work
        singleton_manager = get_security_manager()
        assert singleton_manager._lock is None


if __name__ == "__main__":
    pytest.main([__file__])
