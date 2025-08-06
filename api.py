import os
from dotenv import load_dotenv
from aiohttp import web
import json
import time
from decimal import Decimal
from stellar_sdk import TransactionEnvelope, TransactionBuilder, Asset, PathPaymentStrictReceive, PathPaymentStrictSend
import logging
from datetime import datetime, timedelta
import jwt
from services.trade_services import calculate_fee_and_check_balance, get_estimated_xlm_value
from core.stellar import load_account_async
from services.referrals import log_xlm_volume, calculate_referral_shares

logger = logging.getLogger(__name__)

BOT_TOKEN = None
JWT_SECRET = None
_tx_counter = 0  # Global counter for mock tx_hash

async def jwt_middleware(app, handler):
    async def middleware(request):
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

async def api_sign(request):
    global _tx_counter
    app_context = request.app['app_context']
    telegram_id = int(request['telegram_id'])  # Convert to integer
    data = await request.json()
    xdr = data.get('xdr')
    action_type = data.get('action_type')
    amount = Decimal(str(data.get('amount', 0)))
    include_fee = data.get('include_fee', False)  # Default to False for external use

    # Parse asset from XDR (simplified for mock testing)
    send_asset = Asset.native()  # Default to XLM; adjust if XDR provides asset info
    try:
        tx = TransactionEnvelope.from_xdr(xdr, request.app['network_passphrase'])
        if tx.transaction.operations:
            op = tx.transaction.operations[0]
            if isinstance(op, (PathPaymentStrictReceive, PathPaymentStrictSend)):
                send_asset = op.send_asset
    except Exception as e:
        logger.warning(f"Could not parse asset from XDR, defaulting to XLM: {str(e)}")

    # Bypass XDR validation for mock testing
    if xdr == "mock-xdr" or xdr.startswith("AAAA"):
        logger.warning("Using mock or minimal XDR for testing")
    else:
        try:
            tx = TransactionEnvelope.from_xdr(xdr, request.app['network_passphrase'])
        except Exception as e:
            logger.error(f"Invalid XDR from user {telegram_id}: {str(e)}")
            raise web.HTTPBadRequest(text='Invalid XDR')

    # Fetch status for fee adjustment
    async def get_status(telegram_id):
        async with app_context.db_pool.acquire() as conn:
            is_founder = await conn.fetchval("SELECT EXISTS (SELECT 1 FROM founders WHERE telegram_id = $1)", telegram_id)
            has_referrer = await conn.fetchval("SELECT COUNT(*) FROM referrals WHERE referee_id = $1", telegram_id) > 0
        fee_percentage = 0.01  # Default 1% base rate
        if is_founder:
            fee_percentage = 0.001  # 0.1% for founders (pioneers)
        elif has_referrer:
            fee_percentage = 0.009  # 0.9% for referred users
        return {'is_founder': is_founder, 'has_referrer': has_referrer, 'fee_percentage': fee_percentage}

    status_data = await get_status(telegram_id)
    fee_percentage = status_data.get('fee_percentage', 0.01)  # Default to 1% if missing
    xlm_volume = float(amount) if send_asset.is_native() else await get_estimated_xlm_value(send_asset, float(amount), app_context)
    fee = Decimal('0.0')
    transaction = None
    if include_fee:
        # Calculate fee with adjusted percentage (simplified to 1% base rate for now)
        fee = Decimal(str(round(0.01 * xlm_volume, 7)))  # 1% base rate
        # Skip Horizon call for mock testing; assume balance is sufficient
        transaction = TransactionBuilder.from_xdr(xdr, request.app['network_passphrase']) if xdr != "mock-xdr" and not xdr.startswith("AAAA") else None
        if transaction:
            transaction.append_payment_op(
                destination=request.app['fee_wallet'],
                asset=Asset.native(),
                amount=str(fee)
            )
            # Trigger referral shares if applicable
            if status_data.get('has_referrer', False):
                await calculate_referral_shares(app_context.db_pool, telegram_id, float(fee))

    # Log trading volume for referrals with incremental mock hash
    _tx_counter += 1
    tx_hash = f"mock-tx-{_tx_counter}-{int(time.time())}"
    await log_xlm_volume(telegram_id, xlm_volume, tx_hash, app_context.db_pool)

    try:
        signed_xdr = await app_context.sign_transaction(telegram_id, xdr if xdr.startswith("AAAA") or xdr == "mock-xdr" else transaction.to_xdr() if transaction else xdr)
        tx_hash = f"mock-tx-{_tx_counter}-{int(time.time())}"  # Unique hash for response
        # Skip fees table insert to avoid modifying live DB
        # async with request.app['db_pool'].acquire() as conn:
        #     await conn.execute(
        #         "INSERT INTO fees (telegram_id, action_type, amount, fee, tx_hash) VALUES ($1, $2, $3, $4, $5)",
        #         telegram_id, action_type, float(amount), float(fee), tx_hash
        #     )
    except Exception as e:
        logger.error(f"Signing failed for user {telegram_id}: {str(e)}")
        raise web.HTTPInternalServerError(text=f'Signing failed: {str(e)}')

    return web.json_response({
        'signed_xdr': signed_xdr,
        'hash': tx_hash,
        'fee': float(fee)
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
        web_app['network_passphrase'] = 'Test SDF Network ; September 2015'
        web_app['fee_wallet'] = app_context.fee_wallet
        web_app['db_pool'] = app_context.db_pool
        web_app['sign_transaction'] = app_context.sign_transaction

        web_app.router.add_post('/api/auth/telegram', api_auth_telegram)
        web_app.router.add_post('/api/check_status', api_check_status)
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