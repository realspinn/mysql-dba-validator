#!/usr/bin/env python
"""Verify the refactor works correctly."""
import subprocess
import sys

def run_command(cmd, description):
    """Run a command and report result."""
    print(f"\n{'='*60}")
    print(f"{description}")
    print(f"{'='*60}")
    try:
        result = subprocess.run(cmd, shell=True, capture_output=True, text=True, timeout=120)
        print(result.stdout)
        if result.stderr:
            print("STDERR:", result.stderr)
        return result.returncode == 0
    except subprocess.TimeoutExpired:
        print("TIMEOUT - command took too long")
        return False
    except Exception as e:
        print(f"ERROR: {e}")
        return False

def main():
    """Run verification tests."""
    results = {}
    
    # Test 1: pytest
    results['pytest'] = run_command(
        'python -m pytest -q',
        'Running pytest...'
    )
    
    # Test 2: pip check
    results['pip_check'] = run_command(
        'python -m pip check',
        'Running pip check...'
    )
    
    # Test 3: py_compile
    results['py_compile'] = run_command(
        'python -m py_compile backend/main.py connector/server.py',
        'Running py_compile...'
    )
    
    # Summary
    print(f"\n{'='*60}")
    print("VERIFICATION RESULTS")
    print(f"{'='*60}")
    for name, passed in results.items():
        status = "✓ PASS" if passed else "✗ FAIL"
        print(f"{name}: {status}")
    
    all_pass = all(results.values())
    print(f"\nOverall: {'✓ ALL PASS' if all_pass else '✗ SOME FAILED'}")
    return 0 if all_pass else 1

if __name__ == '__main__':
    sys.exit(main())
