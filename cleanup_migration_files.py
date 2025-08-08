#!/usr/bin/env python3
"""
Cleanup script for migration files
Organizes files into backup folder and removes sensitive data from main workspace
"""

import os
import shutil
import logging
from datetime import datetime

# Configure logging
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

def cleanup_migration_files():
    """Clean up migration files and organize backups"""
    
    # Create backup directory with timestamp
    timestamp = datetime.now().strftime('%Y%m%d_%H%M%S')
    backup_dir = f"migration_backup_{timestamp}"
    
    if not os.path.exists(backup_dir):
        os.makedirs(backup_dir)
        logger.info(f"📁 Created backup directory: {backup_dir}")
    
    # Files to move to backup (sensitive data)
    sensitive_files = [
        're_encrypted_users_20250808_070704.csv',
        'backup_sensitive_files/',
        'production_migration_script.py',
        'local_proper_migration.py',
        'proper_migration_script.py',
        'restore_test_user.py',
        'debug_kms_decryption.py',
        'check_encryption_key.py',
        'analyze_migration_format.py',
        'simple_analyze_format.py',
        'decrypt_old_format.py',
        'test_current_data_format.py',
        'scp_sensitive_data.py'
    ]
    
    # Files to delete (temporary test files)
    temp_files = [
        're_encrypted_users_20250808_070534.csv',
        're_encrypted_users_20250808_070317.csv', 
        're_encrypted_users_20250808_064704.csv'
    ]
    
    # Move sensitive files to backup
    logger.info("📦 Moving sensitive files to backup...")
    for file_path in sensitive_files:
        if os.path.exists(file_path):
            if os.path.isdir(file_path):
                # Copy directory
                dest_path = os.path.join(backup_dir, os.path.basename(file_path))
                try:
                    shutil.copytree(file_path, dest_path, dirs_exist_ok=True)
                    logger.info(f"  ✅ Moved directory: {file_path} -> {dest_path}")
                except Exception as e:
                    logger.warning(f"  ⚠️ Could not copy directory {file_path}: {e}")
            else:
                # Copy file
                dest_path = os.path.join(backup_dir, os.path.basename(file_path))
                try:
                    shutil.copy2(file_path, dest_path)
                    logger.info(f"  ✅ Moved file: {file_path} -> {dest_path}")
                except Exception as e:
                    logger.warning(f"  ⚠️ Could not copy file {file_path}: {e}")
        else:
            logger.info(f"  ℹ️ File not found: {file_path}")
    
    # Delete temporary files
    logger.info("🗑️ Deleting temporary files...")
    for file_path in temp_files:
        if os.path.exists(file_path):
            os.remove(file_path)
            logger.info(f"  ✅ Deleted: {file_path}")
        else:
            logger.info(f"  ℹ️ File not found: {file_path}")
    
    # Create README in backup directory
    readme_content = f"""# Migration Backup - {timestamp}

This backup contains sensitive migration files and data.

## Contents:
- re_encrypted_users_20250808_070704.csv: Re-encrypted user data with new KMS
- backup_sensitive_files/: Original CSV files from old databases
- production_migration_script.py: Script used for production migration
- local_proper_migration.py: Local decryption/re-encryption script
- Various test and analysis scripts

## Security Notes:
- All CSV files contain encrypted data (no plaintext secrets)
- Keep this backup secure and don't commit to version control
- Files can be deleted after confirming migration success

## Usage:
- Restore files to workspace if migration needs to be re-run
- Delete this backup once migration is confirmed stable
"""
    
    with open(os.path.join(backup_dir, 'README.md'), 'w') as f:
        f.write(readme_content)
    
    logger.info(f"📝 Created README in backup directory")
    
    # Update .gitignore to exclude backup directories
    gitignore_content = """
# Migration backups
migration_backup_*/
backup_sensitive_files/
re_encrypted_users_*.csv
production_migration_script.py
local_proper_migration.py
proper_migration_script.py
restore_test_user.py
debug_kms_decryption.py
check_encryption_key.py
analyze_migration_format.py
simple_analyze_format.py
decrypt_old_format.py
test_current_data_format.py
scp_sensitive_data.py
"""
    
    # Append to .gitignore if it exists
    if os.path.exists('.gitignore'):
        with open('.gitignore', 'a') as f:
            f.write(gitignore_content)
        logger.info("✅ Updated .gitignore to exclude migration files")
    else:
        with open('.gitignore', 'w') as f:
            f.write(gitignore_content)
        logger.info("✅ Created .gitignore to exclude migration files")
    
    logger.info(f"\n🎉 Cleanup completed!")
    logger.info(f"📁 Backup location: {backup_dir}")
    logger.info(f"🔒 Sensitive files moved to backup")
    logger.info(f"🗑️ Temporary files deleted")
    logger.info(f"📝 .gitignore updated")
    logger.info(f"\n💡 Next steps:")
    logger.info(f"   1. Review backup contents")
    logger.info(f"   2. Test migration with legacy users")
    logger.info(f"   3. Delete backup once confirmed stable")

if __name__ == "__main__":
    cleanup_migration_files()
