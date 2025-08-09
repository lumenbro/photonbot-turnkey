# main.py (modified for single DB, Turnkey integration, session-based signing)
from services.kms_service import KMSService
import asyncio
import asyncpg
from aiogram import Bot, Dispatcher
from aiogram.fsm.storage.memory import MemoryStorage
from globals import AppContext, TELEGRAM_TOKEN
from services.streaming import StreamingService
from services.referrals import daily_payout
from handlers.referrals import register_referral_handlers
from core.stellar import load_public_key
from handlers.main_menu import register_main_handlers
from handlers.copy_trading import register_copy_handlers
from handlers.walletmanagement import register_wallet_management_handlers
from handlers.wallet_commands import register_wallet_commands
from api import start_server
import logging
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo
import os
from stellar_sdk import Keypair, TransactionEnvelope, Network
from stellar_sdk.utils import sha256
from stellar_sdk.decorated_signature import DecoratedSignature
from stellar_sdk.strkey import StrKey
import aiohttp
import base64
import uuid
import json
import time
import ssl
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.backends import default_backend
from base64 import urlsafe_b64encode
from dotenv import load_dotenv

# Configure logging
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# Load environment variables
load_dotenv()
TURNKEY_API_PUBLIC_KEY = os.getenv('TURNKEY_API_PUBLIC_KEY')
TURNKEY_API_PRIVATE_KEY = os.getenv('TURNKEY_API_PRIVATE_KEY')
TURNKEY_ORG_ID = os.getenv('TURNKEY_ORGANIZATION_ID')
TURNKEY_DISBURSEMENT_WALLET_ID = os.getenv('TURNKEY_DISBURSEMENT_WALLET_ID')
ADMIN_TELEGRAM_ID = int(os.getenv("ADMIN_TELEGRAM_ID", "5014800072"))
if not all([TURNKEY_API_PUBLIC_KEY, TURNKEY_API_PRIVATE_KEY, TURNKEY_ORG_ID, TURNKEY_DISBURSEMENT_WALLET_ID]):
    raise ValueError("Missing Turnkey environment variables in .env")

# Log initial environment details
logger.info(f"Current working directory: {os.getcwd()}")
logger.info(f"FEE_WALLET from os.getenv at startup: {os.getenv('FEE_WALLET')}")

class TurnkeySigner:
    def __init__(self, app_context):
        self.app_context = app_context
        self.client = app_context.client
        self.root_api_public_key = TURNKEY_API_PUBLIC_KEY
        self.root_api_private_key = TURNKEY_API_PRIVATE_KEY
        self.turnkey_org_id = TURNKEY_ORG_ID
        self.turnkey_disbursement_wallet_id = TURNKEY_DISBURSEMENT_WALLET_ID
        self.horizon_url = "https://horizon.stellar.org"

    async def get_stamp(self, body_str, api_public_key, api_private_key):
        def sync_get_stamp():
            try:
                private_key_int = int(api_private_key, 16)
                private_key = ec.derive_private_key(private_key_int, ec.SECP256R1(), default_backend())
                signature = private_key.sign(body_str.encode(), ec.ECDSA(hashes.SHA256()))
                stamp_obj = {
                    "publicKey": api_public_key,
                    "scheme": "SIGNATURE_SCHEME_TK_API_P256",
                    "signature": signature.hex()
                }
                stamp_string = json.dumps(stamp_obj, sort_keys=True, separators=(',', ':'))  # Canonical JSON
                stamp = urlsafe_b64encode(stamp_string.encode()).decode()  # Keep padding if present
                return stamp
            except Exception as e:
                raise ValueError(f"Failed to generate stamp: {str(e)}")

        return await asyncio.to_thread(sync_get_stamp)

    async def sign_transaction(self, telegram_id, transaction_xdr):
        """Sign a Stellar transaction using the active Turnkey session."""
        if telegram_id == self.app_context.fee_telegram_id:
            # Use root org and disbursement wallet ID for fee wallet
            sub_org_id = self.turnkey_org_id
            sign_with = os.getenv("DISBURSEMENT_WALLET")  # Use address for signWith
            public_key = os.getenv("DISBURSEMENT_WALLET")
            temp_public = self.root_api_public_key
            temp_private = self.root_api_private_key
        else:
            # Use WalletManager to get active wallet
            from services.wallet_manager import WalletManager
            wallet_manager = WalletManager(self.app_context.db_pool)
            active_wallet = await wallet_manager.get_active_wallet(telegram_id)
            
            if not active_wallet:
                raise ValueError(f"No active wallet found for telegram_id {telegram_id}. Create one via Node.js backend.")
            
            # For legacy users, get session data from users table
            # For new users, get wallet data from turnkey_wallets table
            async with self.app_context.db_pool.acquire() as conn:
                # Check if legacy user
                is_legacy = await wallet_manager.is_legacy_user(telegram_id)
                
                if is_legacy:
                    # Legacy user - but check if they have a Turnkey wallet first
                    wallet_data = await conn.fetchrow(
                        "SELECT turnkey_sub_org_id, turnkey_key_id, public_key FROM turnkey_wallets WHERE telegram_id = $1 AND is_active = TRUE",
                        int(telegram_id)
                    )
                    
                    if wallet_data:
                        # Legacy user with Turnkey wallet - use Turnkey wallet (mixed state user)
                        sub_org_id = wallet_data["turnkey_sub_org_id"]
                        sign_with = wallet_data["public_key"]
                        public_key = wallet_data["public_key"]
                        logger.info(f"Legacy user {telegram_id} using Turnkey wallet: org_id={sub_org_id}")
                    else:
                        # Pure legacy user - use users table for session data
                        public_key = active_wallet
                        sign_with = active_wallet
                        # Get sub_org_id from users table or use default
                        user_data = await conn.fetchrow(
                            "SELECT turnkey_session_id FROM users WHERE telegram_id = $1",
                            telegram_id
                        )
                        sub_org_id = user_data["turnkey_session_id"] if user_data and user_data["turnkey_session_id"] else self.turnkey_org_id
                        logger.info(f"Pure legacy user {telegram_id} using session: org_id={sub_org_id}")
                else:
                    # New user - use turnkey_wallets table
                    wallet_data = await conn.fetchrow(
                        "SELECT turnkey_sub_org_id, turnkey_key_id, public_key FROM turnkey_wallets WHERE telegram_id = $1 AND is_active = TRUE",
                        int(telegram_id)
                    )
                    if not wallet_data:
                        raise ValueError(f"No active Turnkey wallet found for telegram_id {telegram_id}. Create one via Node.js backend.")
                    sub_org_id = wallet_data["turnkey_sub_org_id"]
                    sign_with = wallet_data["public_key"]
                    public_key = wallet_data["public_key"]

                # Try KMS format first, fall back to legacy format
                session_data = await conn.fetchrow(
                    """
                    SELECT 
                        kms_encrypted_session_key,
                        kms_key_id,
                        temp_api_public_key,
                        temp_api_private_key,
                        session_expiry 
                    FROM users WHERE telegram_id = $1
                    """,
                    int(telegram_id)
                )
                if not session_data:
                    raise ValueError(f"Session data missing for telegram_id {telegram_id}. Recreate session via Node.js backend.")
                
                # Make session_expiry timezone-aware (assuming stored as UTC naive)
                session_expiry = session_data["session_expiry"]
                if session_expiry.tzinfo is None:
                    session_expiry = session_expiry.replace(tzinfo=timezone.utc)
                
                if session_expiry < datetime.now(timezone.utc):
                    raise ValueError(f"Session expired for telegram_id {telegram_id}. Recreate session via Node.js backend.")
                
                # Check if KMS session exists, otherwise use legacy
                if session_data["kms_encrypted_session_key"] and session_data["kms_key_id"]:
                    logger.info(f"Using KMS session for telegram_id {telegram_id}")
                    # Decrypt session keys using KMS
                    temp_public, temp_private = self.app_context.kms_service.decrypt_session_keys(session_data["kms_encrypted_session_key"])
                else:
                    logger.info(f"Using legacy session for telegram_id {telegram_id}")
                    # Use legacy unencrypted session keys
                    temp_public = session_data["temp_api_public_key"]
                    temp_private = session_data["temp_api_private_key"]

        try:
            tx_envelope = TransactionEnvelope.from_xdr(transaction_xdr, Network.PUBLIC_NETWORK_PASSPHRASE)  # Adjust network if mainnet
        except Exception as e:
            logger.error(f"Failed to parse transaction XDR: {str(e)}")
            raise ValueError(f"Invalid transaction XDR: {str(e)}")

        tx_hash = tx_envelope.hash()
        tx_hash_hex = tx_hash.hex()
        logger.debug(f"Transaction Hash (hex): {tx_hash_hex}")

        body = {
            "type": "ACTIVITY_TYPE_SIGN_RAW_PAYLOAD_V2",
            "timestampMs": str(int(time.time() * 1000)),
            "organizationId": sub_org_id,
            "parameters": {
                "signWith": sign_with,
                "payload": tx_hash_hex,
                "encoding": "PAYLOAD_ENCODING_HEXADECIMAL",
                "hashFunction": "HASH_FUNCTION_NOT_APPLICABLE"
            }
        }
        body_str = json.dumps(body, separators=(',', ':'), sort_keys=True)
        stamp = await self.get_stamp(body_str, temp_public, temp_private)
        headers = {
            "Content-Type": "application/json",
            "X-Stamp": stamp
        }
        async with self.client.post(
            "https://api.turnkey.com/public/v1/submit/sign_raw_payload",
            headers=headers,
            data=body_str
        ) as response:
            response_text = await response.text()
            logger.info(f"Turnkey Response: {response.status}, {response_text}")
            if response.status != 200:
                raise ValueError(f"Turnkey signing failed: {response.status}, {response_text}")
            data = await response.json()
            r = data["activity"]["result"]["signRawPayloadResult"]["r"]
            s = data["activity"]["result"]["signRawPayloadResult"]["s"]
            hex_signature = r + s
            if len(hex_signature) != 128 or not all(c in "0123456789abcdefABCDEF" for c in hex_signature.lower()):
                raise ValueError(f"Invalid signature: {hex_signature}")
            signature_bytes = bytes.fromhex(hex_signature)

        keypair = Keypair.from_public_key(public_key)
        try:
            keypair.verify(tx_hash, signature_bytes)
            logger.debug("Signature verification: True")
        except Exception as e:
            logger.debug(f"Signature verification: False - {str(e)}")
            raise

        hint = keypair.signature_hint()  # bytes
        decorated_signature = DecoratedSignature(signature_hint=hint, signature=signature_bytes)
        tx_envelope.signatures.append(decorated_signature)

        try:
            signed_xdr = tx_envelope.to_xdr()
            logger.debug(f"Signed XDR: {signed_xdr}")
            return signed_xdr
        except Exception as e:
            logger.error(f"XDR serialization failed: {str(e)}")
            raise ValueError(f"XDR serialization failed: {str(e)}")

async def init_db_pool():
    ssl_context = ssl.create_default_context(cafile='/home/ubuntu/photonbot-live/global-bundle.pem')
    ssl_context.check_hostname = True
    ssl_context.verify_mode = ssl.CERT_REQUIRED
    pool = await asyncpg.create_pool(
        user=os.getenv('DB_USER', 'botadmin'),
        password=os.getenv('DB_PASSWORD'),
        database=os.getenv('DB_NAME', 'postgres'),
        host=os.getenv('DB_HOST', 'lumenbro-turnkey.cz2imkksk7b4.us-west-1.rds.amazonaws.com'),
        port=int(os.getenv('DB_PORT', 5434)),
        ssl=ssl_context  # Pass SSLContext, not sslrootcert
    )
    async with pool.acquire() as conn:
        # Create schema (idempotent)
        await conn.execute("""
            CREATE TABLE IF NOT EXISTS users (
                telegram_id BIGINT PRIMARY KEY,
                public_key TEXT,
                referral_code TEXT,
                turnkey_user_id TEXT,
                turnkey_session_id TEXT,
                temp_api_public_key TEXT,
                temp_api_private_key TEXT,
                session_expiry TIMESTAMP,
                kms_encrypted_session_key TEXT,
                kms_key_id TEXT,
                user_email TEXT,
                session_created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                source_old_db TEXT,
                encrypted_s_address_secret TEXT,
                migration_date TIMESTAMP,
                pioneer_status BOOLEAN DEFAULT FALSE,
                migration_notified BOOLEAN DEFAULT FALSE,
                migration_notified_at TIMESTAMP
            );
            
            -- Add session_created_at column if it doesn't exist (for existing tables)
            DO $$
            BEGIN
                IF NOT EXISTS (
                    SELECT 1 FROM information_schema.columns 
                    WHERE table_name = 'users' AND column_name = 'session_created_at'
                ) THEN
                    ALTER TABLE users ADD COLUMN session_created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP;
                END IF;
            END $$;
            
            -- Add migration-related columns if they don't exist (for existing tables)
            DO $$
            BEGIN
                IF NOT EXISTS (
                    SELECT 1 FROM information_schema.columns 
                    WHERE table_name = 'users' AND column_name = 'source_old_db'
                ) THEN
                    ALTER TABLE users ADD COLUMN source_old_db TEXT;
                END IF;
                
                IF NOT EXISTS (
                    SELECT 1 FROM information_schema.columns 
                    WHERE table_name = 'users' AND column_name = 'encrypted_s_address_secret'
                ) THEN
                    ALTER TABLE users ADD COLUMN encrypted_s_address_secret TEXT;
                END IF;
                
                IF NOT EXISTS (
                    SELECT 1 FROM information_schema.columns 
                    WHERE table_name = 'users' AND column_name = 'migration_date'
                ) THEN
                    ALTER TABLE users ADD COLUMN migration_date TIMESTAMP;
                END IF;
                
                IF NOT EXISTS (
                    SELECT 1 FROM information_schema.columns 
                    WHERE table_name = 'users' AND column_name = 'pioneer_status'
                ) THEN
                    ALTER TABLE users ADD COLUMN pioneer_status BOOLEAN DEFAULT FALSE;
                END IF;
                
                IF NOT EXISTS (
                    SELECT 1 FROM information_schema.columns 
                    WHERE table_name = 'users' AND column_name = 'migration_notified'
                ) THEN
                    ALTER TABLE users ADD COLUMN migration_notified BOOLEAN DEFAULT FALSE;
                END IF;
                
                IF NOT EXISTS (
                    SELECT 1 FROM information_schema.columns 
                    WHERE table_name = 'users' AND column_name = 'migration_notified_at'
                ) THEN
                    ALTER TABLE users ADD COLUMN migration_notified_at TIMESTAMP;
                END IF;
                
                IF NOT EXISTS (
                    SELECT 1 FROM information_schema.columns 
                    WHERE table_name = 'users' AND column_name = 'legacy_public_key'
                ) THEN
                    ALTER TABLE users ADD COLUMN legacy_public_key TEXT;
                END IF;
                
                -- Add recovery support columns
                IF NOT EXISTS (
                    SELECT 1 FROM information_schema.columns 
                    WHERE table_name = 'users' AND column_name = 'recovery_mode'
                ) THEN
                    ALTER TABLE users ADD COLUMN recovery_mode BOOLEAN DEFAULT FALSE;
                END IF;
                
                IF NOT EXISTS (
                    SELECT 1 FROM information_schema.columns 
                    WHERE table_name = 'users' AND column_name = 'recovery_org_id'
                ) THEN
                    ALTER TABLE users ADD COLUMN recovery_org_id TEXT;
                END IF;
                
                IF NOT EXISTS (
                    SELECT 1 FROM information_schema.columns 
                    WHERE table_name = 'users' AND column_name = 'recovery_session_expires'
                ) THEN
                    ALTER TABLE users ADD COLUMN recovery_session_expires TIMESTAMP;
                END IF;
                
                IF NOT EXISTS (
                    SELECT 1 FROM information_schema.columns 
                    WHERE table_name = 'users' AND column_name = 'recovery_api_key_id'
                ) THEN
                    ALTER TABLE users ADD COLUMN recovery_api_key_id TEXT;
                END IF;
            END $$;
            
            -- Populate legacy_public_key for existing migrated users
            UPDATE users 
            SET legacy_public_key = public_key 
            WHERE source_old_db IS NOT NULL 
            AND encrypted_s_address_secret IS NOT NULL
            AND (legacy_public_key IS NULL OR legacy_public_key = '')
            AND public_key IS NOT NULL;
            CREATE TABLE IF NOT EXISTS turnkey_wallets (
                id BIGSERIAL PRIMARY KEY,
                telegram_id BIGINT REFERENCES users(telegram_id) ON DELETE CASCADE,
                turnkey_sub_org_id TEXT NOT NULL,
                turnkey_key_id TEXT NOT NULL,
                public_key TEXT NOT NULL UNIQUE,
                is_active BOOLEAN DEFAULT TRUE,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                UNIQUE(telegram_id, turnkey_key_id)
            );
            CREATE TABLE IF NOT EXISTS referrals (
                referrer_id BIGINT REFERENCES users(telegram_id),
                referee_id BIGINT REFERENCES users(telegram_id),
                PRIMARY KEY (referrer_id, referee_id)
            );
            CREATE TABLE IF NOT EXISTS founders (
                telegram_id BIGINT PRIMARY KEY REFERENCES users(telegram_id)
            );
            CREATE TABLE IF NOT EXISTS trades (
                id BIGSERIAL PRIMARY KEY,
                user_id BIGINT REFERENCES users(telegram_id),
                xlm_volume DOUBLE PRECISION,
                tx_hash TEXT,
                timestamp TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            );
            CREATE TABLE IF NOT EXISTS rewards (
                id BIGSERIAL PRIMARY KEY,
                user_id BIGINT REFERENCES users(telegram_id),
                amount DOUBLE PRECISION NOT NULL,
                status TEXT DEFAULT 'unpaid',
                paid_at TIMESTAMP
            );
            CREATE TABLE IF NOT EXISTS copy_trading (
                id BIGSERIAL PRIMARY KEY,
                user_id BIGINT REFERENCES users(telegram_id),
                wallet_address TEXT NOT NULL,
                status TEXT DEFAULT 'active',
                multiplier DOUBLE PRECISION DEFAULT 1.0,
                fixed_amount DOUBLE PRECISION,
                slippage DOUBLE PRECISION DEFAULT 0.01
            );
        """)
    return pool

async def shutdown(app_context, streaming_service):
    logger.info("Initiating shutdown...")
    if hasattr(app_context, 'price_service'):
        await app_context.price_service.shutdown()
    if streaming_service:
        for chat_id in list(streaming_service.tasks.keys()):
            try:
                await streaming_service.stop_streaming(chat_id)
            except Exception as e:
                logger.warning(f"Failed to stop streaming for chat_id {chat_id}: {str(e)}")
    for task in app_context.tasks:
        if not task.done():
            task.cancel()
    await asyncio.gather(*app_context.tasks, return_exceptions=True)
    await app_context.shutdown()
    if app_context.bot:
        await app_context.bot.session.close()
    logger.info("Bot stopped gracefully.")

async def schedule_daily_payout(app_context, streaming_service, chat_id=None):
    if chat_id is None:
        admin_id = os.getenv("ADMIN_TELEGRAM_ID")
        if admin_id is None:
            logger.error("ADMIN_TELEGRAM_ID not set in environment variables")
            return
        try:
            chat_id = int(admin_id)
        except ValueError:
            logger.error(f"Invalid ADMIN_TELEGRAM_ID: {admin_id} (must be an integer)")
            return

    while not app_context.shutdown_flag.is_set():
        now = datetime.now(ZoneInfo("UTC"))
        next_run = now.replace(hour=0, minute=0, second=0, microsecond=0) + timedelta(days=1)
        logger.info("Next payout scheduled for %s UTC", next_run)
        await asyncio.sleep((next_run - now).total_seconds())
        logger.info("Running daily payout at %s UTC", datetime.now(ZoneInfo("UTC")))
        try:
            await daily_payout(app_context.db_pool, app_context.db_pool, app_context.bot, chat_id, app_context)
        except Exception as e:
            logger.error(f"Daily payout failed: {str(e)}", exc_info=True)
            if chat_id:
                await app_context.bot.send_message(chat_id, f"Daily payout failed: {str(e)}")

async def setup_fee_wallet(app_context):
    disbursement_wallet_public = os.getenv("DISBURSEMENT_WALLET")
    if not disbursement_wallet_public:
        logger.error("DISBURSEMENT_WALLET not found in .env")
        raise ValueError("DISBURSEMENT_WALLET not found in .env")

    fee_public_key = disbursement_wallet_public
    fee_telegram_id = -1

    if not app_context.fee_wallet:
        logger.error("FEE_WALLET not found in .env")
        raise ValueError("FEE_WALLET not found in .env")
    try:
        Keypair.from_public_key(app_context.fee_wallet)
    except Exception:
        raise ValueError("Invalid FEE_WALLET address")

    async with app_context.db_pool.acquire() as conn:
        exists = await conn.fetchval(
            "SELECT telegram_id FROM users WHERE telegram_id = $1", fee_telegram_id
        )
        if exists:
            await conn.execute(
                "UPDATE users SET public_key = $1 WHERE telegram_id = $2",
                fee_public_key,
                fee_telegram_id
            )
            logger.info(f"Updated fee wallet with telegram_id {fee_telegram_id}")
        else:
            await conn.execute(
                "INSERT INTO users (telegram_id, public_key) "
                "VALUES ($1, $2)",
                fee_telegram_id,
                fee_public_key
            )
            logger.info(f"Inserted fee wallet with telegram_id {fee_telegram_id}")

    app_context.fee_telegram_id = fee_telegram_id

async def run_master():
    db_pool = await init_db_pool()

    app_context = AppContext(db_pool=db_pool)
    app_context.bot = Bot(token=TELEGRAM_TOKEN)
    app_context.client = aiohttp.ClientSession()
    storage = MemoryStorage()
    app_context.dp = Dispatcher(storage=storage)

    from services.price_service import PriceService
    app_context.price_service = PriceService(app_context)

    app_context.fee_wallet = os.getenv("FEE_WALLET")
    logger.info(f"Loaded FEE_WALLET into app_context: {app_context.fee_wallet}")
    if not app_context.fee_wallet:
        raise ValueError("FEE_WALLET not found in .env")
    try:
        Keypair.from_public_key(app_context.fee_wallet)
    except Exception:
        raise ValueError("Invalid FEE_WALLET address")

    await setup_fee_wallet(app_context)

    app_context.turnkey_signer = TurnkeySigner(app_context)
    app_context.kms_service = KMSService()

    async def wrapped_sign_transaction(telegram_id, transaction_xdr):
        return await app_context.turnkey_signer.sign_transaction(telegram_id, transaction_xdr)
    app_context.sign_transaction = wrapped_sign_transaction
    app_context.transaction_signer = wrapped_sign_transaction

    async def wrapped_load_public_key(telegram_id):
        return await load_public_key(app_context, telegram_id)
    app_context.load_public_key = wrapped_load_public_key

    app_context.slippage = 0.05
    app_context.shutdown_flag = asyncio.Event()
    app_context.tasks = []

    streaming_service = StreamingService(app_context)
    register_main_handlers(app_context.dp, app_context, streaming_service)
    register_copy_handlers(dp=app_context.dp, streaming_service=streaming_service, app_context=app_context)
    register_referral_handlers(app_context.dp, app_context)
    register_wallet_management_handlers(app_context.dp, app_context)
    register_wallet_commands(app_context.dp, app_context)
    
    # Register recovery commands
    from handlers.recovery import register_recovery_handlers
    register_recovery_handlers(app_context.dp, app_context)

    await app_context.bot.delete_webhook(drop_pending_updates=True)
    logger.info("Dropped pending updates to prevent stale command processing")

    max_retries = float('inf')
    retry_delay = 1
    max_delay = 60
    retry_count = 0

    app_context.tasks.append(asyncio.create_task(schedule_daily_payout(app_context, streaming_service, chat_id=ADMIN_TELEGRAM_ID)))
    app_context.tasks.append(asyncio.create_task(start_server(app_context)))

    while retry_count < max_retries:
        try:
            await app_context.dp.start_polling(app_context.bot)
            break
        except Exception as e:
            logger.error(f"Polling failed: {str(e)}")
            retry_count += 1
            delay = min(retry_delay * (2 ** retry_count), max_delay)
            logger.warning(f"Retrying in {delay} seconds (attempt {retry_count})...")
            await asyncio.sleep(delay)
        except (KeyboardInterrupt, asyncio.CancelledError):
            await shutdown(app_context, streaming_service)
            logger.info("Bot stopped gracefully.")
            break

if __name__ == "__main__":
    asyncio.run(run_master())
