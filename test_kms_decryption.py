#!/usr/bin/env python3
import asyncio
import asyncpg
import os
from dotenv import load_dotenv
from services.kms_service import KMSService
import logging

# Configure logging
logging.basicConfig(level=logging.DEBUG)
logger = logging.getLogger(__name__)

# Load environment variables
load_dotenv()

async def test_kms_decryption():
    """Test KMS decryption with actual database data"""
    
    # Initialize KMS service
    kms_service = KMSService()
    
    # Test KMS connection
    logger.info("Testing KMS connection...")
    if not kms_service.test_connection():
        logger.error("KMS connection failed")
        return
    
    # Connect to database
    db_pool = await asyncpg.create_pool(
        host=os.getenv('DB_HOST'),
        port=int(os.getenv('DB_PORT', 5434)),
        database=os.getenv('DB_NAME'),
        user=os.getenv('DB_USER'),
        password=os.getenv('DB_PASSWORD'),
        ssl='require'
    )
    
    async with db_pool.acquire() as conn:
        # Get user data for telegram_id 5014800072
        telegram_id = 5014800072
        
        logger.info(f"Fetching session data for telegram_id {telegram_id}...")
        
        session_data = await conn.fetchrow(
            """
            SELECT 
                kms_encrypted_session_key,
                kms_key_id,
                temp_api_public_key,
                temp_api_private_key,
                session_expiry,
                session_created_at
            FROM users WHERE telegram_id = $1
            """,
            telegram_id
        )
        
        if not session_data:
            logger.error(f"No session data found for telegram_id {telegram_id}")
            return
        
        logger.info("Session data found:")
        logger.info(f"  kms_encrypted_session_key: {'Present' if session_data['kms_encrypted_session_key'] else 'NULL'}")
        logger.info(f"  kms_key_id: {session_data['kms_key_id']}")
        logger.info(f"  temp_api_public_key: {'Present' if session_data['temp_api_public_key'] else 'NULL'}")
        logger.info(f"  temp_api_private_key: {'Present' if session_data['temp_api_private_key'] else 'NULL'}")
        logger.info(f"  session_expiry: {session_data['session_expiry']}")
        logger.info(f"  session_created_at: {session_data['session_created_at']}")
        
        if session_data['kms_encrypted_session_key']:
            logger.info("Attempting to decrypt KMS session keys...")
            try:
                public_key, private_key = kms_service.decrypt_session_keys(
                    session_data['kms_encrypted_session_key']
                )
                logger.info("✅ KMS decryption successful!")
                logger.info(f"  Decrypted public key: {public_key[:20]}...")
                logger.info(f"  Decrypted private key: {private_key[:20]}...")
            except Exception as e:
                logger.error(f"❌ KMS decryption failed: {str(e)}")
        else:
            logger.info("No KMS encrypted session keys found")
    
    await db_pool.close()

if __name__ == "__main__":
    asyncio.run(test_kms_decryption())
