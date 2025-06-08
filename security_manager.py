#!/usr/bin/env python3
"""
security_manager.py - Main Security Manager for MCP Server
"""

import os
import json
import asyncio
import logging
import aiohttp
from typing import Dict, Any, Optional, List
from pathlib import Path
from security_providers import SecurityProvider, SecurityContext, create_security_provider

logger = logging.getLogger(__name__)

class SecurityManager:
    """Central manager for all security operations"""
    
    def __init__(self, config_dir: str = "."):
        self.config_dir = Path(config_dir)
        self._providers: Dict[str, SecurityProvider] = {}
        self._contexts: Dict[str, SecurityContext] = {}
        self._lock = asyncio.Lock()
        self._session: Optional[aiohttp.ClientSession] = None
        
    async def initialize(self, session: aiohttp.ClientSession):
        """Initialize with HTTP session"""
        try:
            self._session = session
            await self._load_all_security_configs()
        except Exception as e:
            logger.error(f"Error initializing SecurityManager: {e}")
            # Continue even without loaded configs - fallback to 'none' providers
    
    async def _load_all_security_configs(self):
        """Load all security configurations from servers/ directory"""
        servers_dir = self.config_dir / "servers"
        
        if not servers_dir.exists():
            logger.info("No servers/ directory found")
            return
        
        # Search for directories in servers/
        for server_dir in servers_dir.iterdir():
            if server_dir.is_dir():
                server_name = server_dir.name
                security_file = server_dir / f"{server_name}_security.json"
                
                if security_file.exists():
                    try:
                        await self._load_security_config(server_name)
                        logger.info(f"✅ Security config loaded for server: {server_name}")
                    except Exception as e:
                        logger.error(f"❌ Error loading security config for {server_name}: {e}")
                
    async def _load_security_config(self, server_name: str):
        """Load security configuration for specific server from servers/{server_name}/ structure"""
        server_dir = self.config_dir / "servers" / server_name
        security_file = server_dir / f"{server_name}_security.json"
        
        print(f"🔒 DEBUG: Loading security config for {server_name} from {security_file}")
        
        if not security_file.exists():
            # Fallback to "none" provider
            print(f"⚠️ WARNING: No security file for {server_name}, using 'none' provider")
            provider = create_security_provider('none', {})
            async with self._lock:
                self._providers[server_name] = provider
            return
        
        try:
            with open(security_file, 'r', encoding='utf-8') as f:
                config = json.load(f)
            
            provider_type = config.get('provider_type', 'none')
            provider_config = config.get('config', {})
            
            print(f"🔒 DEBUG: {server_name} uses {provider_type} provider with config: {provider_config}")
            
            # Create provider
            provider = create_security_provider(provider_type, provider_config)
            
            async with self._lock:
                self._providers[server_name] = provider
                
        except Exception as e:
            print(f"❌ ERROR: Error parsing security config for {server_name}: {e}")
            # Fallback to "none" provider
            provider = create_security_provider('none', {})
            async with self._lock:
                self._providers[server_name] = provider
    
    async def get_security_context(self, server_name: str) -> SecurityContext:
        """Get valid security context for server"""
        if not self._session:
            raise RuntimeError("SecurityManager is not initialized")
        
        async with self._lock:
            if server_name not in self._providers:
                try:
                    await self._load_security_config(server_name)
                except Exception as e:
                    logger.error(f"Error loading security config for {server_name}: {e}")
                    # Fallback to 'none' provider
                    provider = create_security_provider('none', {})
                    self._providers[server_name] = provider
            
            provider = self._providers.get(server_name)
            if not provider:
                logger.warning(f"Security provider for server {server_name} is not available, using fallback")
                # Fallback to 'none' provider
                provider = create_security_provider('none', {})
                self._providers[server_name] = provider
            
            # Return cached context if valid
            cached_context = self._contexts.get(server_name)
            if cached_context and not cached_context.is_expired():
                return cached_context
            
            # Create new context with error handling
            try:
                new_context = await provider.authenticate(self._session)
                self._contexts[server_name] = new_context
                return new_context
            except Exception as e:
                logger.error(f"Authentication failed for {server_name}: {e}")
                # Fallback - if authentication fails, use 'none' provider
                if provider.get_provider_type() != 'none':
                    logger.warning(f"Fallback to 'none' provider for {server_name}")
                    fallback_provider = create_security_provider('none', {})
                    self._providers[server_name] = fallback_provider
                    try:
                        fallback_context = await fallback_provider.authenticate(self._session)
                        self._contexts[server_name] = fallback_context
                        return fallback_context
                    except Exception as fallback_error:
                        logger.error(f"Fallback authentication also failed for {server_name}: {fallback_error}")
                        # Last resort - create empty context
                        from security_providers import SecurityContext
                        empty_context = SecurityContext({}, {}, {})
                        self._contexts[server_name] = empty_context
                        return empty_context
                else:
                    # If 'none' provider fails, create empty context
                    from security_providers import SecurityContext
                    empty_context = SecurityContext({}, {}, {})
                    self._contexts[server_name] = empty_context
                    return empty_context
    
    async def test_authentication(self, server_name: str) -> bool:
        """Test authentication for server"""
        try:
            context = await self.get_security_context(server_name)
            provider = self._providers.get(server_name)
            
            if not provider:
                logger.warning(f"Provider for {server_name} does not exist in test_authentication")
                return False
            
            try:
                return await provider.test_authentication(self._session, context)
            except Exception as test_error:
                logger.error(f"Test authentication failed for {server_name}: {test_error}")
                # Fallback - if test fails, try to refresh context
                try:
                    await self.refresh_authentication(server_name)
                    # Try test again with new context
                    new_context = await self.get_security_context(server_name)
                    return await provider.test_authentication(self._session, new_context)
                except Exception as retry_error:
                    logger.error(f"Retry test authentication also failed for {server_name}: {retry_error}")
                    return False
                    
        except Exception as e:
            logger.error(f"❌ ERROR: Authentication test failed for {server_name}: {e}")
            return False

    async def refresh_authentication(self, server_name: str) -> bool:
        """Force refresh authentication for server"""
        try:
            async with self._lock:
                # Clear cached context
                if server_name in self._contexts:
                    del self._contexts[server_name]
            
            # Get new context
            await self.get_security_context(server_name)
            return True
        except Exception as e:
            print(f"❌ ERROR: Authentication refresh failed for {server_name}: {e}")
            return False

    async def list_servers_auth_status(self) -> List[Dict[str, Any]]:
        """Return authentication status for all servers"""
        try:
            async with self._lock:
                results = []
                
                for server_name, provider in self._providers.items():
                    try:
                        context = self._contexts.get(server_name)
                        
                        status = {
                            'server_name': server_name,
                            'provider_type': 'unknown',
                            'authenticated': False,
                            'last_auth_time': None,
                            'error': None
                        }
                        
                        # Safe provider type extraction
                        try:
                            status['provider_type'] = provider.get_provider_type()
                        except Exception as e:
                            logger.error(f"Error getting provider type for {server_name}: {e}")
                            status['error'] = f"Provider error: {str(e)}"
                        
                        # Safe context validation
                        try:
                            if context:
                                status['authenticated'] = not context.is_expired()
                                if hasattr(context, 'auth_data') and context.auth_data:
                                    if context.auth_data.get('created_at'):
                                        status['last_auth_time'] = context.auth_data['created_at']
                                
                                if context.is_expired():
                                    status['error'] = 'Authentication expired'
                        except Exception as e:
                            logger.error(f"Error processing context for {server_name}: {e}")
                            status['authenticated'] = False
                            status['error'] = f"Context error: {str(e)}"
                        
                        results.append(status)
                        
                    except Exception as e:
                        logger.error(f"Error processing server {server_name}: {e}")
                        # Add at least basic status even on error
                        results.append({
                            'server_name': server_name,
                            'provider_type': 'error',
                            'authenticated': False,
                            'last_auth_time': None,
                            'error': f"Processing error: {str(e)}"
                        })
                
                return results
                
        except Exception as e:
            logger.error(f"Critical error in list_servers_auth_status: {e}")
            return []  # Fallback to empty list

    def get_available_providers(self) -> List[str]:
        """Return list of available security providers"""
        from security_providers import SECURITY_PROVIDERS
        return list(SECURITY_PROVIDERS.keys())

    def get_provider_config_template(self, provider_type: str) -> Dict[str, Any]:
        """Return configuration template for provider type"""
        templates = {
            'none': {
                'provider_type': 'none'
            },
            'custom_headers': {
                'provider_type': 'custom_headers',
                'config': {
                    'headers': {
                        'Api-Key': '${OPENSUBTITLES_API_KEY}',
                        'User-Agent': 'MCP-Client/1.0',
                        'Content-Type': 'application/json'
                    },
                    'test_url': 'https://api.example.com/test'
                }
            }
        }
        
        return templates.get(provider_type, {})
    
    def apply_security_to_request(self, server_name: str, context: SecurityContext,
                                headers: Dict[str, str], params: Dict[str, str]) -> Dict[str, Any]:
        """Apply security context to HTTP request"""
        final_headers = {**headers}
        final_headers.update(context.headers)
        
        final_params = {**params}
        final_params.update(context.query_params)
        
        return {
            'headers': final_headers,
            'params': final_params,
            'cookies': context.cookies
        }
    

# Global security manager instance
security_manager: Optional[SecurityManager] = None

def get_security_manager(config_dir: str = ".") -> SecurityManager:
    """Singleton factory for SecurityManager"""
    global security_manager
    if security_manager is None:
        security_manager = SecurityManager(config_dir)
    return security_manager