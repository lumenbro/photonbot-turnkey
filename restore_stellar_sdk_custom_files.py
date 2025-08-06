#!/usr/bin/env python3
"""
Restore custom Stellar SDK files if they get overwritten during package reinstallation.
This script restores the custom modifications from the backup directory.
"""

import os
import shutil
import sys
from pathlib import Path

def find_stellar_sdk_path():
    """Find the Stellar SDK installation path"""
    try:
        import stellar_sdk
        return Path(stellar_sdk.__file__).parent
    except ImportError:
        # Fallback paths
        possible_paths = [
            Path("C:/Python313/Lib/site-packages/stellar_sdk"),  # Windows
            Path("/home/ubuntu/photonbot-live/venv/lib/python3.11/site-packages/stellar_sdk"),  # Linux venv
            Path("/home/ubuntu/photonbot-live/venv/lib/python3.10/site-packages/stellar_sdk"),  # Linux venv
            Path("/home/ubuntu/photonbot-live/venv/lib/python3.9/site-packages/stellar_sdk"),   # Linux venv
        ]
        
        for path in possible_paths:
            if path.exists():
                return path
        
        raise FileNotFoundError("Could not find stellar_sdk installation")

def restore_custom_files():
    """Restore custom Stellar SDK files from backup"""
    
    # Find the correct Stellar SDK path
    stellar_sdk_path = find_stellar_sdk_path()
    backup_path = Path("stellar_sdk_backup")
    
    print(f"Found Stellar SDK at: {stellar_sdk_path}")
    
    # Custom files to restore
    custom_files = [
        {
            "backup": backup_path / "base_call_builder.py",
            "target": stellar_sdk_path / "call_builder" / "call_builder_async" / "base_call_builder.py"
        },
        {
            "backup": backup_path / "exceptions.py", 
            "target": stellar_sdk_path / "exceptions.py"
        }
    ]
    
    print("Restoring custom Stellar SDK files...")
    
    for file_info in custom_files:
        backup_file = file_info["backup"]
        target_file = file_info["target"]
        
        if not backup_file.exists():
            print(f"❌ Backup file not found: {backup_file}")
            continue
            
        if not target_file.parent.exists():
            print(f"❌ Target directory not found: {target_file.parent}")
            continue
            
        try:
            # Create backup of current file if it exists
            if target_file.exists():
                current_backup = target_file.with_suffix(target_file.suffix + ".original")
                shutil.copy2(target_file, current_backup)
                print(f"📋 Backed up current file: {current_backup}")
            
            # Restore custom file
            shutil.copy2(backup_file, target_file)
            print(f"✅ Restored: {target_file}")
            
        except Exception as e:
            print(f"❌ Failed to restore {target_file}: {e}")
    
    print("\nRestoration complete!")
    print("Note: If you had to restore files, you may need to restart your Python environment.")

def check_custom_files():
    """Check if custom files are present and have correct timestamps"""
    
    stellar_sdk_path = find_stellar_sdk_path()
    
    custom_files = [
        stellar_sdk_path / "call_builder" / "call_builder_async" / "base_call_builder.py",
        stellar_sdk_path / "exceptions.py"
    ]
    
    print(f"Checking custom Stellar SDK files in: {stellar_sdk_path}")
    
    for file_path in custom_files:
        if file_path.exists():
            # Check if file was modified on 7/14/2025 (custom modification date)
            stat = file_path.stat()
            if stat.st_mtime >= 1720896000:  # July 14, 2025 timestamp
                print(f"✅ Custom file present: {file_path.name}")
            else:
                print(f"⚠️  File may have been overwritten: {file_path.name}")
        else:
            print(f"❌ Custom file missing: {file_path.name}")

if __name__ == "__main__":
    if len(sys.argv) > 1 and sys.argv[1] == "restore":
        restore_custom_files()
    else:
        check_custom_files()
        print("\nTo restore files, run: python restore_stellar_sdk_custom_files.py restore")
