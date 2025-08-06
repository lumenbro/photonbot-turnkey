import aiohttp
import os
from datetime import datetime, timedelta
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.hazmat.primitives import serialization, hashes
from cryptography.hazmat.backends import default_backend
import base64
import json
import uuid
from pyhpke import CipherSuite, kem as KEM, kdf as KDF, aead as AEAD  # Adjusted for lowercase in pyhpke 0.6.2
from base64 import urlsafe_b64encode

TURNKEY_API_URL = "https://api.turnkey.com/public/v1/submit/create_read_write_session"

async def get_turnkey_stamp(body_str, api_public_key, api_private_key):
    private_key_int = int(api_private_key, 16)
    private_key = ec.derive_private_key(private_key_int, ec.SECP256R1(), default_backend())
    signature = private_key.sign(body_str.encode(), ec.ECDSA(hashes.SHA256()))
    stamp_obj = {
        "publicKey": api_public_key,
        "scheme": "SIGNATURE_SCHEME_TK_API_P256",
        "signature": signature.hex()
    }
    stamp_string = json.dumps(stamp_obj, sort_keys=True, separators=(',', ':'))
    return urlsafe_b64encode(stamp_string.encode()).decode()

async def create_or_refresh_session(user_id, app_context, sub_org_id, duration_seconds=31536000):  # 1 year default
    # Fetch or generate turnkey_user_id (v4 UUID)
    async with app_context.db_pool.acquire() as conn:
        turnkey_user_id = await conn.fetchval(
            "SELECT turnkey_user_id FROM users WHERE telegram_id = $1", user_id
        )
        if not turnkey_user_id:
            turnkey_user_id = str(uuid.uuid4())
            await conn.execute(
                "UPDATE users SET turnkey_user_id = $1 WHERE telegram_id = $2",
                turnkey_user_id, user_id
            )

    # Generate temp P-256 keypair for bundle encryption
    private_key = ec.generate_private_key(ec.SECP256R1())
    target_public_key = private_key.public_key().public_bytes(
        serialization.Encoding.X962, serialization.PublicFormat.CompressedPoint
    ).hex()

    payload = {
        "type": "ACTIVITY_TYPE_CREATE_READ_WRITE_SESSION_V2",
        "timestampMs": str(int(datetime.now().timestamp() * 1000)),
        "organizationId": sub_org_id,  # Sub-org for user
        "parameters": {
            "targetPublicKey": target_public_key,
            "userId": turnkey_user_id,  # Use v4 UUID
            "apiKeyName": f"BotSession-User{user_id}",
            "expirationSeconds": str(duration_seconds),
            "invalidateExisting": False  # Keep old if refreshing; set True for full reset
        }
    }

    body_str = json.dumps(payload, separators=(',', ':'), sort_keys=True)
    stamp = await get_turnkey_stamp(body_str, os.getenv('TURNKEY_API_PUBLIC_KEY'), os.getenv('TURNKEY_API_PRIVATE_KEY'))
    headers = {
        "Content-Type": "application/json",
        "Accept": "application/json",
        "X-Stamp": stamp,
        "X-Idempotency-Key": str(uuid.uuid4())
    }

    async with aiohttp.ClientSession() as session:
        async with session.post(TURNKEY_API_URL, json=payload, headers=headers) as resp:
            if resp.status != 200:
                raise ValueError(f"Session failed: {await resp.text()}")
            data = await resp.json()
            activity = data['activity']
            session_id = activity['result']['createReadWriteSessionResultV2']['apiKeyId']
            credential_bundle = activity['result']['createReadWriteSessionResultV2']['credentialBundle']
            expiry = datetime.now() + timedelta(seconds=duration_seconds)

            # Decrypt bundle (real HPKE)
            temp_public, temp_private = await decrypt_credential_bundle(credential_bundle, private_key)

            # Store temp API keys and expiry in DB (no bundle needed after decryption)
            async with app_context.db_pool.acquire() as conn:
                await conn.execute(
                    "UPDATE users SET turnkey_session_id = $1, temp_api_public_key = $2, temp_api_private_key = $3, session_expiry = $4 WHERE telegram_id = $5",
                    session_id, temp_public, temp_private, expiry, user_id
                )

            return session_id, temp_public, temp_private

async def decrypt_credential_bundle(bundle, private_key):
    suite = CipherSuite(kem=KEM.P256_HKDF_SHA256, kdf=KDF.HKDF_SHA256, aead=AEAD.AES_128_GCM)
    bundle_data = json.loads(base64.b64decode(bundle))  # Bundle is base64 JSON with 'encapsulatedPublic', 'ciphertext'
    skR = suite.kem.deserialize_private_key(private_key.private_bytes(encoding=serialization.Encoding.DER, format=serialization.PrivateFormat.PKCS8, encryption_algorithm=serialization.NoEncryption()))
    encapsulated_public = base64.b64decode(bundle_data['encapsulatedPublic'])
    ciphertext = base64.b64decode(bundle_data['ciphertext'])
    decrypted = suite.open(ciphertext, skR, encapsulated_public, info=b"turnkey session")
    data = json.loads(decrypted.decode())
    return data['publicKey'], data['privateKey']

# Check/refresh if expired
async def get_valid_session(user_id, app_context, sub_org_id):
    async with app_context.db_pool.acquire() as conn:
        row = await conn.fetchrow("SELECT temp_api_public_key, temp_api_private_key, session_expiry FROM users WHERE telegram_id = $1", user_id)
        if row and row['session_expiry'] is not None and row['session_expiry'] > datetime.now():
            return row['temp_api_public_key'], row['temp_api_private_key']
        else:
            # Refresh
            session_id, temp_public, temp_private = await create_or_refresh_session(user_id, app_context, sub_org_id)
            return temp_public, temp_private
