# Python KMS Service Fix for Session Key Decryption

## Problem Summary
The Python bot is failing with this error:
```
AttributeError: 'KMSService' object has no attribute 'decrypt_session_keys'. Did you mean: 'decrypt_session_key'?
```

## Root Cause
1. **Method name mismatch**: Python bot calls `decrypt_session_keys()` (plural) but KMSService only has `decrypt_session_key()` (singular)
2. **Data format mismatch**: Node.js stores session keys as JSON, Python expects different format
3. **Environment context mismatch**: Different encryption contexts between Node.js and Python

## Required Changes
**File to modify**: `services/kms_service.py`

### Add this method to the KMSService class:

```python
def decrypt_session_keys(self, encrypted_session_key):
    """Decrypt session keys using KMS - matches Node.js format"""
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
        
        # Parse the JSON data (Node.js stores session keys as JSON)
        decrypted_json = response['Plaintext'].decode('utf-8')
        session_data = json.loads(decrypted_json)
        
        logger.info(f"✅ Successfully decrypted session keys for environment: production")
        
        # Return in the format Python expects: (public_key, private_key)
        return session_data['apiPublicKey'], session_data['apiPrivateKey']
        
    except Exception as e:
        logger.error(f"❌ Session keys decryption failed: {e}")
        logger.error(f"   Encrypted data length: {len(encrypted_session_key)}")
        logger.error(f"   KMS Key ID: {self.key_id}")
        raise ValueError(f"Session key decryption failed: {str(e)}")
```

### Optional: Update existing encrypt method for consistency:

```python
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
                'Environment': 'production'
            }
        )
        
        logger.info(f"✅ Successfully encrypted session keys")
        return base64.b64encode(response['CiphertextBlob']).decode('utf-8')
        
    except Exception as e:
        logger.error(f"❌ Session keys encryption failed: {e}")
        raise
```

## Technical Details

### Data Format Explanation
- **Node.js stores**: `{"apiPublicKey": "02abc...", "apiPrivateKey": "def123..."}`
- **Python expects**: `(public_key, private_key)` tuple
- **Solution**: Parse JSON and return tuple

### Environment Context
Both Node.js and Python must use the same encryption context:
```python
EncryptionContext={
    'Service': 'lumenbro-session-keys',
    'Environment': 'production'
}
```

### Error Handling
The new method includes comprehensive logging to help debug any remaining issues.

## Testing After Fix
1. Try a transaction in the Python bot
2. Check logs for "✅ Successfully decrypted session keys"
3. Verify transaction signing works

## Verification Commands
After applying the fix, you can verify with:
```python
# In Python bot console/debug
try:
    public, private = kms_service.decrypt_session_keys(encrypted_data)
    print(f"✅ Decryption successful: {public[:10]}...")
except Exception as e:
    print(f"❌ Still failing: {e}")
```

## Why This Approach
- **Minimal changes**: Only adds missing method, doesn't break existing code
- **Backward compatible**: Existing `decrypt_session_key()` still works
- **Format agnostic**: Handles JSON from Node.js, returns what Python expects
- **Robust error handling**: Clear error messages for debugging

This fix ensures compatibility between the Node.js session creation and Python session usage while maintaining all existing functionality.
