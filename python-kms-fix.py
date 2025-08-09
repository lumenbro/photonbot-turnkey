# Add this method to Python KMSService class (services/kms_service.py)

def decrypt_session_keys(self, encrypted_session_key):
    """Decrypt session keys using KMS - matches Node.js format"""
    try:
        ciphertext_blob = base64.b64decode(encrypted_session_key)
        response = self.kms_client.decrypt(
            CiphertextBlob=ciphertext_blob,
            KeyId=self.key_id,
            EncryptionContext={
                'Service': 'lumenbro-session-keys',
                'Environment': 'production'  # Match Node.js environment
            }
        )
        
        # Parse the JSON data (Node.js stores as JSON)
        decrypted_json = response['Plaintext'].decode('utf-8')
        session_data = json.loads(decrypted_json)
        
        # Return in the format Python expects: (public_key, private_key)
        return session_data['apiPublicKey'], session_data['apiPrivateKey']
        
    except Exception as e:
        logger.error(f"Session keys decryption failed: {e}")
        raise

# Also fix the environment context to match Node.js
def encrypt_session_keys(self, public_key, private_key):
    """Encrypt session keys using KMS - matches Node.js format"""
    try:
        # Create JSON payload matching Node.js format
        session_data = {
            "apiPublicKey": public_key,
            "apiPrivateKey": private_key
        }
        
        plaintext = json.dumps(session_data).encode('utf-8')
        
        response = self.kms_client.encrypt(
            KeyId=self.key_id,
            Plaintext=plaintext,
            EncryptionContext={
                'Service': 'lumenbro-session-keys',
                'Environment': 'production'  # Match Node.js environment
            }
        )
        return base64.b64encode(response['CiphertextBlob']).decode('utf-8')
    except Exception as e:
        logger.error(f"Session keys encryption failed: {e}")
        raise
