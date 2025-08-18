# Stellar SDK 13.0 Upgrade Guide
## PhotonBot Turnkey Migration Strategy

**Timeline**: August 17, 2025 → September 3, 2025 (Protocol 23 Deadline)  
**Current Version**: Stellar SDK 12.2.0 (with custom modifications)  
**Target Version**: Stellar SDK 13.0.0  

---

## 🎯 **Overview**

This guide provides a comprehensive strategy for upgrading PhotonBot from Stellar SDK 12.2.0 to 13.0.0 before the Protocol 23 deadline on September 3, 2025.

### **Critical Considerations**
- **Custom Modifications**: Your bot has custom modifications to `base_call_builder.py` and `exceptions.py`
- **Heavy Async Usage**: Extensive use of async call builders throughout the codebase
- **Production Safety**: Must maintain 100% uptime during transition
- **Protocol 23 Deadline**: September 3, 2025 (2.5 weeks from now)

---

## 📋 **Testing Strategy**

### **Phase 1: Quick Clean Test (Day 1)**
```bash
# First, test if clean SDK 13.0 works without custom modifications
quick_test_sdk13.bat  # Windows
# OR
python quick_test_sdk13_clean.py  # Direct Python

# This tests:
# ✅ All Stellar SDK imports without custom modifications
# ✅ Async call builder functionality
# ✅ Bot module imports
# ✅ Basic async functionality
```

### **Phase 2: Full Environment Setup (If Needed)**
```bash
# If clean test fails, run full setup with custom modifications
python setup_sdk13_test_env.py

# This will:
# 1. Create sdk13_test_env virtual environment
# 2. Install Stellar SDK 13.0.0
# 3. Install all other dependencies
# 4. Test clean SDK 13.0 first
# 5. Apply custom modifications if needed
# 6. Create test_sdk13_clean.bat and test_sdk13_custom.bat
```

### **Phase 3: SDK 13.0 Compatibility Testing (Days 2-3)**
```bash
# If clean test passed, you're ready for production testing
# If clean test failed, use the full environment:

# Test clean SDK 13.0
test_sdk13_clean.bat  # Windows
# OR
./test_sdk13_clean.sh  # Linux

# Test with custom modifications
test_sdk13_custom.bat  # Windows
# OR
./test_sdk13_custom.sh  # Linux

# Tests include:
# ✅ All Stellar SDK imports
# ✅ Async call builder functionality
# ✅ Bot module imports
# ✅ Custom modifications compatibility (if needed)
```

### **Phase 4: Custom Modifications Assessment (Days 4-5)**
- **Check if custom modifications are still needed** in SDK 13.0
- **Compare functionality** between SDK 12.2.0 and 13.0
- **Update custom files** if necessary for SDK 13.0 compatibility

### **Phase 5: Full Bot Testing (Days 6-8)**
- **Test all bot functionality** in the SDK 13.0 environment
- **Verify async operations** work correctly
- **Test wallet operations**, trading, and all critical features
- **Performance testing** to ensure no regressions

### **Phase 6: Production Deployment (Days 9-10)**
- **Update requirements.txt** to SDK 13.0
- **Deploy to test server** first
- **Monitor for 24-48 hours**
- **Deploy to production** if no issues

---

## 🔧 **Detailed Testing Steps**

### **Step 1: Environment Setup**
```bash
# 1. Create test environment
python setup_sdk13_test_env.py

# 2. Verify setup
cd sdk13_test_env
python -c "import stellar_sdk; print(stellar_sdk.__version__)"
# Should output: 13.0.0
```

### **Step 2: Compatibility Testing**
```bash
# Run the comprehensive test suite
python test_sdk_13_upgrade.py

# Expected output:
# ✅ SDK Imports: PASS
# ✅ Async Functionality: PASS
# ✅ Bot Module Imports: PASS
```

### **Step 3: Custom Modifications Check**
```bash
# Check if custom files are still needed
python restore_stellar_sdk_custom_files.py

# If custom files are overwritten, restore them
python restore_stellar_sdk_custom_files.py restore
```

### **Step 4: Bot Functionality Testing**
```bash
# Test core bot functionality in SDK 13.0 environment
cd sdk13_test_env
python -c "
import asyncio
from core.stellar import *
from services.wallet_manager import *
from handlers.main_menu import *
print('✅ All core modules import successfully')
"
```

---

## 🚨 **Potential Issues & Solutions**

### **Issue 1: Import Errors**
**Symptoms**: `ImportError` when importing Stellar SDK modules
**Solution**: 
- Check if module paths have changed in SDK 13.0
- Update import statements if necessary
- Verify all dependencies are compatible

### **Issue 2: Async Call Builder Changes**
**Symptoms**: Async operations fail or behave differently
**Solution**:
- Review SDK 13.0 changelog for async changes
- Update async call builder usage if needed
- Test all async operations thoroughly

### **Issue 3: Custom Modifications Conflicts**
**Symptoms**: Custom modifications don't work with SDK 13.0
**Solution**:
- Check if functionality is now built-in to SDK 13.0
- Update custom modifications for SDK 13.0 compatibility
- Consider removing custom modifications if no longer needed

### **Issue 4: Performance Regressions**
**Symptoms**: Bot runs slower or uses more resources
**Solution**:
- Profile performance before and after upgrade
- Identify bottlenecks
- Optimize if necessary

---

## 📊 **Testing Checklist**

### **Pre-Testing**
- [ ] Backup current production environment
- [ ] Document current Stellar SDK version (12.2.0)
- [ ] Create test environment with SDK 13.0
- [ ] Restore custom modifications to test environment

### **Compatibility Testing**
- [ ] All Stellar SDK imports work
- [ ] Async call builders function correctly
- [ ] Bot modules import without errors
- [ ] Custom modifications are compatible
- [ ] No breaking changes in API usage

### **Functionality Testing**
- [ ] Wallet creation and management
- [ ] Transaction signing and submission
- [ ] Trading operations (DEX)
- [ ] Copy trading functionality
- [ ] Referral system
- [ ] Price fetching and caching
- [ ] Database operations
- [ ] Telegram bot interactions

### **Performance Testing**
- [ ] Response times are acceptable
- [ ] Memory usage is reasonable
- [ ] No memory leaks
- [ ] Concurrent user handling

### **Production Readiness**
- [ ] All tests pass
- [ ] Performance is acceptable
- [ ] Custom modifications work
- [ ] Backup and rollback plan ready

---

## 🚀 **Production Deployment Plan**

### **Step 1: Update Requirements**
```bash
# Update requirements.txt
# Change: stellar-sdk>=8.0.0
# To: stellar-sdk>=13.0.0
```

### **Step 2: Test Server Deployment**
```bash
# Deploy to test server first
git pull origin main
pip install -r requirements.txt
python restore_stellar_sdk_custom_files.py restore
sudo systemctl restart photonbot-test
```

### **Step 3: Monitor Test Server**
- Monitor for 24-48 hours
- Check logs for errors
- Verify all functionality works
- Test with real transactions

### **Step 4: Production Deployment**
```bash
# Deploy to production
git pull origin main
pip install -r requirements.txt
python restore_stellar_sdk_custom_files.py restore
sudo systemctl restart photonbot
```

### **Step 5: Post-Deployment Monitoring**
- Monitor logs for 24 hours
- Check for any errors or issues
- Verify all user operations work
- Be ready to rollback if needed

---

## 🔄 **Rollback Plan**

### **If Issues Arise**
```bash
# 1. Stop the bot
sudo systemctl stop photonbot

# 2. Revert to SDK 12.2.0
pip uninstall stellar-sdk
pip install stellar-sdk==12.2.0

# 3. Restore custom modifications
python restore_stellar_sdk_custom_files.py restore

# 4. Restart the bot
sudo systemctl start photonbot
```

### **Emergency Contacts**
- **Backup Environment**: Keep SDK 12.2.0 environment ready
- **Database Backup**: Ensure recent database backup
- **Custom Files**: Keep backup of custom modifications

---

## 📈 **Success Metrics**

### **Technical Metrics**
- [ ] Zero import errors
- [ ] All async operations work
- [ ] Performance within 10% of current
- [ ] No memory leaks
- [ ] All bot features functional

### **Business Metrics**
- [ ] Zero downtime during upgrade
- [ ] All user operations work
- [ ] No user complaints
- [ ] Trading functionality intact
- [ ] Wallet operations successful

---

## 🎯 **Timeline Summary**

| Day | Task | Status |
|-----|------|--------|
| 1 | Environment Setup | ⏳ Pending |
| 2-3 | Compatibility Testing | ⏳ Pending |
| 4-5 | Custom Modifications | ⏳ Pending |
| 6-8 | Full Bot Testing | ⏳ Pending |
| 9-10 | Production Deployment | ⏳ Pending |
| 11+ | Monitoring | ⏳ Pending |

**Deadline**: September 3, 2025 (Protocol 23)

---

## 📞 **Support & Resources**

### **Documentation**
- [Stellar SDK 13.0 Changelog](https://github.com/StellarCN/py-stellar-base/blob/main/CHANGELOG.md)
- [Protocol 23 Information](https://stellar.org/protocol/protocol-23)
- [Async Call Builder Documentation](https://stellar-sdk.readthedocs.io/)

### **Testing Tools**
- `setup_sdk13_test_env.py` - Environment setup
- `test_sdk_13_upgrade.py` - Compatibility testing
- `restore_stellar_sdk_custom_files.py` - Custom file management

### **Emergency Procedures**
- Rollback script ready
- Backup environment available
- Custom file backups maintained

---

**Last Updated**: August 17, 2025  
**Status**: Ready for testing phase
