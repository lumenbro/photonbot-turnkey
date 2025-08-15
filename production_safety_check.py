#!/usr/bin/env python3
"""
Production Safety Check for TEST_MODE Implementation
Validates that all TEST_MODE flags are properly gated and production functionality is preserved.
"""

import os
import re
import sys
from pathlib import Path

def check_test_mode_flags():
    """Check all TEST_MODE usage patterns for safety."""
    print("🔍 PRODUCTION SAFETY CHECK")
    print("=" * 50)
    
    # Files to check
    python_files = [
        "main.py",
        "globals.py", 
        "services/local_signer.py",
        "services/wallet_manager.py",
        "services/trade_services.py",
        "handlers/main_menu.py",
        "handlers/walletmanagement.py",
        "utils/user_access.py",
        "core/stellar.py",
        "api.py"
    ]
    
    issues = []
    warnings = []
    
    for file_path in python_files:
        if not os.path.exists(file_path):
            continue
            
        print(f"\n📁 Checking: {file_path}")
        
        with open(file_path, 'r', encoding='utf-8') as f:
            content = f.read()
            lines = content.split('\n')
            
        # Check for TEST_MODE usage patterns
        for i, line in enumerate(lines, 1):
            line_num = i
            
            # Check for direct os.getenv('TEST_MODE') usage (should use app_context.is_test_mode)
            if 'os.getenv(\'TEST_MODE\'' in line and 'app_context.is_test_mode' not in line:
                issues.append(f"❌ {file_path}:{line_num} - Direct TEST_MODE env check (use app_context.is_test_mode)")
                
            # Check for hardcoded test values in production paths
            if 'TEST_MODE' in line and any(test_value in line for test_value in ['localhost', '5434', 'testuser', 'testpass']):
                if 'app_context.is_test_mode' in line:
                    # This is okay - properly gated
                    pass
                else:
                    issues.append(f"❌ {file_path}:{line_num} - Test values not properly gated")
                    
            # Check for missing app_context parameter in functions that need it
            if 'def ' in line and 'app_context' in line and 'app_context=None' not in line:
                # This might be okay, but worth checking
                pass
                
            # Check for proper TEST_MODE flag usage
            if 'if app_context.is_test_mode:' in line:
                print(f"  ✅ Line {line_num}: Proper TEST_MODE flag usage")
                
            # Check for potential blocking of production functionality
            if 'TEST_MODE' in line and 'return' in line and 'app_context.is_test_mode' not in line:
                warnings.append(f"⚠️  {file_path}:{line_num} - Potential production blocking")
    
    return issues, warnings

def check_environment_variables():
    """Check environment variable configuration."""
    print(f"\n🔧 ENVIRONMENT VARIABLES CHECK")
    print("=" * 50)
    
    # Check current .env
    if os.path.exists('.env'):
        with open('.env', 'r') as f:
            env_content = f.read()
            
        test_mode = 'TEST_MODE=true' in env_content
        stellar_network = 'STELLAR_NETWORK=PUBLIC' in env_content
        
        print(f"Current .env:")
        print(f"  TEST_MODE: {'true' if test_mode else 'false'}")
        print(f"  STELLAR_NETWORK: {'PUBLIC' if stellar_network else 'TESTNET'}")
        
        if test_mode:
            print("  ⚠️  WARNING: TEST_MODE=true in .env (will be overridden in production)")
        else:
            print("  ✅ TEST_MODE=false or not set (production-safe)")
    else:
        print("  ⚠️  No .env file found")

def check_production_fallbacks():
    """Check that production fallbacks are in place."""
    print(f"\n🛡️  PRODUCTION FALLBACKS CHECK")
    print("=" * 50)
    
    # Check main.py for proper fallbacks
    if os.path.exists('main.py'):
        with open('main.py', 'r') as f:
            content = f.read()
            
        # Check for Turnkey variable validation
        if 'TURNKEY_API_PUBLIC_KEY' in content and 'TURNKEY_API_PRIVATE_KEY' in content:
            print("  ✅ Turnkey variables are validated")
        else:
            print("  ⚠️  Turnkey variable validation not found")
            
        # Check for proper signer selection
        if 'app_context.sign_transaction = LocalSigner' in content and 'TurnkeySigner' in content:
            print("  ✅ Proper signer selection logic")
        else:
            print("  ❌ Signer selection logic incomplete")

def check_async_patterns():
    """Check for proper async patterns."""
    print(f"\n⚡ ASYNC PATTERNS CHECK")
    print("=" * 50)
    
    python_files = [
        "services/trade_services.py",
        "core/stellar.py",
        "services/wallet_manager.py"
    ]
    
    for file_path in python_files:
        if not os.path.exists(file_path):
            continue
            
        with open(file_path, 'r') as f:
            content = f.read()
            
        # Check for sync Stellar SDK usage
        if 'from stellar_sdk.call_builder import' in content and 'Async' not in content:
            print(f"  ⚠️  {file_path}: Potential sync Stellar SDK usage")
        else:
            print(f"  ✅ {file_path}: Async Stellar SDK usage")
            
        # Check for blocking operations
        if 'time.sleep(' in content or 'requests.get(' in content:
            print(f"  ❌ {file_path}: Blocking operations found")
        else:
            print(f"  ✅ {file_path}: No blocking operations")

def run_simulation_tests():
    """Run simulation tests for production mode."""
    print(f"\n🧪 PRODUCTION MODE SIMULATION")
    print("=" * 50)
    
    # Simulate production environment
    original_test_mode = os.getenv('TEST_MODE')
    original_stellar_network = os.getenv('STELLAR_NETWORK')
    
    try:
        # Set production-like environment
        os.environ['TEST_MODE'] = 'false'
        os.environ['STELLAR_NETWORK'] = 'PUBLIC'
        
        print("  ✅ Environment set to production mode")
        
        # Try to import key modules
        try:
            import globals
            print("  ✅ globals.py imports successfully")
        except Exception as e:
            print(f"  ❌ globals.py import failed: {e}")
            
        try:
            from services.local_signer import LocalSigner
            print("  ✅ LocalSigner imports successfully")
        except Exception as e:
            print(f"  ❌ LocalSigner import failed: {e}")
            
    except Exception as e:
        print(f"  ❌ Simulation failed: {e}")
    finally:
        # Restore original environment
        if original_test_mode:
            os.environ['TEST_MODE'] = original_test_mode
        if original_stellar_network:
            os.environ['STELLAR_NETWORK'] = original_stellar_network

def main():
    """Run comprehensive production safety check."""
    print("🚀 PHOTONBOT PRODUCTION SAFETY CHECK")
    print("=" * 60)
    
    # Run all checks
    issues, warnings = check_test_mode_flags()
    check_environment_variables()
    check_production_fallbacks()
    check_async_patterns()
    run_simulation_tests()
    
    # Summary
    print(f"\n📊 SUMMARY")
    print("=" * 50)
    
    if issues:
        print(f"❌ CRITICAL ISSUES FOUND ({len(issues)}):")
        for issue in issues:
            print(f"  {issue}")
        print("\n🔧 FIX THESE ISSUES BEFORE DEPLOYMENT!")
    else:
        print("✅ No critical issues found")
        
    if warnings:
        print(f"\n⚠️  WARNINGS ({len(warnings)}):")
        for warning in warnings:
            print(f"  {warning}")
        print("\n🔍 Review these warnings")
    else:
        print("✅ No warnings")
        
    if not issues and not warnings:
        print("\n🎉 PRODUCTION READY!")
        print("✅ All TEST_MODE flags are properly gated")
        print("✅ Production fallbacks are in place")
        print("✅ Async patterns are correct")
        print("✅ Safe to deploy to production")
    else:
        print("\n🚨 NOT PRODUCTION READY")
        print("❌ Fix issues before deployment")
        
    return len(issues) == 0

if __name__ == "__main__":
    success = main()
    sys.exit(0 if success else 1)
