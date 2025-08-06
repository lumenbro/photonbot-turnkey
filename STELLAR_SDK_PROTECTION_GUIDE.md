# Stellar SDK Custom Files Protection Guide

## 🎯 **Overview**
This guide ensures that custom modifications to the Stellar SDK are preserved during package reinstallation or environment updates.

## 📁 **Custom Files Identified**
Based on the KMS_INTEGRATION_SUMMARY.md, the following files contain custom modifications:

1. **`base_call_builder.py`**
   - Location: `C:\Python313\Lib\site-packages\stellar_sdk\call_builder\call_builder_async\`
   - Modified: July 14, 2025 at 1:04 PM
   - Purpose: Custom Grok 4 integration modifications

2. **`exceptions.py`**
   - Location: `C:\Python313\Lib\site-packages\stellar_sdk\`
   - Modified: July 14, 2025 at 1:50 PM
   - Purpose: Custom error handling modifications

## 🔒 **Protection Strategy**

### **1. Backup Created**
✅ **Backup Location**: `stellar_sdk_backup/`
- `base_call_builder.py` - Custom call builder modifications
- `exceptions.py` - Custom exception handling

### **2. Restoration Script**
✅ **Script**: `restore_stellar_sdk_custom_files.py`
- **Check Status**: `python restore_stellar_sdk_custom_files.py`
- **Restore Files**: `python restore_stellar_sdk_custom_files.py restore`

### **3. Before Package Operations**
**ALWAYS** run these commands before reinstalling packages:

```bash
# 1. Check if custom files are present
python restore_stellar_sdk_custom_files.py

# 2. If files are missing or overwritten, restore them
python restore_stellar_sdk_custom_files.py restore
```

## ⚠️ **Critical Operations That Require Protection**

### **Package Reinstallation**
```bash
# BEFORE reinstalling stellar-sdk
python restore_stellar_sdk_custom_files.py

pip uninstall stellar-sdk
pip install stellar-sdk==12.2.0

# AFTER reinstalling
python restore_stellar_sdk_custom_files.py restore
```

### **Virtual Environment Creation**
```bash
# Create new venv
python -m venv new_env

# Activate and install packages
new_env\Scripts\activate
pip install -r requirements.txt

# Restore custom files to new environment
python restore_stellar_sdk_custom_files.py restore
```

### **System Python Updates**
```bash
# After any Python or pip updates
python restore_stellar_sdk_custom_files.py restore
```

## 🔍 **Identification Method**

### **Timestamp Check**
Custom files have different timestamps than the rest of the library:
- **Custom files**: Modified on July 14, 2025
- **Original files**: Modified on July 13, 2025

### **File Size Differences**
Custom files may have different sizes due to modifications.

## 🛠️ **Automated Protection**

### **Pre-Install Hook (Optional)**
You can create a pip pre-install hook to automatically backup files:

```bash
# Create pip configuration
mkdir %APPDATA%\pip
echo [global] > %APPDATA%\pip\pip.conf
echo pre-install = python restore_stellar_sdk_custom_files.py >> %APPDATA%\pip\pip.conf
```

## 📋 **Checklist Before Deployment**

- [ ] Custom files are present and have correct timestamps
- [ ] Backup directory contains latest versions
- [ ] Restoration script works correctly
- [ ] Test import of stellar_sdk after any changes

## 🚨 **Emergency Recovery**

If custom files are lost:

1. **Check backup**: `dir stellar_sdk_backup`
2. **Restore immediately**: `python restore_stellar_sdk_custom_files.py restore`
3. **Verify restoration**: `python restore_stellar_sdk_custom_files.py`
4. **Test functionality**: Import and test stellar_sdk features

## 📞 **Troubleshooting**

### **Files Not Restoring**
- Check file permissions
- Ensure backup files exist
- Verify target directories exist

### **Import Errors After Restoration**
- Restart Python environment
- Clear `__pycache__` directories
- Reinstall stellar-sdk if needed

---

**Last Updated**: August 6, 2025
**Status**: ✅ Backup created, restoration script functional
