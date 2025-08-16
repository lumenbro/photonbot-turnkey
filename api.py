import os
from dotenv import load_dotenv
from aiohttp import web
import json
import time
from decimal import Decimal
from stellar_sdk import TransactionEnvelope, Asset, PathPaymentStrictReceive, PathPaymentStrictSend, Payment
import logging
from datetime import datetime, timedelta
import jwt
from services.trade_services import calculate_fee_and_check_balance, get_estimated_xlm_value
from core.stellar import load_account_async
from services.referrals import log_xlm_volume, calculate_referral_shares

logger = logging.getLogger(__name__)

BOT_TOKEN = None
JWT_SECRET = None

async def jwt_middleware(app, handler):
    async def middleware(request):
        # Allow unauthenticated access to Telegram auth endpoint
        if request.path == '/api/auth/telegram':
            return await handler(request)
        token = request.headers.get('Authorization', '').replace('Bearer ', '')
        if not token:
            raise web.HTTPUnauthorized(text='Missing JWT')
        try:
            payload = jwt.decode(token, JWT_SECRET, algorithms=['HS256'])
            request['telegram_id'] = str(payload['telegram_id'])
            logger.debug(f"Decoded telegram_id: {request['telegram_id']}")
        except jwt.InvalidTokenError as e:
            logger.error(f"JWT decoding failed: {str(e)}")
            raise web.HTTPUnauthorized(text='Invalid JWT')
        return await handler(request)
    return middleware

async def api_auth_telegram(request):
    app_context = request.app['app_context']
    data = await request.json()
    telegram_id = str(data.get('id'))
    auth_date = data.get('auth_date')
    hash_val = data.get('hash')
    init_data = data.get('init_data')

    if not BOT_TOKEN:
        raise web.HTTPInternalServerError(text='Bot token not configured')

    if init_data:
        try:
            init_params = dict(param.split('=') for param in init_data.split('&'))
            user_data = json.loads(init_params.get('user', '{}'))
            if str(user_data.get('id')) != telegram_id:
                raise web.HTTPBadRequest(text='Invalid initData user ID')
        except Exception as e:
            logger.error(f"Invalid initData: {str(e)}")
            raise web.HTTPBadRequest(text='Invalid initData')
    elif auth_date:
        logger.warning("Hash validation bypassed for testing")
        if int(time.time()) - auth_date > 86400:
            raise web.HTTPBadRequest(text='Expired auth date')
    else:
        raise web.HTTPBadRequest(text='Missing auth data')

    async with app_context.db_pool.acquire() as conn:
        user = await conn.fetchrow("SELECT telegram_id, public_key FROM users WHERE telegram_id = $1", int(telegram_id))
        if not user:
            if app_context.is_test_mode and callable(app_context.generate_keypair):
                # Only create users in TEST_MODE
                public_key, (encrypted_secret, encrypted_data_key) = await app_context.generate_keypair(telegram_id)
                await conn.execute(
                    "INSERT INTO users (telegram_id, public_key, encrypted_secret, encrypted_data_key) VALUES ($1, $2, $3, $4)",
                    int(telegram_id), public_key, encrypted_secret, encrypted_data_key
                )
                referral_code = f"ref-api-{telegram_id}"
                await conn.execute(
                    "UPDATE users SET referral_code = $1 WHERE telegram_id = $2",
                    referral_code, int(telegram_id)
                )
                logger.info(f"Registered new user {telegram_id} with public_key {public_key} and referral_code {referral_code}")
            else:
                # In production, require user to exist (created via Node.js/Turnkey)
                raise web.HTTPBadRequest(text='User not found. Please register via the mini-app first.')
        else:
            public_key = user['public_key']
            logger.debug(f"Using existing public_key: {public_key} for telegram_id: {telegram_id}")

    token = jwt.encode({
        'telegram_id': telegram_id,
        'exp': datetime.utcnow() + timedelta(hours=24)
    }, JWT_SECRET, algorithm='HS256')

    return web.json_response({'jwt': token})

async def api_check_status(request):
    app_context = request.app['app_context']
    telegram_id = int(request['telegram_id'])

    async with app_context.db_pool.acquire() as conn:
        is_founder = await conn.fetchval("SELECT EXISTS (SELECT 1 FROM founders WHERE telegram_id = $1)", telegram_id)
        has_referrer = await conn.fetchval("SELECT COUNT(*) FROM referrals WHERE referee_id = $1", telegram_id) > 0

    fee_percentage = 0.01  # Default 1% base rate
    if is_founder:
        fee_percentage = 0.001  # 0.1% for founders (pioneers)
    elif has_referrer:
        fee_percentage = 0.009  # 0.9% for referred users

    return web.json_response({'is_founder': is_founder, 'has_referrer': has_referrer, 'fee_percentage': fee_percentage})

async def get_user_authenticator_type(request, telegram_id: int):
    """Determine user authenticator type and signing method, mirroring Node logic where possible."""
    app_context = request.app['app_context']
    async with app_context.db_pool.acquire() as conn:
        user = await conn.fetchrow(
            """
            SELECT 
                u.telegram_id,
                u.kms_encrypted_session_key,
                u.kms_key_id,
                u.temp_api_public_key,
                u.temp_api_private_key,
                u.session_expiry,
                u.source_old_db
            FROM users u
            WHERE u.telegram_id = $1
            """,
            telegram_id,
        )
        wallet_row = await conn.fetchrow(
            """
            SELECT turnkey_sub_org_id, turnkey_key_id, public_key
            FROM turnkey_wallets 
            WHERE telegram_id = $1 AND is_active = TRUE
            """,
            telegram_id,
        )

    if not user:
        raise web.HTTPBadRequest(text='User not found')

    authenticator_type = 'unknown'
    signing_method = 'unknown'
    has_active_session = False

    # KMS session (new users)
    if user['kms_encrypted_session_key'] and user['kms_key_id']:
        authenticator_type = 'session_keys'
        signing_method = 'python_bot_kms'
        has_active_session = True
    # Turnkey wallet present (treat as Telegram Cloud client-side keys or server-side Turnkey)
    elif wallet_row:
        authenticator_type = 'telegram_cloud'
        signing_method = 'python_bot_tg_cloud'
        has_active_session = True
    # Legacy session keys
    elif user['temp_api_public_key'] and user['temp_api_private_key']:
        authenticator_type = 'legacy'
        signing_method = 'python_bot_legacy'
        has_active_session = True
    # Legacy migrated without active session
    elif user['source_old_db']:
        authenticator_type = 'legacy'
        signing_method = 'python_bot_legacy'
        has_active_session = False

    # Session expiry check
    session_expiry = user['session_expiry']
    if has_active_session and session_expiry:
        # Compare using naive UTC
        if session_expiry < datetime.utcnow():
            has_active_session = False
            signing_method = 'session_expired'

    return {
        'authenticator_type': authenticator_type,
        'signing_method': signing_method,
        'has_active_session': has_active_session,
        'turnkey_sub_org_id': wallet_row['turnkey_sub_org_id'] if wallet_row else None,
        'turnkey_key_id': wallet_row['turnkey_key_id'] if wallet_row else None,
    }

def _detect_fee_in_tx(tx_env: TransactionEnvelope, fee_wallet: str) -> Decimal:
    """Scan transaction for a native payment to the fee wallet and return that amount."""
    try:
        for op in tx_env.transaction.operations:
            if isinstance(op, Payment):
                if op.destination == fee_wallet and op.asset.is_native():
                    return Decimal(str(op.amount))
    except Exception:
        pass
    return Decimal('0')

async def _estimate_xlm_volume_from_tx(tx_env: TransactionEnvelope, app_context) -> float:
    """Estimate XLM volume for referrals/fees logging.
    - Payment: native -> amount; non-native -> strict-send price to XLM
    - Path payments: consider both send and dest side; use larger XLM equivalent
    """
    try:
        ops = tx_env.transaction.operations
        if not ops:
            return 0.0
        op = ops[0]
        if isinstance(op, Payment):
            if op.asset.is_native():
                return float(op.amount)
            return await get_estimated_xlm_value(op.asset, float(op.amount), app_context)
        if isinstance(op, PathPaymentStrictSend):
            send_xlm = await (get_estimated_xlm_value(op.send_asset, float(op.send_amount), app_context) if not op.send_asset.is_native() else float(op.send_amount))
            dest_xlm = await (get_estimated_xlm_value(op.dest_asset, float(op.dest_min), app_context) if not op.dest_asset.is_native() else float(op.dest_min))
            return float(max(send_xlm, dest_xlm))
        if isinstance(op, PathPaymentStrictReceive):
            send_xlm = await (get_estimated_xlm_value(op.send_asset, float(op.send_max), app_context) if not op.send_asset.is_native() else float(op.send_max))
            dest_xlm = await (get_estimated_xlm_value(op.dest_asset, float(op.dest_amount), app_context) if not op.dest_asset.is_native() else float(op.dest_amount))
            return float(max(send_xlm, dest_xlm))
    except Exception as e:
        logger.warning(f"Failed to estimate XLM volume from tx: {str(e)}")
    return 0.0

async def api_get_authenticator(request):
    telegram_id = int(request['telegram_id'])
    app_context = request.app['app_context']
    info = await get_user_authenticator_type(request, telegram_id)

    # Resolve active public key: prefer active Turnkey wallet, else users.public_key
    async with app_context.db_pool.acquire() as conn:
        wallet_row = await conn.fetchrow(
            "SELECT public_key FROM turnkey_wallets WHERE telegram_id = $1 AND is_active = TRUE",
            telegram_id,
        )
        if wallet_row and wallet_row['public_key']:
            active_public_key = wallet_row['public_key']
        else:
            user_row = await conn.fetchrow(
                "SELECT public_key FROM users WHERE telegram_id = $1",
                telegram_id,
            )
            active_public_key = user_row['public_key'] if user_row else None

        is_founder = await conn.fetchval(
            "SELECT EXISTS (SELECT 1 FROM founders WHERE telegram_id = $1)", telegram_id
        )
        has_referrer = await conn.fetchval(
            "SELECT COUNT(*) FROM referrals WHERE referee_id = $1", telegram_id
        ) > 0

    fee_percentage = 0.01
    if is_founder:
        fee_percentage = 0.001
    elif has_referrer:
        fee_percentage = 0.009

    return web.json_response({
        'user': {
            'telegram_id': telegram_id,
            'public_key': active_public_key,
        },
        'authenticator': info,
        'fee_status': {
            'is_founder': is_founder,
            'has_referrer': has_referrer,
            'fee_percentage': fee_percentage,
        },
    })

async def api_sign(request):
    app_context = request.app['app_context']
    telegram_id = int(request['telegram_id'])  # Convert to integer
    data = await request.json()
    xdr = data.get('xdr')
    action_type = data.get('action_type', 'payment')
    include_fee = data.get('include_fee', False)

    if not xdr:
        raise web.HTTPBadRequest(text='Missing XDR')

    # Validate/parse XDR
    try:
        tx_env = TransactionEnvelope.from_xdr(xdr, request.app['network_passphrase'])
    except Exception as e:
        logger.error(f"Invalid XDR from user {telegram_id}: {str(e)}")
        raise web.HTTPBadRequest(text='Invalid XDR')

    # Determine authenticator and session
    auth_info = await get_user_authenticator_type(request, telegram_id)
    if not auth_info['has_active_session']:
        return web.json_response({
            'error': 'No active session',
            'signing_method': auth_info['signing_method'],
            'requires_login': True
        }, status=401)

    # Detect fee already included in the transaction
    detected_fee = _detect_fee_in_tx(tx_env, request.app['fee_wallet'])
    fee_amount = float(detected_fee)
    if not include_fee and fee_amount == 0.0:
        logger.debug('include_fee is false and no fee op detected; proceeding without modifying XDR')

    # Compute tx hash (pre-sign)
    try:
        tx_hash_hex = tx_env.hash().hex()
    except Exception:
        tx_hash_hex = f"tx-{int(time.time())}"

    # Estimate XLM volume from transaction contents for logging
    xlm_volume = await _estimate_xlm_volume_from_tx(tx_env, app_context)
    try:
        await log_xlm_volume(telegram_id, xlm_volume, tx_hash_hex, app_context.db_pool)
    except Exception as e:
        logger.warning(f"Failed to log XLM volume: {str(e)}")

    # Sign using the configured signer (Local in TEST_MODE, Turnkey in prod)
    try:
        signed_xdr = await app_context.sign_transaction(telegram_id, xdr)
    except Exception as e:
        logger.error(f"Signing failed for user {telegram_id}: {str(e)}")
        raise web.HTTPInternalServerError(text=f'Signing failed: {str(e)}')

    return web.json_response({
        'success': True,
        'signed_xdr': signed_xdr,
        'hash': tx_hash_hex,
        'fee': fee_amount,
        'signing_method': auth_info['signing_method']
    })

async def start_server(app_context):
    global BOT_TOKEN, JWT_SECRET
    try:
        logger.debug("Loading .env file")
        load_dotenv()
        logger.debug("Loaded .env")
        BOT_TOKEN = os.getenv('BOT_TOKEN')
        JWT_SECRET = os.getenv('JWT_SECRET')
        logger.debug("BOT_TOKEN: %s, JWT_SECRET: %s", BOT_TOKEN, JWT_SECRET)
        if not BOT_TOKEN or not JWT_SECRET:
            logger.error("Missing BOT_TOKEN or JWT_SECRET in .env after loading")
            raise ValueError("Missing BOT_TOKEN or JWT_SECRET in .env")

        web_app = web.Application(middlewares=[jwt_middleware])
        web_app['app_context'] = app_context
        web_app['network_passphrase'] = app_context.network_passphrase
        web_app['fee_wallet'] = app_context.fee_wallet
        web_app['db_pool'] = app_context.db_pool
        web_app['sign_transaction'] = app_context.sign_transaction

        web_app.router.add_post('/api/auth/telegram', api_auth_telegram)
        web_app.router.add_post('/api/check_status', api_check_status)
        web_app.router.add_get('/api/authenticator', api_get_authenticator)
        web_app.router.add_post('/api/sign', api_sign)

        runner = web.AppRunner(web_app)
        await runner.setup()
        site = web.TCPSite(runner, 'localhost', 8080)
        await site.start()
        logger.info("aiohttp server started on http://localhost:8080")
        return runner
    except Exception as e:
        logger.error("Server startup failed: %s", str(e))
        raise