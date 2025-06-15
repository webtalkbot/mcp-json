#!/usr/bin/env python3
"""
batch_openapi_convert.py - Batch conversion of OpenAPI files
"""

import os
import sys
import glob
import json
import yaml
from pathlib import Path
from concurrent.futures import ThreadPoolExecutor
import threading
import subprocess

def find_converter_script():
    """Find the OpenAPI converter script automatically"""
    possible_names = [
        "openapi_to_mcp.py",
        "openapi_to_mcp-json.py", 
        "openapi_converter.py"
    ]
    
    for name in possible_names:
        if os.path.exists(name):
            return name
    
    raise FileNotFoundError("OpenAPI converter script not found. Tried: " + ", ".join(possible_names))

def is_openapi_file(file_path: str) -> bool:
    """Check if file is actually an OpenAPI specification"""
    try:
        if file_path.endswith('.json'):
            with open(file_path, 'r') as f:
                data = json.load(f)
        else:
            with open(file_path, 'r') as f:
                data = yaml.safe_load(f)
        
        # Check for OpenAPI indicators
        return (
            'openapi' in data or 
            'swagger' in data or 
            ('info' in data and 'paths' in data)
        )
    except:
        return False

def convert_single_file(file_path: str, converter_script: str) -> tuple:
    """Convert a single file and return result"""
    try:
        print(f"📄 Processing: {file_path}")
        result = subprocess.run(
            [sys.executable, converter_script, file_path], 
            capture_output=True, text=True, timeout=300
        )
        
        if result.returncode == 0:
            return (file_path, True, "Success")
        else:
            return (file_path, False, result.stderr.strip())
    except Exception as e:
        return (file_path, False, str(e))

def batch_convert_openapi_files(directory: str = ".", pattern: str = "*.yaml"):
    """Batch conversion of all OpenAPI files in a directory"""
    
    # Extended patterns for OpenAPI files
    patterns = [
        "*.yaml", "*.yml", "*.json",
        "*openapi*", "*swagger*", "*api*.yaml", "*api*.json"
    ]
    
    openapi_files = []
    for pattern in patterns:
        openapi_files.extend(glob.glob(os.path.join(directory, pattern)))
    
    # Filtering duplicates and invalid files
    openapi_files = list(set(openapi_files))
    openapi_files = [f for f in openapi_files if os.path.isfile(f) and is_openapi_file(f)]
    
    if not openapi_files:
        print(f"❌ No OpenAPI files found in {directory}")
        return
    
    print(f"🔍 Found {len(openapi_files)} potential OpenAPI files:")
    for i, file_path in enumerate(openapi_files, 1):
        print(f"   {i}. {file_path}")
    
    print(f"\n🚀 Starting batch conversion...")
    
    successful = 0
    failed = 0
    
    converter_script = find_converter_script()
    
    with ThreadPoolExecutor(max_workers=4) as executor:
        futures = [executor.submit(convert_single_file, f, converter_script) for f in openapi_files]
        
        for future in futures:
            file_path, success, message = future.result()
            if success:
                print(f"   ✅ {file_path}: {message}")
                successful += 1
            else:
                print(f"   ❌ {file_path}: {message}")
                failed += 1
    
    print(f"\n📊 Batch conversion summary:")
    print(f"   ✅ Successful: {successful}")
    print(f"   ❌ Failed: {failed}")
    print(f"   📁 All servers saved to: {os.getenv('SERVERS_DIR', './servers')}")

if __name__ == "__main__":
    import argparse
    
    parser = argparse.ArgumentParser(description="Batch conversion of OpenAPI files")
    parser.add_argument('--directory', '-d', default='.', help='Directory to scan')
    parser.add_argument('--pattern', '-p', default='*.yaml', help='File pattern')
    
    args = parser.parse_args()
    batch_convert_openapi_files(args.directory, args.pattern)
