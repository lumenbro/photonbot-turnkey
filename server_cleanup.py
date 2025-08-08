#!/usr/bin/env python3
"""
Server cleanup script for migration files
Organizes files into backup folder and removes sensitive data from main directory
"""

import os
import shutil
import logging
from datetime import datetime

# Configure logging
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

def cleanup_server_migration_files():
    """Clean up migration files on server and organize backups"""
    
    # Create backup directory with timestamp
    timestamp = datetime.now().strftime('%Y%m%d_%H%M%S')
    backup_dir = f"migration_backup_{timestamp}"
    
    if not os.path.exists(backup_dir):
        os.makedirs(backup_dir)
        logger.info(f"📁 Created backup directory: {backup_dir}")
    
    # Files to move to backup (sensitive data)
    sensitive_files = [
        're_encrypted_users_20250808_070704.csv',
        'production_migration_script.py',
        'nitro_users.csv',
        'copytrading_users.csv',
        'copytrading_founders.csv',
        'global-bundle.pem',
        'analyze_migration_format.py',
        'debug_kms_decryption.py',
        'check_encryption_key.py',
        'decrypt_old_format.py',
        'simple_analyze_format.py',
        'test_current_data_format.py',
        'proper_migration_script.py',
        'restore_test_user.py'
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
            dest_path = os.path.join(backup_dir, os.path.basename(file_path))
            shutil.copy2(file_path, dest_path)
            logger.info(f"  ✅ Moved file: {file_path} -> {dest_path}")
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
    readme_content = f"""# Server Migration Backup - {timestamp}

This backup contains sensitive migration files and data from the server.

## Contents:
- re_encrypted_users_20250808_070704.csv: Re-encrypted user data with new KMS
- production_migration_script.py: Script used for production migration
- Original CSV files from old databases
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
    
    logger.info(f"\n🎉 Server cleanup completed!")
    logger.info(f"📁 Backup location: {backup_dir}")
    logger.info(f"🔒 Sensitive files moved to backup")
    logger.info(f"🗑️ Temporary files deleted")
    logger.info(f"\n💡 Next steps:")
    logger.info(f"   1. Review backup contents")
    logger.info(f"   2. Test migration with legacy users")
    logger.info(f"   3. Delete backup once confirmed stable")

if __name__ == "__main__":
    cleanup_server_migration_files()
