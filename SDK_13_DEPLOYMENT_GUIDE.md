# SDK 13.0 Deployment Guide

## Server Deployment Steps

### 1. Update Requirements
```bash
# Update requirements.txt to use SDK 13.0
pip install "stellar-sdk[aiohttp]>=13.0.0" --upgrade
```

### 2. Apply Custom Fixes
```bash
# Run the restore script to apply custom modifications
python restore_stellar_sdk_custom_files.py restore
```

### 3. Verify Installation
```bash
# Check SDK version
python -c "import stellar_sdk; print(f'SDK Version: {stellar_sdk.__version__}')"
```

### 4. Test Bot
```bash
# Start the bot to verify it works
python main.py
```

## What This Does

- **Upgrades to SDK 13.0**: Ready for Protocol 23 (September 3, 2025)
- **Applies Custom Fixes**: Fixes async call builder and exception handling issues
- **Maintains Compatibility**: All existing bot functionality continues to work

## Files Modified

The restore script modifies:
- `stellar_sdk/call_builder/call_builder_async/base_call_builder.py`
- `stellar_sdk/exceptions.py`

## Backup Files

Original files are backed up as:
- `base_call_builder.py.original`
- `exceptions.py.original`

## Rollback (if needed)

To revert to original SDK files:
```bash
# Restore original files
cp base_call_builder.py.original base_call_builder.py
cp exceptions.py.original exceptions.py
```

## Notes

- Custom modifications are required due to SDK aiohttp compatibility issues
- These fixes have been tested and work perfectly with real transactions
- No changes needed to bot code - only SDK files are modified
