#!/usr/bin/env python3
"""
Decrypt data using the old bot's encryption method
"""

import asyncio
import asyncpg
import boto3
import base64
import json
import logging
import os
from cryptography.fernet import Fernet
from dotenv import load_dotenv

# Configure logging
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# Load environment variables
load_dotenv()

async def decrypt_old_bot_data():
    """Decrypt data using the old bot's encryption method"""
    
    TEST_USER_ID = 1723652081
    
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
            # Get test user's encrypted data
            user_data = await conn.fetchrow("""
                SELECT encrypted_s_address_secret, public_key, source_old_db
                FROM users WHERE telegram_id = $1
            """, TEST_USER_ID)
            
            if not user_data or not user_data['encrypted_s_address_secret']:
                logger.error(f"❌ No encrypted data found for user {TEST_USER_ID}")
                return False
            
            encrypted_data = user_data['encrypted_s_address_secret']
            logger.info(f"📋 Found encrypted data for user {TEST_USER_ID}")
            logger.info(f"   Public key: {user_data['public_key']}")
            logger.info(f"   Source: {user_data['source_old_db']}")
            
            # The data appears to be encrypted with the old bot's method
            # Let's try to decrypt it using the old KMS key and Fernet
            
            # First, let's try to use the old KMS key (from the old AWS account)
            old_kms_client = boto3.client("kms", region_name="us-west-1")
            
            # We need to find the old KMS key ID
            # Let's try some common patterns
            old_key_candidates = [
                "27958fe3-0f3f-44d4-b21d-9d820d5ad96c",  # Current key
                # Add other possible old key IDs here
            ]
            
            logger.info("🔍 Trying to decrypt with old bot's method...")
            
            # The old bot likely used this pattern:
            # 1. KMS encrypts a data key
            # 2. Fernet encrypts the actual data with that data key
            # 3. Both are stored together
            
            # Let's try to parse the encrypted data as if it contains both
            try:
                # Decode base64
                ciphertext_blob = base64.b64decode(encrypted_data)
                logger.info(f"🔐 Ciphertext blob length: {len(ciphertext_blob)}")
                
                # Try to extract the encrypted data key (first part)
                # Old bot might have stored: encrypted_data_key + encrypted_data
                if len(ciphertext_blob) > 100:  # Reasonable minimum
                    # Assume first 100 bytes are the encrypted data key
                    encrypted_data_key = ciphertext_blob[:100]
                    encrypted_actual_data = ciphertext_blob[100:]
                    
                    logger.info(f"🔑 Encrypted data key length: {len(encrypted_data_key)}")
                    logger.info(f"📦 Encrypted actual data length: {len(encrypted_actual_data)}")
                    
                    # Try to decrypt the data key with the old KMS key
                    for key_id in old_key_candidates:
                        try:
                            logger.info(f"🔍 Trying key: {key_id}")
                            
                            # Decrypt the data key
                            response = old_kms_client.decrypt(
                                CiphertextBlob=encrypted_data_key,
                                KeyId=key_id
                            )
                            
                            data_key = response['Plaintext']
                            logger.info(f"✅ Successfully decrypted data key with key: {key_id}")
                            
                            # Now use the data key to decrypt the actual data with Fernet
                            fernet = Fernet(data_key)
                            decrypted_data = fernet.decrypt(encrypted_actual_data)
                            
                            s_address_secret = decrypted_data.decode('utf-8')
                            logger.info(f"✅ Successfully decrypted S-address secret!")
                            logger.info(f"   S-address: {s_address_secret[:20]}...")
                            
                            return True
                            
                        except Exception as e:
                            logger.info(f"❌ Failed with key {key_id}: {type(e).__name__}")
                            continue
                    
                    logger.error("❌ All key candidates failed")
                    
                else:
                    logger.error("❌ Ciphertext too short for expected format")
                    
            except Exception as e:
                logger.error(f"❌ Error parsing ciphertext: {e}")
            
            return False
            
    except Exception as e:
        logger.error(f"❌ Decrypt failed: {e}")
        return False
    finally:
        if 'pool' in locals():
            await pool.close()

if __name__ == "__main__":
    success = asyncio.run(decrypt_old_bot_data())
    exit(0 if success else 1)
