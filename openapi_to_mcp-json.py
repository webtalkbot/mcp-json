#!/usr/bin/env python3
"""
- Automatic OpenAPI 3.0 to MCP Format Converter
Converts an OpenAPI specification to MCP {server_name}_endpoints.json and {server_name}_security.json
Automatically uses SERVERS_DIR from the .env file
"""

import json
import yaml
import os
import argparse
import requests
import tempfile
import sys
from typing import Dict, Any, List, Optional, Union
from pathlib import Path
import re
from urllib.parse import urlparse
from tqdm import tqdm

# Load environment variables
try:
    from dotenv import load_dotenv

    load_dotenv()
except ImportError:
    print("⚠️  Warning: dotenv is not installed, environment variables will not be loaded from .env")


class OpenAPIToMCPConverter:
    """Automatic OpenAPI 3.0 to MCP Format Converter"""

    def __init__(self):
        self.openapi_doc = None
        self.server_name = None
        self.base_url = None
        self.servers_dir = self._get_servers_dir()

    def _get_servers_dir(self) -> str:
        """Automatically retrieves the servers directory from .env or uses a default"""
        servers_dir = os.getenv('SERVERS_DIR', './servers')

        # Normalize path
        if servers_dir.startswith('./'):
            servers_dir = servers_dir[2:]
        elif servers_dir.startswith('/'):
            pass  # Absolute path
        else:
            servers_dir = './' + servers_dir

        return servers_dir

    def is_url(self, path: str) -> bool:
        """Check if the input is a URL"""
        return path.startswith(('http://', 'https://'))

    def download_from_url(self, url: str) -> str:
        """Download OpenAPI specification from URL and return local file path"""
        print(f"🌐 Downloading from URL: {url}")
        
        try:
            headers = {
                'User-Agent': 'OpenAPI-to-MCP-Converter/1.0',
                'Accept': 'application/json, application/yaml, text/yaml, text/plain, */*'
            }
            
            response = requests.get(url, headers=headers, timeout=30)
            response.raise_for_status()
            
            # Determine file extension based on content type or URL
            content_type = response.headers.get('content-type', '').lower()
            
            if 'json' in content_type or url.endswith('.json'):
                extension = '.json'
            elif 'yaml' in content_type or url.endswith(('.yaml', '.yml')):
                extension = '.yaml'
            else:
                # Try to auto-detect from content
                content = response.text.strip()
                extension = '.json' if content.startswith('{') else '.yaml'
            
            # Create temporary file
            with tempfile.NamedTemporaryFile(mode='w', suffix=extension, delete=False, encoding='utf-8') as tmp_file:
                tmp_file.write(response.text)
                temp_path = tmp_file.name
            
            print(f"   ✅ Downloaded successfully ({len(response.text)} characters)")
            return temp_path
            
        except requests.RequestException as e:
            raise Exception(f"Failed to download from URL: {e}")
        except Exception as e:
            raise Exception(f"Error processing URL: {e}")

    def load_openapi_file(self, file_path: str) -> Dict[str, Any]:
        """Loads an OpenAPI file (JSON or YAML) with automatic detection"""
        try:
            with open(file_path, 'r', encoding='utf-8') as f:
                content = f.read().strip()

                # Automatic format detection
                if content.startswith('{') or file_path.lower().endswith('.json'):
                    return json.loads(content)
                else:
                    return yaml.safe_load(content)

        except yaml.YAMLError as e:
            raise Exception(f"Invalid YAML format in {file_path}: {e}")
        except json.JSONDecodeError as e:
            raise Exception(f"Invalid JSON format in {file_path}: {e}")
        except Exception as e:
            raise Exception(f"Error loading OpenAPI file: {e}")

    def _validate_openapi_schema(self, openapi_doc: Dict[str, Any]) -> bool:
        """Validates basic OpenAPI schema structure"""
        required_fields = ['openapi', 'info', 'paths']
        for field in required_fields:
            if field not in openapi_doc:
                raise ValueError(f"Missing required OpenAPI field: {field}")
        
        info = openapi_doc.get('info', {})
        if 'title' not in info:
            raise ValueError("Missing required field: info.title")
        
        return True

    def _auto_detect_server_name(self, openapi_doc: Dict[str, Any], file_path: str) -> str:
        """Automatically detects the server name from the OpenAPI document or file"""
        # 1. Attempt from info.title
        title = openapi_doc.get('info', {}).get('title', '')
        if title:
            name = re.sub(r'[^a-zA-Z0-9_\-]', '_', title.lower())
            name = re.sub(r'_+', '_', name).strip('_')
            if name and len(name) > 2:
                return name

        # 2. Attempt from file name
        file_name = Path(file_path).stem
        if file_name and file_name not in ['openapi', 'swagger', 'api', 'spec']:
            name = re.sub(r'[^a-zA-Z0-9_\-]', '_', file_name.lower())
            name = re.sub(r'_+', '_', name).strip('_')
            return name

        # 3. Attempt from base URL
        base_url = self.extract_base_url(openapi_doc)
        if base_url:
            parsed = urlparse(base_url)
            domain_parts = parsed.netloc.split('.')
            if domain_parts:
                # Example: api.github.com -> github
                if len(domain_parts) >= 2:
                    name = domain_parts[-2]  # github from api.github.com
                else:
                    name = domain_parts[0]

                name = re.sub(r'[^a-zA-Z0-9_\-]', '_', name.lower())
                if name and len(name) > 2:
                    return name

        # 4. Default fallback
        return "api_server"

    def extract_base_url(self, openapi_doc: Dict[str, Any]) -> Optional[str]:
        """Automatically extracts the base URL from the servers section"""
        servers = openapi_doc.get('servers', [])
        if servers and len(servers) > 0:
            url = servers[0].get('url', '').rstrip('/')
            # Expand relative URLs
            if url.startswith('/'):
                return f"https://api.example.com{url}"
            return url
        return None

    def _auto_detect_auth_env_var(self, server_name: str, scheme_config: Dict[str, Any]) -> str:
        """Automatically detects the environment variable name for authentication"""
        scheme_type = scheme_config.get('type', '')
        scheme_name = scheme_config.get('name', 'api_key')

        # Attempts to create env var name
        server_upper = server_name.upper().replace('-', '_')

        if scheme_type == 'apiKey':
            # API_KEY variations
            possible_names = [
                f"{server_upper}_API_KEY",
                f"{server_upper}_KEY",
                f"{server_upper}_TOKEN",
                f"{scheme_name}".upper().replace('-', '_')
            ]
        elif scheme_type == 'http':
            scheme = scheme_config.get('scheme', 'basic')
            if scheme == 'bearer':
                possible_names = [
                    f"{server_upper}_BEARER_TOKEN",
                    f"{server_upper}_TOKEN",
                    f"{server_upper}_ACCESS_TOKEN"
                ]
            else:
                possible_names = [
                    f"{server_upper}_AUTH",
                    f"{server_upper}_BASIC_AUTH"
                ]
        else:
            possible_names = [f"{server_upper}_TOKEN"]

        # Return the first (most logical)
        return possible_names[0]

    def extract_security_info(self, openapi_doc: Dict[str, Any]) -> Dict[str, Any]:
        """Automatically extracts security information"""
        security_schemes = openapi_doc.get('components', {}).get('securitySchemes', {})

        headers = {}
        auth_type = "none"

        # Analyze all security schemes
        for scheme_name, scheme_config in security_schemes.items():
            scheme_type = scheme_config.get('type', '')

            if scheme_type == 'apiKey':
                auth_type = "custom_headers"
                in_location = scheme_config.get('in', 'header')
                key_name = scheme_config.get('name', 'X-API-Key')

                if in_location == 'header':
                    env_var_name = self._auto_detect_auth_env_var(self.server_name, scheme_config)
                    headers[key_name] = f"${{{env_var_name}}}"
                elif in_location == 'query':
                    # For query parameter API keys - add to headers as fallback
                    env_var_name = self._auto_detect_auth_env_var(self.server_name, scheme_config)
                    headers[f"X-{key_name}"] = f"${{{env_var_name}}}"

            elif scheme_type == 'http':
                auth_type = "custom_headers"
                scheme = scheme_config.get('scheme', 'basic')

                if scheme == 'bearer':
                    env_var_name = self._auto_detect_auth_env_var(self.server_name, scheme_config)
                    headers['Authorization'] = f"Bearer ${{{env_var_name}}}"
                elif scheme == 'basic':
                    env_var_name = self._auto_detect_auth_env_var(self.server_name, scheme_config)
                    headers['Authorization'] = f"Basic ${{{env_var_name}}}"

            elif scheme_type == 'oauth2':
                auth_type = "custom_headers"
                env_var_name = self._auto_detect_auth_env_var(self.server_name, scheme_config)
                headers['Authorization'] = f"Bearer ${{{env_var_name}}}"

        # Add standard headers
        user_agent = f"MCP-{self.server_name.replace('_', '-').title()}/1.0"
        headers["User-Agent"] = user_agent

        # If Accept header is not defined, add it
        if "Accept" not in headers:
            headers["Accept"] = "application/json"

        # Content-Type for POST/PUT/PATCH operations
        if "Content-Type" not in headers:
            headers["Content-Type"] = "application/json"

        # Create final security configuration
        if auth_type == "none":
            return {"provider_type": "none"}
        else:
            config = {
                "provider_type": "custom_headers",
                "config": {
                    "headers": headers
                }
            }

            # Add test_url if available
            if self.base_url:
                config["config"]["test_url"] = f"{self.base_url}/health"

            return config

    def _smart_parameter_extraction(self, parameters: List[Dict], operation_params: List[Dict] = None) -> tuple:
        """Intelligent parameter extraction with automatic type detection"""
        path_params = {}
        query_params = {}

        # Combine parameters
        all_params = (parameters or []) + (operation_params or [])

        for param in all_params:
            param_name = param.get('name', '')
            param_in = param.get('in', '')
            param_required = param.get('required', False)
            schema = param.get('schema', {})

            # Intelligent determination of default value
            default_value = self._get_smart_default_value(param, schema)

            if param_in == 'path':
                # Path parameters are always required and use placeholders
                path_params[param_name] = f"{{{param_name}}}"

            elif param_in == 'query':
                # Query parameters - if required or no default, use placeholder
                if param_required or not default_value:
                    query_params[param_name] = f"{{{param_name}}}"
                else:
                    query_params[param_name] = str(default_value)

        return path_params, query_params

    def _get_smart_default_value(self, param: Dict[str, Any], schema: Dict[str, Any]) -> Any:
        """Intelligent determination of default value for a parameter"""
        # 1. Explicit default from schema
        if 'default' in schema:
            return schema['default']

        # 2. Example from parameter
        if 'example' in param:
            return param['example']

        # 3. First example from examples
        if 'examples' in param and param['examples']:
            first_example = list(param['examples'].values())[0]
            if isinstance(first_example, dict) and 'value' in first_example:
                return first_example['value']

        # 4. Enum first value
        if 'enum' in schema and schema['enum']:
            return schema['enum'][0]

        # 5. Type-based defaults
        schema_type = schema.get('type', 'string')
        if schema_type == 'integer':
            return schema.get('minimum', 1)
        elif schema_type == 'number':
            return schema.get('minimum', 1.0)
        elif schema_type == 'boolean':
            return False
        elif schema_type == 'array':
            return []

        # 6. None for string or others
        return None

    def _smart_request_body_extraction(self, request_body: Dict[str, Any]) -> Optional[str]:
        """Intelligent request body extraction with automatic detection"""
        if not request_body:
            return None

        content = request_body.get('content', {})

        # Priority content types
        priority_types = [
            'application/json',
            'application/x-www-form-urlencoded',
            'multipart/form-data',
            'text/plain',
            'application/xml'
        ]

        # Find the most suitable content type
        selected_type = None
        for content_type in priority_types:
            if content_type in content:
                selected_type = content_type
                break

        # If not in priority, use the first available
        if not selected_type and content:
            selected_type = list(content.keys())[0]

        if not selected_type:
            return None

        schema = content[selected_type].get('schema', {})
        return self._create_smart_body_template(schema, selected_type)

    def _create_smart_body_template(self, schema: Dict[str, Any], content_type: str) -> str:
        """Creates an intelligent body template"""
        if content_type == 'application/json':
            return self._create_json_template(schema)
        elif content_type == 'application/x-www-form-urlencoded':
            return self._create_form_template(schema)
        elif content_type == 'multipart/form-data':
            return self._create_multipart_template(schema)
        else:
            # Fallback to JSON
            return self._create_json_template(schema)

    def _create_json_template(self, schema: Dict[str, Any]) -> str:
        """Creates a JSON template from schema with intelligent typing"""
        
        def process_schema(sch: Dict[str, Any], level: int = 0, prop_name: str = None) -> Any:
            if level > 3:  # Protection against infinite recursion
                return "{...}"

            sch_type = sch.get('type', 'object')

            if sch_type == 'object':
                properties = sch.get('properties', {})
                result = {}
                for current_prop_name, prop_schema in properties.items():
                    result[current_prop_name] = process_schema(prop_schema, level + 1, current_prop_name)
                return result

            elif sch_type == 'array':
                items_schema = sch.get('items', {'type': 'string'})
                return [process_schema(items_schema, level + 1, prop_name)]

            elif sch_type in ['integer', 'number']:
                return f"{{{prop_name or 'number'}}}"

            elif sch_type == 'boolean':
                return f"{{{prop_name or 'boolean'}}}"

            else:  # string and others
                if 'enum' in sch:
                    return f"{{{prop_name or 'enum_value'}}}"
                return f"{{{prop_name or 'string'}}}"

        template_obj = process_schema(schema)
        return json.dumps(template_obj, ensure_ascii=False, indent=None)

    def _create_form_template(self, schema: Dict[str, Any]) -> str:
        """Creates a form-encoded template"""
        properties = schema.get('properties', {})
        if not properties:
            return "{form_data}"

        # For form data, create a JSON object
        template_obj = {}
        for prop_name in properties.keys():
            template_obj[prop_name] = f"{{{prop_name}}}"

        return json.dumps(template_obj, ensure_ascii=False)

    def _create_multipart_template(self, schema: Dict[str, Any]) -> str:
        """Creates a multipart template"""
        return self._create_form_template(schema)  # Similar to form

    def _smart_endpoint_naming(self, path: str, method: str, operation: Dict[str, Any]) -> str:
        """Intelligent endpoint naming"""
        # 1. Use operationId if it exists and is reasonable
        operation_id = operation.get('operationId', '')
        if operation_id and len(operation_id) > 2:
            name = re.sub(r'[^a-zA-Z0-9_]', '_', operation_id.lower())
            name = re.sub(r'_+', '_', name).strip('_')
            return name

        # 2. Intelligent creation from path and method
        method_lower = method.lower()

        # Clean path
        clean_path = path.strip('/')
        if not clean_path:
            return f"{method_lower}_root"

        # Replace path parameters
        clean_path = re.sub(r'\{[^}]+\}', 'by_id', clean_path)

        # Replace special characters
        clean_path = re.sub(r'[^a-zA-Z0-9_/]', '_', clean_path)
        clean_path = clean_path.replace('/', '_').strip('_')
        clean_path = re.sub(r'_+', '_', clean_path)

        # Specific mapping for common patterns
        if method_lower == 'get' and 'by_id' in clean_path:
            name = clean_path.replace('by_id', 'details')
        elif method_lower == 'get':
            name = f"get_{clean_path}" if clean_path else "list_all"
        elif method_lower == 'post':
            name = f"create_{clean_path}" if clean_path else "create"
        elif method_lower == 'put':
            name = f"update_{clean_path}" if clean_path else "update"
        elif method_lower == 'patch':
            name = f"patch_{clean_path}" if clean_path else "patch"
        elif method_lower == 'delete':
            name = f"delete_{clean_path}" if clean_path else "delete"
        else:
            name = f"{method_lower}_{clean_path}" if clean_path else method_lower

        return name

    def convert_openapi_to_mcp(self, openapi_doc: Dict[str, Any], server_name: str = None) -> tuple:
        """Main automatic conversion function"""
        self.openapi_doc = openapi_doc

        # Automatic server name detection if not provided
        if not server_name:
            server_name = self._auto_detect_server_name(openapi_doc, "")

        self.server_name = server_name
        self.base_url = self.extract_base_url(openapi_doc)

        print(f"🔍 Detected settings:")
        print(f"   📝 Server name: {self.server_name}")
        print(f"   🌐 Base URL: {self.base_url or 'Not defined'}")
        print(f"   📁 Output dir: {self.servers_dir}")

        # Extract paths
        paths = openapi_doc.get('paths', {})
        endpoints = {}

        print(f"🔄 Processing endpoints...")

        total_operations = sum(len([m for m in path_item.keys() if m in ['get', 'post', 'put', 'patch', 'delete', 'head', 'options', 'trace']]) 
                              for path_item in paths.values())

        with tqdm(total=total_operations, desc="Processing endpoints") as pbar:
            for path, path_item in paths.items():
                # Path-level parameters and configuration
                path_parameters = path_item.get('parameters', [])

                # Iterate through all supported HTTP methods
                http_methods = ['get', 'post', 'put', 'patch', 'delete', 'head', 'options', 'trace']

                for method in http_methods:
                    if method not in path_item:
                        continue

                    operation = path_item[method]

                    # Intelligent endpoint naming
                    endpoint_name = self._smart_endpoint_naming(path, method, operation)

                    # Prevent duplicates
                    original_name = endpoint_name
                    counter = 1
                    while endpoint_name in endpoints:
                        endpoint_name = f"{original_name}_{counter}"
                        counter += 1

                    # Extract parameters
                    operation_params = operation.get('parameters', [])
                    path_params, query_params = self._smart_parameter_extraction(path_parameters, operation_params)

                    # Create complete URL
                    full_url = self._build_full_url(path, self.base_url)

                    # Base endpoint object
                    endpoint = {
                        "method": method.upper(),
                        "url": full_url,
                        "description": self._get_endpoint_description(operation, method, path)
                    }

                    # Add parameters
                    if path_params:
                        endpoint["path_params"] = path_params

                    if query_params:
                        endpoint["query_params"] = query_params

                    # Extract request body for methods that support it
                    if method.upper() in ['POST', 'PUT', 'PATCH']:
                        request_body = operation.get('requestBody')
                        if request_body:
                            body_template = self._smart_request_body_extraction(request_body)
                            if body_template:
                                endpoint["body_template"] = body_template

                    # Automatic timeout determination based on operation
                    endpoint["timeout"] = self._determine_timeout(operation, method)

                    endpoints[endpoint_name] = endpoint
                    pbar.update(1)
                    # print(f"   ✅ {method.upper():<6} {endpoint_name:<25} → {path}") # Removed for progress bar

        # Extract security information
        print(f"🔒 Processing security configuration...")
        security_config = self.extract_security_info(openapi_doc)

        return endpoints, security_config

    def _build_full_url(self, path: str, base_url: str) -> str:
        """Creates a complete URL with intelligent path handling"""
        if not base_url:
            base_url = "https://api.example.com"

        # Normalize path
        if not path.startswith('/'):
            path = '/' + path

        # Remove trailing slash from base_url
        base_url = base_url.rstrip('/')

        return f"{base_url}{path}"

    def _get_endpoint_description(self, operation: Dict[str, Any], method: str, path: str) -> str:
        """Intelligent creation of endpoint description"""
        # Priority: description > summary > automatic description
        description = operation.get('description', '').strip()
        summary = operation.get('summary', '').strip()

        if description:
            return description
        elif summary:
            return summary
        else:
            # Automatic description based on method and path
            method_descriptions = {
                'get': 'Retrieve',
                'post': 'Create',
                'put': 'Update',
                'patch': 'Partially update',
                'delete': 'Delete',
                'head': 'Get headers for',
                'options': 'Get options for'
            }

            base_desc = method_descriptions.get(method.lower(), method.upper())
            path_clean = path.strip('/').replace('/', ' ').replace('{', '').replace('}', '')

            if path_clean:
                return f"{base_desc} {path_clean}"
            else:
                return f"{base_desc} resource"

    def _determine_timeout(self, operation: Dict[str, Any], method: str) -> int:
        """Automatic timeout determination based on operation type"""
        # Longer timeout for POST/PUT (data upload)
        if method.upper() in ['POST', 'PUT', 'PATCH']:
            return 60
        # Shorter for GET
        elif method.upper() == 'GET':
            return 30
        # Standard for others
        else:
            return 30

    def save_mcp_files(self, server_name: str, endpoints: Dict[str, Any], security_config: Dict[str, Any]) -> str:
        """Saves MCP files to the correct directory"""
        # Create server directory according to SERVERS_DIR
        server_dir = Path(self.servers_dir) / server_name
        server_dir.mkdir(parents=True, exist_ok=True)

        print(f"💾 Saving MCP files...")

        # Save endpoints file
        endpoints_file = server_dir / f"{server_name}_endpoints.json"
        with open(endpoints_file, 'w', encoding='utf-8') as f:
            json.dump(endpoints, f, indent=2, ensure_ascii=False)

        # Save security file
        security_file = server_dir / f"{server_name}_security.json"
        with open(security_file, 'w', encoding='utf-8') as f:
            json.dump(security_config, f, indent=2, ensure_ascii=False)

        print(f"✅ MCP files created:")
        print(f"   📁 {server_dir}")
        print(f"   📄 {endpoints_file.name}")
        print(f"   📄 {security_file.name}")

        # Automatic .env template creation
        self._create_env_template(server_name, security_config, server_dir)

        # Automatic command usage display
        self._show_usage_instructions(server_name)

        return str(server_dir)

    def _create_env_template(self, server_name: str, security_config: Dict[str, Any], server_dir: Path):
        """Automatically creates an .env template"""
        env_vars = []

        if security_config.get("provider_type") == "custom_headers":
            headers = security_config.get("config", {}).get("headers", {})
            for header_name, header_value in headers.items():
                if isinstance(header_value, str) and header_value.startswith('${') and header_value.endswith('}'):
                    env_var = header_value[2:-1]  # Remove ${ and }
                    env_vars.append(env_var)

        if env_vars:
            env_template_file = server_dir / f"{server_name}_env_template.txt"
            with open(env_template_file, 'w', encoding='utf-8') as f:
                f.write(f"# Environment variables for {server_name} MCP server\n")
                f.write(f"# Add these variables to your .env file:\n\n")
                for env_var in sorted(set(env_vars)):
                    f.write(f"{env_var}=your_actual_value_here\n")

                f.write(f"\n# Example for {server_name}:\n")
                if env_vars and env_vars[0] and "API_KEY" in env_vars[0]:
                    f.write(f"# {env_vars[0]}=abc123xyz789\n")

            print(f"   📄 {env_template_file.name} (environment variables template)")

            # Warning about required env vars
            print(f"\n💡 Required environment variables:")
            for env_var in sorted(set(env_vars)):
                print(f"   {env_var}=your_actual_value")

    def _show_usage_instructions(self, server_name: str):
        """Displays automatic usage instructions"""
        print(f"\n🚀 Automatic usage:")
        print(f"   python mcp_manager.py add {server_name} concurrent_mcp_server.py \\")
        print(f"       --description 'Converted from OpenAPI' \\")
        print(f"       --transport sse --mode public")

        print(f"\n🔧 To start:")
        print(f"   python mcp_manager.py start {server_name}")

        print(f"\n📊 Status:")
        print(f"   python mcp_manager.py status {server_name}")


def check_dependencies():
    """Check if required dependencies are installed"""
    missing = []
    
    try:
        import yaml
    except ImportError:
        missing.append("PyYAML")
    
    try:
        from tqdm import tqdm
    except ImportError:
        missing.append("tqdm")
        
    try:
        import requests
    except ImportError:
        missing.append("requests")
    
    if missing:
        print("❌ Missing required dependencies:")
        for dep in missing:
            print(f"   pip install {dep}")
        sys.exit(1)

def main():
    check_dependencies()
    parser = argparse.ArgumentParser(
        description="Automatic OpenAPI 3.0 to MCP Format Converter",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Automatic features:
  • Loads SERVERS_DIR from the .env file
  • Automatic server name detection from OpenAPI document
  • Intelligent mapping of security schemes
  • Automatic creation of environment variables template
  • Smart endpoint naming and parameter handling

Usage examples:
  python openapi_to_mcp.py openapi.yaml
  python openapi_to_mcp.py swagger.json --name custom_name
  python openapi_to_mcp.py api_spec.yaml --preview
        """
    )

    parser.add_argument(
        'openapi_file',
        help='Path to the OpenAPI file (JSON or YAML) or URL to download from'
    )

    parser.add_argument(
        '--name', '-n',
        help='Name of the MCP server (automatically detected if not specified)'
    )

    parser.add_argument(
        '--preview', '-p',
        action='store_true',
        help='Only display conversion without saving files'
    )

    parser.add_argument(
        '--verbose', '-v',
        action='store_true',
        help='Verbose output for debugging'
    )

    args = parser.parse_args()

    temp_file_path = None # Initialize here

    try:
        # Create converter
        converter = OpenAPIToMCPConverter()

        # Handle URL or local file
        if converter.is_url(args.openapi_file):
            print(f"🌐 Input detected as URL: {args.openapi_file}")
            temp_file_path = converter.download_from_url(args.openapi_file)
            file_path = temp_file_path
        else:
            # Validate local file
            if not os.path.exists(args.openapi_file):
                print(f"❌ OpenAPI file does not exist: {args.openapi_file}")
                sys.exit(1)
            file_path = args.openapi_file
            print(f"📖 Loading local OpenAPI file: {args.openapi_file}")

        print(f"📁 SERVERS_DIR from .env: {converter.servers_dir}")

        # Load OpenAPI document
        openapi_doc = converter.load_openapi_file(file_path)

        # Validate OpenAPI version
        openapi_version = openapi_doc.get('openapi', openapi_doc.get('swagger', 'unknown'))
        print(f"📋 OpenAPI version: {openapi_version}")

        if not openapi_version.startswith('3.'):
            print(f"⚠️  Warning: Optimized for OpenAPI 3.x, detected version: {openapi_version}")

        # Automatic server name detection
        if args.name:
            server_name = re.sub(r'[^a-zA-Z0-9_]', '_', args.name.lower())
            if server_name != args.name.lower():
                print(f"⚠️  Server name adjusted from '{args.name}' to '{server_name}'")
        else:
            server_name = converter._auto_detect_server_name(openapi_doc, args.openapi_file)
            print(f"🎯 Automatically detected server name: {server_name}")

        # Main conversion
        print(f"\n🔄 Starting automatic conversion...")
        endpoints, security_config = converter.convert_openapi_to_mcp(openapi_doc, server_name)

        print(f"\n✅ Conversion completed:")
        print(f"   📊 Number of endpoints: {len(endpoints)}")
        print(f"   🔒 Security provider: {security_config.get('provider_type', 'none')}")

        # Preview or save
        if args.preview:
            print(f"\n📋 PREVIEW - Endpoints:")
            print(f"{'Method':<8} {'Name':<30} {'URL'}")
            print("─" * 80)

            for endpoint_name, endpoint_config in endpoints.items():
                method = endpoint_config.get('method', 'GET')
                url = endpoint_config.get('url', '')
                # Shorten URL for better display
                display_url = url if len(url) <= 40 else url[:37] + "..."
                print(f"{method:<8} {endpoint_name:<30} {display_url}")

            print(f"\n🔒 PREVIEW - Security:")
            print(json.dumps(security_config, indent=2, ensure_ascii=False))

            if args.verbose:
                print(f"\n🔧 VERBOSE - Detailed overview of endpoints:")
                for endpoint_name, endpoint_config in endpoints.items():
                    print(f"\n📍 {endpoint_name}:")
                    for key, value in endpoint_config.items():
                        if isinstance(value, dict):
                            print(f"   {key}: {json.dumps(value, ensure_ascii=False)}")
                        else:
                            print(f"   {key}: {value}")
        else:
            # Automatic file saving
            server_dir = converter.save_mcp_files(server_name, endpoints, security_config)

            # Check if mcp_manager.py exists
            print(f"\n💡 To register this server manually:")
            print(f"   python mcp_manager.py add {server_name} concurrent_mcp_server.py \\")
            print(f"       --description 'Converted from OpenAPI' \\")
            print(f"       --transport sse --mode public")

    except KeyboardInterrupt:
        print(f"\n⏹️  Conversion interrupted by user")
        sys.exit(1)
    except Exception as e:
        print(f"❌ Error: {e}")
        if args.verbose:
            import traceback

            print(f"\n🔧 VERBOSE - Traceback:")
            traceback.print_exc()
        sys.exit(1)
    finally:
        # Cleanup temporary file if downloaded from URL
        if temp_file_path and os.path.exists(temp_file_path):
            try:
                os.unlink(temp_file_path)
                print(f"�️  Temporary file cleaned up")
            except:
                pass

if __name__ == "__main__":
    main()
