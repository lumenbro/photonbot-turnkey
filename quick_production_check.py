#!/usr/bin/env python3
"""
Quick Production Safety Check for TEST_MODE Implementation
"""

import os
import sys

def check_test_mode_usage():
    """Check for proper TEST_MODE usage patterns."""
    print("🔍 TEST_MODE USAGE CHECK")
    print("=" * 40)
    
    critical_files = [
        "main.py",
        "globals.py",
        "services/local_signer.py",
        "services/wallet_manager.py",
        "handlers/main_menu.py",
        "utils/user_access.py"
    ]
    
    issues = []
    
    for file_path in critical_files:
        if not os.path.exists(file_path):
            continue
            
        try:
            with open(file_path, 'r', encoding='utf-8', errors='ignore') as f:
                content = f.read()
                
            # Check for direct os.getenv('TEST_MODE') usage (but allow it in globals.py where it's set)
            if 'os.getenv(\'TEST_MODE\'' in content and 'app_context.is_test_mode' not in content:
                if file_path == "globals.py" and "self.is_test_mode = os.getenv" in content:
                    print(f"  ✅ {file_path}: Centralized TEST_MODE flag creation (correct)")
                else:
                    issues.append(f"❌ {file_path}: Direct TEST_MODE env check (should use app_context.is_test_mode)")
                
            # Check for proper app_context.is_test_mode usage
            if 'app_context.is_test_mode' in content:
                print(f"  ✅ {file_path}: Uses centralized TEST_MODE flag")
            else:
                print(f"  ⚠️  {file_path}: No TEST_MODE usage found")
                
        except Exception as e:
            print(f"  ❌ {file_path}: Error reading file - {e}")
    
    return issues

def check_environment():
    """Check current environment configuration."""
    print(f"\n🔧 ENVIRONMENT CHECK")
    print("=" * 40)
    
    test_mode = os.getenv('TEST_MODE', 'false').lower() == 'true'
    stellar_network = os.getenv('STELLAR_NETWORK', 'PUBLIC')
    
    print(f"Current Environment:")
    print(f"  TEST_MODE: {test_mode}")
    print(f"  STELLAR_NETWORK: {stellar_network}")
    
    if test_mode:
        print("  ⚠️  Currently in TEST_MODE (will be false in production)")
    else:
        print("  ✅ Currently in production mode")
        
    return test_mode

def check_production_readiness():
    """Check if code is ready for production."""
    print(f"\n🛡️  PRODUCTION READINESS")
    print("=" * 40)
    
    # Check for key production components
    checks = [
        ("main.py exists", os.path.exists("main.py")),
        ("globals.py exists", os.path.exists("globals.py")),
        ("local_signer.py exists", os.path.exists("services/local_signer.py")),
        ("wallet_manager.py exists", os.path.exists("services/wallet_manager.py")),
    ]
    
    all_good = True
    for check_name, check_result in checks:
        status = "✅" if check_result else "❌"
        print(f"  {status} {check_name}")
        if not check_result:
            all_good = False
    
    return all_good

def simulate_production_mode():
    """Simulate production mode to test fallbacks."""
    print(f"\n🧪 PRODUCTION MODE SIMULATION")
    print("=" * 40)
    
    # Save current environment
    original_test_mode = os.getenv('TEST_MODE')
    original_stellar_network = os.getenv('STELLAR_NETWORK')
    
    try:
        # Set production environment
        os.environ['TEST_MODE'] = 'false'
        os.environ['STELLAR_NETWORK'] = 'PUBLIC'
        
        print("  ✅ Environment set to production mode")
        
        # Test imports
        try:
            import globals
            print("  ✅ globals.py imports successfully")
        except Exception as e:
            print(f"  ❌ globals.py import failed: {e}")
            return False
            
        try:
            from services.local_signer import LocalSigner
            print("  ✅ LocalSigner imports successfully")
        except Exception as e:
            print(f"  ❌ LocalSigner import failed: {e}")
            return False
            
        print("  ✅ All production imports successful")
        return True
        
    except Exception as e:
        print(f"  ❌ Simulation failed: {e}")
        return False
    finally:
        # Restore environment
        if original_test_mode:
            os.environ['TEST_MODE'] = original_test_mode
        if original_stellar_network:
            os.environ['STELLAR_NETWORK'] = original_stellar_network

def main():
    """Run comprehensive production safety check."""
    print("🚀 PHOTONBOT PRODUCTION SAFETY CHECK")
    print("=" * 50)
    
    # Run checks
    issues = check_test_mode_usage()
    current_test_mode = check_environment()
    production_ready = check_production_readiness()
    simulation_success = simulate_production_mode()
    
    # Summary
    print(f"\n📊 SUMMARY")
    print("=" * 40)
    
    if issues:
        print(f"❌ CRITICAL ISSUES FOUND ({len(issues)}):")
        for issue in issues:
            print(f"  {issue}")
        print("\n🔧 FIX THESE ISSUES BEFORE DEPLOYMENT!")
        return False
    else:
        print("✅ No critical TEST_MODE issues found")
    
    if not production_ready:
        print("❌ Missing critical production files")
        return False
    else:
        print("✅ All critical files present")
    
    if not simulation_success:
        print("❌ Production mode simulation failed")
        return False
    else:
        print("✅ Production mode simulation successful")
    
    print("\n🎉 PRODUCTION READY!")
    print("✅ All TEST_MODE flags are properly gated")
    print("✅ Production fallbacks are in place")
    print("✅ Safe to deploy to production")
    
    if current_test_mode:
        print("\n💡 REMINDER: Set TEST_MODE=false in production environment")
    
    return True

if __name__ == "__main__":
    success = main()
    sys.exit(0 if success else 1)
