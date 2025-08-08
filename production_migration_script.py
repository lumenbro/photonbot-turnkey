#!/usr/bin/env python3
"""
Production migration script to import user data to RDS
"""

import asyncio
import asyncpg
import pandas as pd
import logging
import os
import sys
from datetime import datetime
from dotenv import load_dotenv

# Configure logging
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# Load environment variables
load_dotenv()

async def migrate_users_to_production():
    """Migrate users from CSV files to production RDS database"""
    
    # Production database connection
    DB_CONFIG = {
        'host': 'lumenbro-turnkey.cz2imkksk7b4.us-west-1.rds.amazonaws.com',
        'port': 5434,
        'user': 'postgres',
        'password': os.getenv('DB_PASSWORD', 'your-password'),
        'database': 'postgres',
        'ssl': 'require'
    }
    
    try:
        # Connect to production database
        pool = await asyncpg.create_pool(**DB_CONFIG)
        logger.info("🔗 Connected to production database")
        
        async with pool.acquire() as conn:
            # Check if migration has already been run
            migration_check = await conn.fetchval(
                "SELECT COUNT(*) FROM users WHERE source_old_db IS NOT NULL"
            )
            
            if migration_check > 0:
                logger.warning(f"⚠️ Migration already run! Found {migration_check} migrated users")
                response = input("Do you want to continue anyway? (y/N): ")
                if response.lower() != 'y':
                    logger.info("Migration cancelled")
                    return False
            
            # Load migration data
            logger.info("📊 Loading migration data...")
            
            # Load nitro users
            nitro_users = pd.read_csv('nitro_users.csv')
            logger.info(f"📋 Loaded {len(nitro_users)} nitro users")
            
            # Load copy trading users
            copytrading_users = pd.read_csv('copytrading_users.csv')
            logger.info(f"📋 Loaded {len(copytrading_users)} copy trading users")
            
            # Load founders
            copytrading_founders = pd.read_csv('copytrading_founders.csv')
            logger.info(f"👑 Loaded {len(copytrading_founders)} founders")
            
            # Start migration
            logger.info("🚀 Starting migration...")
            
            migrated_count = 0
            founder_count = 0
            
            for _, user in nitro_users.iterrows():
                telegram_id = user['telegram_id']
                
                # Skip the -1 user (disbursement wallet)
                if telegram_id == -1:
                    continue
                
                # Check if user already exists
                existing = await conn.fetchval(
                    "SELECT telegram_id FROM users WHERE telegram_id = $1",
                    telegram_id
                )
                
                if existing:
                    logger.info(f"⏭️ User {telegram_id} already exists, skipping")
                    continue
                
                # Get referral code from copy trading data
                referral_code = None
                copytrading_user = copytrading_users[copytrading_users['telegram_id'] == telegram_id]
                if not copytrading_user.empty:
                    referral_code = copytrading_user.iloc[0]['referral_code']
                
                # Check if user is a founder
                is_founder = bool(copytrading_founders['telegram_id'].isin([telegram_id]).any())
                
                # Insert user
                await conn.execute("""
                    INSERT INTO users (
                        telegram_id, 
                        public_key, 
                        encrypted_s_address_secret,
                        source_old_db,
                        migration_date,
                        pioneer_status,
                        referral_code
                    ) VALUES ($1, $2, $3, $4, $5, $6, $7)
                """, 
                telegram_id,
                user['public_key'],
                user['encrypted_secret'],
                'nitro_users.csv',
                datetime.now(),
                is_founder,
                referral_code
                )
                
                # Add to founders table if applicable
                if is_founder:
                    await conn.execute("""
                        INSERT INTO founders (telegram_id) VALUES ($1)
                        ON CONFLICT (telegram_id) DO NOTHING
                    """, telegram_id)
                    founder_count += 1
                
                migrated_count += 1
                logger.info(f"✅ Migrated user {telegram_id} (founder: {is_founder})")
            
            logger.info(f"\n🎉 Migration completed!")
            logger.info(f"📊 Total users migrated: {migrated_count}")
            logger.info(f"👑 Founders added: {founder_count}")
            
            return True
            
    except Exception as e:
        logger.error(f"❌ Migration failed: {e}")
        return False
    finally:
        if 'pool' in locals():
            await pool.close()

if __name__ == "__main__":
    success = asyncio.run(migrate_users_to_production())
    sys.exit(0 if success else 1)
