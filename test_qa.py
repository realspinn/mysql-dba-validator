#!/usr/bin/env python
"""Quick product QA tests"""
import json
import os
import urllib.request
import sys

def test_local_mode_connection():
    """Test Local Mode connection"""
    data = json.dumps({
        'host': '127.0.0.1',
        'port': 3306,
        'username': 'readonly',
        'password': os.environ.get('MYSQL_PASSWORD', '')
    }).encode('utf-8')
    
    req = urllib.request.Request(
        'http://127.0.0.1:8420/api/connections/test',
        data=data,
        headers={'Content-Type': 'application/json'}
    )
    
    try:
        with urllib.request.urlopen(req) as response:
            result = json.loads(response.read().decode())
            print("✓ Local Mode Connection Test PASSED")
            return True
    except Exception as e:
        print(f"✗ Local Mode Connection Test FAILED: {e}")
        return False

def test_invalid_port():
    """Test that invalid port (3307) is rejected"""
    data = json.dumps({
        'host': '127.0.0.1',
        'port': 3307,
        'username': 'readonly',
        'password': os.environ.get('MYSQL_PASSWORD', '')
    }).encode('utf-8')
    
    req = urllib.request.Request(
        'http://127.0.0.1:8420/api/connections/test',
        data=data,
        headers={'Content-Type': 'application/json'}
    )
    
    try:
        with urllib.request.urlopen(req) as response:
            result = json.loads(response.read().decode())
            print("✗ Port validation FAILED - port 3307 should be rejected")
            return False
    except urllib.error.HTTPError as e:
        if e.code == 422:
            print("✓ Port validation PASSED - port 3307 correctly rejected")
            return True
        else:
            print(f"✗ Port validation FAILED - unexpected error code {e.code}")
            return False
    except Exception as e:
        print(f"✗ Port validation FAILED - unexpected error: {e}")
        return False

def test_health():
    """Test health endpoint"""
    try:
        with urllib.request.urlopen('http://127.0.0.1:8420/api/health') as response:
            result = json.loads(response.read().decode())
            print("✓ Health endpoint PASSED")
            return True
    except Exception as e:
        print(f"✗ Health endpoint FAILED: {e}")
        return False

if __name__ == '__main__':
    print("\n=== LOCAL MODE UX VALIDATION ===\n")
    
    results = []
    results.append(("Startup", test_health()))
    results.append(("Local Mode Connection", test_local_mode_connection()))
    results.append(("Port Validation", test_invalid_port()))
    
    print("\n=== RESULTS ===")
    for test_name, passed in results:
        status = "PASS" if passed else "FAIL"
        print(f"{test_name}: {status}")
    
    if all(r[1] for r in results):
        print("\n✓ All Local Mode tests PASSED")
        sys.exit(0)
    else:
        print("\n✗ Some tests FAILED")
        sys.exit(1)
