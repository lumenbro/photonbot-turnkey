# COPY THIS METHOD INTO services/kms_service.py in your Python project
# Add it to the KMSService class alongside the existing methods

def decrypt_session_keys(self, encrypted_session_key):
    """Decrypt session keys using KMS - matches Node.js format
    
    This method is called by the Python bot and expects to return a tuple
    of (public_key, private_key). Node.js stores the keys as JSON, so we
    need to parse the JSON and return the expected format.
    """
    try:
        # Decode the base64 encrypted data
        ciphertext_blob = base64.b64decode(encrypted_session_key)
        
        # Decrypt using KMS with the same context as Node.js
        response = self.kms_client.decrypt(
            CiphertextBlob=ciphertext_blob,
            KeyId=self.key_id,
            EncryptionContext={
                'Service': 'lumenbro-session-keys',
                'Environment': 'production'  # Must match Node.js environment
            }
        )
        
        # Parse the JSON data (Node.js stores session keys as JSON)
        decrypted_json = response['Plaintext'].decode('utf-8')
        session_data = json.loads(decrypted_json)
        
        # Log success for debugging
        logger.info(f"✅ Successfully decrypted session keys for environment: production")
        logger.debug(f"   Public key: {session_data['apiPublicKey'][:20]}...")
        
        # Return in the format Python bot expects: (public_key, private_key)
        return session_data['apiPublicKey'], session_data['apiPrivateKey']
        
    except json.JSONDecodeError as e:
        logger.error(f"❌ Failed to parse session data JSON: {e}")
        logger.error(f"   Raw decrypted data: {response['Plaintext']}")
        raise ValueError(f"Invalid session data format: {str(e)}")
        
    except Exception as e:
        logger.error(f"❌ Session keys decryption failed: {e}")
        logger.error(f"   Encrypted data length: {len(encrypted_session_key)} chars")
        logger.error(f"   KMS Key ID: {self.key_id}")
        logger.error(f"   Environment: production")
        raise ValueError(f"Session key decryption failed: {str(e)}")
