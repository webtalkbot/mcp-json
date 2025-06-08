#!/usr/bin/env python3
"""
security_providers.py - Universal security providers for MCP Server
"""

import abc
import os
import json
import logging
import aiohttp
import asyncio
from typing import Dict, Any, Optional
from dataclasses import dataclass
from datetime import datetime, timedelta

logger = logging.getLogger(__name__)

@dataclass
class SecurityContext:
    """Context for security operations"""
    headers: Dict[str, str]
    query_params: Dict[str, str]
    cookies: Dict[str, str]
    auth_data: Dict[str, Any]
    expires_at: Optional[datetime] = None
    
    def is_expired(self) -> bool:
        """Check if security context has expired"""
        if self.expires_at is None:
            return False
        return datetime.now() >= self.expires_at

class SecurityProvider(abc.ABC):
    """Base class for security providers"""
    
    def __init__(self, config: Dict[str, Any]):
        self.config = config
    
    def get_provider_type(self) -> str:
        """Return provider type"""
        return self.__class__.__name__.lower().replace('provider', '')
    
    @abc.abstractmethod
    async def authenticate(self, session: aiohttp.ClientSession) -> SecurityContext:
        """Perform authentication and return security context"""
        pass
    
    @abc.abstractmethod
    async def test_authentication(self, session: aiohttp.ClientSession,
                                context: SecurityContext) -> bool:
        """Test if authentication is functional"""
        pass

class NoneProvider(SecurityProvider):
    """Provider for APIs without authentication"""
    
    async def authenticate(self, session: aiohttp.ClientSession) -> SecurityContext:
        return SecurityContext(headers={}, query_params={}, cookies={}, auth_data={'provider': 'none'})
    
    async def test_authentication(self, session: aiohttp.ClientSession, context: SecurityContext) -> bool:
        return True

class CustomHeadersProvider(SecurityProvider):
    """Provider for custom headers with environment variable support"""
    
    async def authenticate(self, session: aiohttp.ClientSession) -> SecurityContext:
        headers_config = self.config.get('headers', {})
        resolved_headers = {}
        
        for header_name, header_value in headers_config.items():
            # If value starts with ${}, it's an environment variable
            if isinstance(header_value, str) and header_value.startswith('${') and header_value.endswith('}'):
                env_key = header_value[2:-1]  # Remove ${ and }
                env_value = os.getenv(env_key)
                if env_value:
                    resolved_headers[header_name] = env_value
                else:
                    logger.warning(f"Environment variable {env_key} not found for header {header_name}")
            else:
                resolved_headers[header_name] = str(header_value)
        
        return SecurityContext(
            headers=resolved_headers,
            query_params={},
            cookies={},
            auth_data={'provider': 'custom_headers', 'header_count': len(resolved_headers)}
        )
    
    async def test_authentication(self, session: aiohttp.ClientSession, context: SecurityContext) -> bool:
        test_url = self.config.get('test_url')
        if not test_url:
            return True
        
        try:
            async with session.get(test_url, headers=context.headers, timeout=10) as response:
                return response.status in [200, 201, 202]
        except Exception as e:
            logger.error(f"Custom headers test failed: {e}")
            return False

# Registry of all available providers
SECURITY_PROVIDERS = {
    'none': NoneProvider,
    'custom_headers': CustomHeadersProvider
}

def create_security_provider(provider_type: str, config: Dict[str, Any]) -> SecurityProvider:
    """Factory function for creating security provider"""
    provider_class = SECURITY_PROVIDERS.get(provider_type)
    if not provider_class:
        available = list(SECURITY_PROVIDERS.keys())
        raise ValueError(f"Unknown security provider: {provider_type}. Available: {available}")
    
    return provider_class(config)