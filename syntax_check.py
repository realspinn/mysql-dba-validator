#!/usr/bin/env python
"""Quick syntax check."""
import py_compile
import sys

try:
    py_compile.compile(r'd:\mysql-dba-validator-v1.1\mysql-dba-validator\backend\main.py', doraise=True)
    py_compile.compile(r'd:\mysql-dba-validator-v1.1\mysql-dba-validator\connector\server.py', doraise=True)
    
    # Try to import
    sys.path.insert(0, r'd:\mysql-dba-validator-v1.1\mysql-dba-validator')
    from backend.main import ValidateRequest
    
    print('Python Syntax: OK')
    print('ValidateRequest fields:', list(ValidateRequest.model_fields.keys()))
    
except Exception as e:
    print(f'Error: {e}')
    sys.exit(1)
