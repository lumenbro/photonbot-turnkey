import boto3
import base64
import json
import logging

logger = logging.getLogger(__name__)

class KMSService:
    def __init__(self):
        self.kms_client = boto3.client("kms", region_name="us-west-1")
        self.key_id = "27958fe3-0f3f-44d4-b21d-9d820d5ad96c"

    def decrypt_session_keys(self, encrypted_data):
        """Decrypt session keys using AWS KMS"""
        try:
            response = self.kms_client.decrypt(
                CiphertextBlob=base64.b64decode(encrypted_data),
                KeyId=self.key_id
            )
            decrypted_data = json.loads(response["Plaintext"].decode("utf-8"))
            return decrypted_data["apiPublicKey"], decrypted_data["apiPrivateKey"]
        except Exception as e:
            logger.error(f"KMS decryption failed: {str(e)}")
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
