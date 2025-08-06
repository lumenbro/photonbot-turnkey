import boto3
import base64
import json
import logging
import os

logger = logging.getLogger(__name__)

class KMSService:
    def __init__(self):
        self.kms_client = boto3.client("kms", region_name="us-west-1")
        self.key_id = "27958fe3-0f3f-44d4-b21d-9d820d5ad96c"

    def decrypt_session_keys(self, encrypted_data):
        """Decrypt session keys using AWS KMS"""
        try:
            logger.info(f"Attempting to decrypt session keys with key_id: {self.key_id}")
            logger.debug(f"Encrypted data length: {len(encrypted_data)}")
            logger.debug(f"Encrypted data preview: {encrypted_data[:50]}...")
            
            # Decode base64
            ciphertext_blob = base64.b64decode(encrypted_data)
            logger.debug(f"Ciphertext blob length: {len(ciphertext_blob)}")
            
            # Use the same encryption context as the Node.js service
            response = self.kms_client.decrypt(
                CiphertextBlob=ciphertext_blob,
                KeyId=self.key_id,
                EncryptionContext={
                    'Service': 'lumenbro-session-keys',
                    'Environment': os.getenv('NODE_ENV', 'development')
                }
            )
            
            logger.debug(f"KMS response received, plaintext length: {len(response['Plaintext'])}")
            
            # Decode the plaintext
            plaintext_str = response["Plaintext"].decode("utf-8")
            logger.debug(f"Plaintext string: {plaintext_str}")
            
            # Parse JSON
            decrypted_data = json.loads(plaintext_str)
            logger.info(f"Successfully decrypted session keys for public key: {decrypted_data.get('apiPublicKey', 'N/A')[:20]}...")
            
            return decrypted_data["apiPublicKey"], decrypted_data["apiPrivateKey"]
        except Exception as e:
            logger.error(f"KMS decryption failed: {str(e)}")
            logger.error(f"Exception type: {type(e).__name__}")
            raise ValueError(f"KMS decryption failed: {str(e)}")

    def test_connection(self):
        """Test KMS connection and key access"""
        try:
            response = self.kms_client.describe_key(KeyId=self.key_id)
            logger.info(f"KMS connection successful. Key: {response['KeyMetadata']['KeyId']}")
            return True
        except Exception as e:
            logger.error(f"KMS connection failed: {str(e)}")
            return False
