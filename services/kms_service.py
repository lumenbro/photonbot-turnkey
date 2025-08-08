import boto3
import base64
import json
import logging
from cryptography.fernet import Fernet

logger = logging.getLogger(__name__)

class KMSService:
    def __init__(self, key_id="27958fe3-0f3f-44d4-b21d-9d820d5ad96c"):
        self.kms_client = boto3.client("kms", region_name="us-west-1")
        self.key_id = key_id
    
    def decrypt_session_key(self, encrypted_session_key):
        """Decrypt session key using KMS"""
        try:
            ciphertext_blob = base64.b64decode(encrypted_session_key)
            response = self.kms_client.decrypt(
                CiphertextBlob=ciphertext_blob,
                KeyId=self.key_id,
                EncryptionContext={
                    'Service': 'lumenbro-session-keys',
                    'Environment': 'production'
                }
            )
            return response['Plaintext']
        except Exception as e:
            logger.error(f"Session key decryption failed: {e}")
            raise
    
    def encrypt_session_key(self, session_key):
        """Encrypt session key using KMS"""
        try:
            response = self.kms_client.encrypt(
                KeyId=self.key_id,
                Plaintext=session_key,
                EncryptionContext={
                    'Service': 'lumenbro-session-keys',
                    'Environment': 'production'
                }
            )
            return base64.b64encode(response['CiphertextBlob']).decode('utf-8')
        except Exception as e:
            logger.error(f"Session key encryption failed: {e}")
            raise
    
    def decrypt_s_address_secret(self, encrypted_data):
        """Decrypt S-address secret - handles both new and old formats"""
        try:
            # First, try to decrypt as new KMS format (JSON payload)
            logger.info("🔍 Attempting new KMS format decryption...")
            try:
                ciphertext_blob = base64.b64decode(encrypted_data)
                response = self.kms_client.decrypt(
                    CiphertextBlob=ciphertext_blob,
                    KeyId=self.key_id,
                    EncryptionContext={
                        'Service': 'lumenbro-migration',
                        'Environment': 'production',
                        'DataType': 's_address'
                    }
                )
                decrypted_json = response['Plaintext'].decode('utf-8')
                payload = json.loads(decrypted_json)
                s_address_secret = payload['s_address_secret']
                logger.info(f"✅ Successfully decrypted new KMS format")
                return s_address_secret
            except Exception as new_format_error:
                logger.info(f"❌ New format failed: {type(new_format_error).__name__}")
            
            # If new format fails, try old hybrid format (KMS + Fernet)
            logger.info("🔍 Attempting old hybrid format decryption...")
            ciphertext_blob = base64.b64decode(encrypted_data)
            
            # The old format has the KMS-encrypted data key at the beginning
            # Try different split points to find the data key
            split_points = [100, 128, 256]
            
            for split_point in split_points:
                if len(ciphertext_blob) < split_point:
                    continue
                    
                try:
                    # Split the ciphertext
                    encrypted_data_key = ciphertext_blob[:split_point]
                    encrypted_secret = ciphertext_blob[split_point:]
                    
                    logger.info(f"   Trying split point: {split_point}")
                    logger.info(f"   Data key length: {len(encrypted_data_key)}")
                    logger.info(f"   Secret length: {len(encrypted_secret)}")
                    
                    # Note: This will fail because we don't have access to the old KMS key
                    # But we can detect the format and provide a helpful error message
                    logger.error(f"❌ Old hybrid format detected but cannot decrypt - missing old KMS key access")
                    logger.error(f"   Data appears to be in old hybrid format (KMS + Fernet)")
                    logger.error(f"   Need access to old KMS key: cd27efb2-0e00-44f5-b218-cb5a6e671a82")
                    logger.error(f"   Account: 961017070653")
                    raise ValueError("Data is in old hybrid format but cannot decrypt without old KMS key access")
                    
                except Exception as split_error:
                    logger.info(f"   ❌ Split point {split_point} failed: {type(split_error).__name__}")
                    continue
            
            logger.error("❌ Could not determine encryption format")
            raise ValueError("Unknown encryption format")
            
        except Exception as e:
            logger.error(f"❌ S-address decryption failed: {str(e)}")
            raise ValueError(f"S-address decryption failed: {str(e)}")
    
    def encrypt_s_address_secret(self, s_address_secret, telegram_id):
        """Encrypt S-address secret with new KMS format"""
        try:
            # Create JSON payload
            payload = {
                "s_address_secret": s_address_secret,
                "telegram_id": str(telegram_id),
                "data_type": "migration"
            }
            
            plaintext = json.dumps(payload).encode('utf-8')
            
            # Encrypt with new KMS
            response = self.kms_client.encrypt(
                KeyId=self.key_id,
                Plaintext=plaintext,
                EncryptionContext={
                    'Service': 'lumenbro-migration',
                    'Environment': 'production',
                    'DataType': 's_address'
                }
            )
            
            return base64.b64encode(response['CiphertextBlob']).decode('utf-8')
        except Exception as e:
            logger.error(f"❌ S-address encryption failed: {e}")
            raise
