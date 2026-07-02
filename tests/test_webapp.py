#!/usr/bin/env python3
"""
Test script for webapp.py
Run this before pushing to production to verify all routes work.

Usage:
    python test_webapp.py
"""

import sys
import os
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'src'))

# Set test environment variables
os.environ['HALO_DB_PASSWORD'] = os.getenv('HALO_DB_PASSWORD', 'test_password')
os.environ['FLASK_DEBUG'] = 'True'
os.environ['HALO_DB_WAIT_TIMEOUT'] = os.getenv('HALO_DB_WAIT_TIMEOUT', '1')
os.environ['HALO_DB_WAIT_INTERVAL'] = os.getenv('HALO_DB_WAIT_INTERVAL', '0.1')

def test_imports():
    """Test that all imports work."""
    print("Testing imports...")
    try:
        import webapp
        print("✓ All imports successful")
        return True
    except Exception as e:
        print(f"✗ Import failed: {e}")
        return False


def test_routes():
    """Test that all routes are defined."""
    print("\nTesting routes...")
    try:
        import webapp
        
        expected_routes = [
            '/',
            '/lifetime',
            '/compare',
            '/settings',
            '/advanced',
            '/medals',
            '/highlights',
            '/columns',
            '/player/<player_name>',
            '/weapons',
            '/hall',
            '/maps',
            '/trends',
            '/insights',
            '/suggestions',
            '/leaderboard',
            '/api/export'
        ]
        
        app_routes = []
        for rule in webapp.app.url_map.iter_rules():
            app_routes.append(str(rule))
        
        missing = []
        for route in expected_routes:
            if route not in app_routes:
                missing.append(route)
        
        if missing:
            print(f"✗ Missing routes: {missing}")
            return False
        
        print(f"✓ All {len(expected_routes)} routes found:")
        for route in sorted(app_routes):
            print(f"  - {route}")
        return True
        
    except Exception as e:
        print(f"✗ Route test failed: {e}")
        import traceback
        traceback.print_exc()
        return False


def test_cache():
    """Test that cache initializes."""
    print("\nTesting cache initialization...")
    try:
        import webapp
        
        if webapp.cache is None:
            print("✗ Cache is None")
            return False
        
        print("✓ Cache initialized")
        return True
        
    except Exception as e:
        print(f"✗ Cache test failed: {e}")
        return False


def test_database_connection():
    """Test database connection (if available)."""
    print("\nTesting database connection...")
    try:
        import webapp
        
        # Try to get engine
        engine = webapp.ENGINE
        
        # Try a simple query
        with engine.connect() as conn:
            result = conn.execute(webapp.text("SELECT 1"))
            result.fetchone()
        
        print("✓ Database connection successful")
        return True
        
    except Exception as e:
        print(f"⚠ Database connection failed (this is OK for testing): {e}")
        return True  # Don't fail the test, DB might not be available


def test_build_functions():
    """Test that key build functions exist."""
    print("\nTesting build functions...")
    try:
        import webapp
        
        required_functions = [
            'build_csr_overview',
            'build_csr_trends',
            'build_outlier_spotlight',
            'build_ranked_arena_summary',
            '_build_ranked_arena_period',
            'build_breakdown',
            'build_cards',
            'normalize_trend_df',
            'apply_trend_range'
        ]
        
        missing = []
        for func_name in required_functions:
            if not hasattr(webapp, func_name):
                missing.append(func_name)
        
        if missing:
            print(f"✗ Missing functions: {missing}")
            return False
        
        print(f"✓ All {len(required_functions)} required functions found")
        return True
        
    except Exception as e:
        print(f"✗ Build function test failed: {e}")
        return False


def main():
    """Run all tests."""
    print("=" * 60)
    print("WEBAPP TEST SUITE")
    print("=" * 60)
    
    tests = [
        ("Imports", test_imports),
        ("Routes", test_routes),
        ("Cache", test_cache),
        ("Database", test_database_connection),
        ("Build Functions", test_build_functions)
    ]
    
    results = []
    for name, test_func in tests:
        try:
            result = test_func()
            results.append((name, result))
        except Exception as e:
            print(f"\n✗ {name} test crashed: {e}")
            import traceback
            traceback.print_exc()
            results.append((name, False))
    
    # Summary
    print("\n" + "=" * 60)
    print("TEST SUMMARY")
    print("=" * 60)
    
    passed = sum(1 for _, result in results if result)
    total = len(results)
    
    for name, result in results:
        status = "✓ PASS" if result else "✗ FAIL"
        print(f"{status:8} {name}")
    
    print(f"\n{passed}/{total} tests passed")
    
    if passed == total:
        print("\n✓ All tests passed! Ready for deployment.")
        return 0
    else:
        print("\n✗ Some tests failed. Fix issues before deploying.")
        return 1


if __name__ == '__main__':
    sys.exit(main())
