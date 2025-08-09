# Instructions for Composer Agent: Fix Python KMS Session Key Decryption

## Issue Description
The Python Telegram bot is failing to decrypt session keys created by the Node.js backend, causing transaction signing to fail with this error:
```
AttributeError: 'KMSService' object has no attribute 'decrypt_session_keys'
```

## What Needs to Be Done
**File to modify**: `services/kms_service.py`

**Action**: Add a new method called `decrypt_session_keys()` (plural) to the existing KMSService class.

## Why This Is Needed
1. **Method name mismatch**: The Python bot code calls `decrypt_session_keys()` but only `decrypt_session_key()` exists
2. **Data format compatibility**: Node.js stores session keys as JSON, Python needs to parse this format
3. **Environment context**: Must use matching KMS encryption context between Node.js and Python

## Exact Code to Add
Add this method to the KMSService class in `services/kms_service.py`:

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
        
        logger.info(f"✅ Successfully decrypted session keys")
        
        # Return in the format Python expects: (public_key, private_key)
        return session_data['apiPublicKey'], session_data['apiPrivateKey']
        
    except Exception as e:
        logger.error(f"❌ Session keys decryption failed: {e}")
        raise ValueError(f"Session key decryption failed: {str(e)}")
```

## Where to Place It
- **Location**: Inside the KMSService class
- **Position**: After the existing `decrypt_session_key()` method
- **Indentation**: Same level as other class methods

## What This Fixes
- ✅ Adds the missing `decrypt_session_keys()` method that the bot is trying to call
- ✅ Handles JSON format from Node.js session creation
- ✅ Uses correct KMS encryption context ('production' environment)
- ✅ Returns tuple format that Python bot expects: (public_key, private_key)
- ✅ Includes proper error handling and logging

## Testing After Implementation
1. Start the Python bot
2. Try to make a transaction (buy/sell)
3. Check logs for "✅ Successfully decrypted session keys"
4. Verify transaction signing works without the AttributeError

## Technical Background
- **Node.js**: Creates sessions with `encryptSessionKeys()`, stores as JSON: `{"apiPublicKey": "...", "apiPrivateKey": "..."}`
- **Python**: Calls `decrypt_session_keys()`, expects tuple: `(public_key, private_key)`
- **KMS**: Both use same key ID and service context for encryption/decryption

This is a simple method addition that bridges the format difference between Node.js session creation and Python session usage without changing any existing functionality.
