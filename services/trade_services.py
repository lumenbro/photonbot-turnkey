import logging
import asyncio
import time
from decimal import Decimal
from stellar_sdk import Asset, PathPaymentStrictReceive, PathPaymentStrictSend, ChangeTrust, Keypair, Payment
from stellar_sdk.exceptions import NotFoundError
from stellar_sdk.call_builder.call_builder_async import LedgersCallBuilder as AsyncLedgersCallBuilder
from stellar_sdk.call_builder.call_builder_async import TransactionsCallBuilder as AsyncTransactionsCallBuilder
from stellar_sdk.call_builder.call_builder_async import OrderbookCallBuilder as AsyncOrderbookCallBuilder
from stellar_sdk.call_builder.call_builder_async.orderbook_call_builder import OrderbookCallBuilder
from stellar_sdk.call_builder.call_builder_async.strict_send_paths_call_builder import StrictSendPathsCallBuilder
from stellar_sdk.call_builder.call_builder_async.strict_receive_paths_call_builder import StrictReceivePathsCallBuilder
from stellar_sdk.call_builder.call_builder_async import EffectsCallBuilder as AsyncEffectsCallBuilder
from core.stellar import build_and_submit_transaction, has_trustline, load_account_async, parse_asset, get_recommended_fee
from services.referrals import calculate_referral_shares, log_xlm_volume
from globals import is_founder  

logger = logging.getLogger(__name__)
logging.basicConfig(level=logging.DEBUG)

def calculate_available_xlm(account):
    xlm_balance = float(next((b["balance"] for b in account["balances"] if b["asset_type"] == "native"), "0"))
    selling_liabilities = float(next((b["selling_liabilities"] for b in account["balances"] if b["asset_type"] == "native"), "0"))
    subentry_count = account["subentry_count"]
    num_sponsoring = account.get("num_sponsoring", 0)
    num_sponsored = account.get("num_sponsored", 0)
    minimum_reserve = 2 + (subentry_count + num_sponsoring - num_sponsored) * 0.5
    available_xlm = xlm_balance - selling_liabilities - minimum_reserve
    return max(available_xlm, 0)

async def wait_for_transaction_confirmation(tx_hash, app_context, max_attempts=60, interval=1):
    attempts = 0
    while attempts < max_attempts:
        try:
            builder = AsyncTransactionsCallBuilder(horizon_url=app_context.horizon_url, client=app_context.client).transaction(tx_hash)
            tx = await builder.call()
            if "successful" in tx:
                if tx["successful"]:
                    logger.info(f"Transaction {tx_hash} confirmed successfully")
                    return True
                else:
                    result_codes = tx.get("result_codes", {})
                    logger.error(f"Transaction {tx_hash} failed on the network with result_codes: {result_codes}")
                    raise ValueError(f"Transaction {tx_hash} failed: Check details at https://stellar.expert/explorer/public/tx/{tx_hash}. Result codes: {result_codes}")
            else:
                await asyncio.sleep(interval)
                attempts += 1
        except Exception as e:
            if "404" in str(e):
                await asyncio.sleep(interval)
                attempts += 1
            else:
                logger.error(f"Error checking transaction {tx_hash}: {str(e)}", exc_info=True)
                raise
    raise ValueError(f"Transaction {tx_hash} not confirmed after {max_attempts} attempts")

async def perform_buy(telegram_id, db_pool, asset_code, asset_issuer, amount, app_context):
    """Perform a buy operation using path payments."""
    logger.info(f"🔍 TEST_MODE DEBUG: Starting buy operation for user {telegram_id}")
    logger.debug(f"🔍 TEST_MODE DEBUG: Asset: {asset_code}:{asset_issuer}, Amount: {amount}")
    logger.debug(f"🔍 TEST_MODE DEBUG: Network: {app_context.network_passphrase}")
    logger.debug(f"🔍 TEST_MODE DEBUG: Horizon URL: {app_context.horizon_url}")
    
    logger.info(f"Asset code: {asset_code}")
    
    asset = parse_asset({"code": asset_code, "issuer": asset_issuer})
    if asset is None:
        raise ValueError(f"Invalid asset: {asset_code}:{asset_issuer}")
    native_asset = Asset.native()
    
    public_key = await app_context.load_public_key(telegram_id)
    logger.debug(f"🔍 TEST_MODE DEBUG: User public key: {public_key}")
    
    account = await load_account_async(public_key, app_context)
    logger.debug(f"🔍 TEST_MODE DEBUG: Account loaded, sequence: {account['sequence']}")

    # Check if the trustline exists; if not, create it
    trustline_needed = not await has_trustline(account, asset)
    logger.debug(f"🔍 TEST_MODE DEBUG: Trustline needed: {trustline_needed}")
    
    if trustline_needed:
        logger.info(f"Adding trustline for {asset_code}:{asset_issuer} for user {telegram_id}")
        available_xlm = calculate_available_xlm(account)
        fee = await get_recommended_fee(app_context) / 10000000  # Convert stroops to XLM
        if available_xlm < fee + 0.5:  # 0.5 XLM for trustline reserve
            raise ValueError(f"Insufficient XLM to create trustline: need {fee + 0.5:.7f} XLM, available {available_xlm:.7f} XLM")
        
        operations = [ChangeTrust(asset=asset, limit="1000000000.0")]
        try:
            logger.debug(f"🔍 TEST_MODE DEBUG: Creating trustline transaction")
            response, xdr = await build_and_submit_transaction(
                telegram_id, db_pool, operations, app_context, memo=f"Add Trust {asset_code}"
            )
            await wait_for_transaction_confirmation(response["hash"], app_context)
            logger.info(f"Trustline for {asset_code}:{asset_issuer} created successfully")
            # Reload the account to update the sequence number and balances
            account = await load_account_async(public_key, app_context)
            logger.debug(f"🔍 TEST_MODE DEBUG: Account reloaded, new sequence: {account['sequence']}")
        except Exception as e:
            logger.error(f"Failed to add trustline for {asset_code}:{asset_issuer}: {str(e)}", exc_info=True)
            raise ValueError(f"Failed to create trustline for {asset_code}: {str(e)}")
    
    available_xlm = calculate_available_xlm(account)
    logger.info(f"User balance: {available_xlm} XLM (available)")
    logger.debug(f"🔍 TEST_MODE DEBUG: Available XLM: {available_xlm}")
    
    dest_amount = Decimal(str(amount)).quantize(Decimal('0.0000001'))
    dest_amount_str = format(dest_amount, 'f')
    if float(dest_amount) <= 0:
        raise ValueError(f"Invalid amount to buy: {dest_amount}")
    
    fee = await calculate_fee_and_check_balance(app_context, telegram_id, asset, float(dest_amount))
    logger.debug(f"🔍 TEST_MODE DEBUG: Calculated fee: {fee}")
    
    builder = StrictReceivePathsCallBuilder(
        horizon_url=app_context.horizon_url,
        client=app_context.client,
        source=public_key,
        destination_asset=asset,
        destination_amount=dest_amount_str
    ).limit(10)
    
    logger.info(f"Querying paths: {builder.horizon_url}/paths/strict-receive with params: {builder.params}")
    logger.debug(f"🔍 TEST_MODE DEBUG: Path query URL: {builder.horizon_url}/paths/strict-receive")
    logger.debug(f"🔍 TEST_MODE DEBUG: Path query params: {builder.params}")
    
    paths_response = await builder.call()
    logger.info(f"Paths response: {paths_response}")
    logger.debug(f"🔍 TEST_MODE DEBUG: Paths response keys: {list(paths_response.keys()) if isinstance(paths_response, dict) else 'Not a dict'}")
    
    paths_response = await builder.call()
    logger.info(f"Paths response: {paths_response}")
    
    paths = paths_response.get("_embedded", {}).get("records", [])
    logger.debug(f"🔍 TEST_MODE DEBUG: Found {len(paths)} paths")
    
    if not paths:
        raise ValueError(f"No paths found to buy {dest_amount} {asset_code} with XLM - insufficient liquidity")
    
    # Filter paths to only those where source_asset_type is 'native'
    paths = [p for p in paths if p["source_asset_type"] == "native"]
    logger.debug(f"🔍 TEST_MODE DEBUG: {len(paths)} paths after filtering for native source")
    
    if not paths:
        raise ValueError(f"No viable paths found using native XLM to buy {dest_amount} {asset_code} - insufficient liquidity")
    
    paths.sort(key=lambda p: (float(p["source_amount"]), len(p["path"])))
    
    selected_path = None
    for path in paths:
        max_source_amount = Decimal(path["source_amount"])
        logger.info(f"Evaluating path with source amount: {max_source_amount} XLM (hops: {len(path['path'])})")
        logger.debug(f"🔍 TEST_MODE DEBUG: Path details: {path}")
        
        # Updated: Handle native assets in the path
        path_assets = [native_asset]  # Start with XLM (source)
        for p in path["path"]:
            if p["asset_type"] == "native":
                path_assets.append(Asset.native())
            else:
                path_assets.append(Asset(p["asset_code"], p["asset_issuer"]))
        path_assets.append(asset)  # End with destination asset (KXLM)

        liquidity_ok = True
        if path["path"]:
            for i in range(len(path_assets) - 1):
                selling_asset = path_assets[i]
                buying_asset = path_assets[i + 1]
                logger.debug(f"🔍 TEST_MODE DEBUG: Checking liquidity for {selling_asset.code} -> {buying_asset.code}")
                
                order_book_builder = OrderbookCallBuilder(
                    horizon_url=app_context.horizon_url,
                    client=app_context.client,
                    selling=selling_asset,
                    buying=buying_asset
                ).limit(10)
                order_book = await order_book_builder.call()
                bids = order_book.get("bids", [])
                if not bids:
                    logger.warning(f"No bids found for {selling_asset.code} -> {buying_asset.code} in path")
                    liquidity_ok = False
                    break
                total_source_amount = Decimal('0.0')
                for bid in bids:
                    bid_price = Decimal(bid["price"])
                    bid_amount = Decimal(bid["amount"])
                    if bid_price == Decimal('0.0'):
                        logger.warning(f"Invalid bid price in order book for {selling_asset.code} -> {buying_asset.code}")
                        liquidity_ok = False
                        break
                    source_needed = (dest_amount / bid_price).quantize(Decimal('0.0000001'))
                    total_source_amount += source_needed if bid_amount >= source_needed else bid_amount
                    if total_source_amount >= max_source_amount:
                        break
                if total_source_amount < float(dest_amount):
                    logger.warning(f"Insufficient ask amount for {selling_asset.code} -> {buying_asset.code}: available {total_source_amount}, required {dest_amount}")
                    liquidity_ok = False
                    break
        
        if liquidity_ok:
            selected_path = path
            break
    
    if not selected_path:
        raise ValueError(f"No viable path found to buy {dest_amount} {asset_code} with XLM - insufficient liquidity")
    
    max_source_amount = Decimal(selected_path["source_amount"])
    slippage = Decimal(str(getattr(app_context, 'slippage', 0.05)))
    if selected_path["path"]:
        slippage *= 2
    max_source_amount_with_slippage = (max_source_amount * (1 + slippage)).quantize(Decimal('0.0000001'))
    max_source_amount_str = format(max_source_amount_with_slippage, 'f')
    
    logger.info(f"Selected path source amount: {max_source_amount} XLM (hops: {len(selected_path['path'])})")
    logger.info(f"Expected to spend at most {max_source_amount_with_slippage} XLM to buy {dest_amount} {asset_code}")
    
    # Adjust the fee based on user status right before the transaction
    fee_percentage = 0.01  # Default: 1% for non-referred users
    has_referrer = False  # Initialize for use later
    is_founder_user = await is_founder(telegram_id, app_context.db_pool)
    if is_founder_user:
        fee_percentage = 0.001  # 0.1% for founders
        logger.info(f"User {telegram_id} is a founder, applying fee percentage: {fee_percentage * 100}%")
    else:
        async with db_pool.acquire() as conn:
            try:
                has_referrer = await conn.fetchval(
                    "SELECT COUNT(*) FROM referrals WHERE referee_id = $1",
                    telegram_id
                ) > 0
                if has_referrer:
                    fee_percentage = 0.009  # 0.9% for referred users
                    logger.info(f"User {telegram_id} has a referrer, applying fee percentage: {fee_percentage * 100}%")
                else:
                    logger.info(f"User {telegram_id} has no referrer, applying default fee percentage: {fee_percentage * 100}%")
            except Exception as e:
                logger.error(f"Error checking referrer status for user {telegram_id}: {str(e)}")
                has_referrer = False  # Default to no referrer if the query fails
    
    adjusted_fee = Decimal(str(round(fee_percentage * float(max_source_amount), 7)))
    min_fee = Decimal(str(await get_recommended_fee(app_context) / 10000000))  # Convert stroops to XLM
    adjusted_fee = max(adjusted_fee, min_fee)
    
    total_xlm_needed = max_source_amount_with_slippage + adjusted_fee
    if total_xlm_needed > available_xlm:
        raise ValueError(f"Insufficient XLM balance: need {total_xlm_needed:.7f} XLM, available {available_xlm:.7f} XLM")
    
    logger.info(f"Adjusted fee for user {telegram_id}: {adjusted_fee:.7f} XLM (Fee percentage: {fee_percentage * 100}%)")
    
    # Build the transaction with the adjusted fee
    path = [Asset(p["asset_code"], p["asset_issuer"]) for p in selected_path["path"]]
    operations = [
        PathPaymentStrictReceive(
            send_asset=native_asset,
            send_max=max_source_amount_str,
            destination=public_key,
            dest_asset=asset,
            dest_amount=dest_amount_str,
            path=path
        ),
        Payment(
            destination=app_context.fee_wallet,
            asset=native_asset,
            amount=str(adjusted_fee)
        )
    ]
    
    logger.info(f"Buy {dest_amount} {asset_code} for max {max_source_amount_str} XLM + fee {adjusted_fee} XLM (PPSR, slippage {slippage * 100}%)")
    
    response, xdr = await build_and_submit_transaction(telegram_id, db_pool, operations, app_context, memo=f"Buy {asset_code}")
    await wait_for_transaction_confirmation(response["hash"], app_context)
    
    effects_builder = AsyncEffectsCallBuilder(horizon_url=app_context.horizon_url, client=app_context.client).for_transaction(response["hash"])
    effects_response = await effects_builder.call()
    actual_fee_paid = Decimal('0.0')
    actual_xlm_spent = Decimal('0.0')
    actual_amount_received = Decimal('0.0')
    xlm_debits = []  # Initialize the list to collect XLM debits
    
    for effect in effects_response["_embedded"]["records"]:
        if effect["type"] == "account_credited" and effect["account"] == app_context.fee_wallet and effect["asset_type"] == "native":
            actual_fee_paid = Decimal(effect["amount"])
        if effect["type"] == "account_debited" and effect["account"] == public_key and effect["asset_type"] == "native":
            # Collect all XLM debits from user account
            xlm_debits.append(Decimal(effect["amount"]))
        if effect["type"] == "account_credited" and effect["account"] == public_key and effect.get("asset_code") == asset_code and effect.get("asset_issuer") == asset_issuer:
            actual_amount_received = Decimal(effect["amount"])
    
    # The largest XLM debit should be the actual trade amount (fee is smaller)
    if xlm_debits:
        xlm_debits.sort(reverse=True)  # Sort in descending order
        actual_xlm_spent = xlm_debits[0]  # Take the largest debit
        logger.info(f"Found XLM debits: {xlm_debits}, using largest: {actual_xlm_spent}")
    
    if actual_fee_paid == 0:
        logger.warning(f"Could not determine actual fee paid for transaction {response['hash']}, using adjusted fee: {adjusted_fee}")
        actual_fee_paid = adjusted_fee
    
    if actual_xlm_spent == 0:
        logger.warning(f"Could not determine actual XLM spent for transaction {response['hash']}, using max source amount: {max_source_amount}")
        actual_xlm_spent = max_source_amount
    
    if actual_amount_received == 0:
        logger.warning(f"Could not determine actual amount received for transaction {response['hash']}, using destination amount: {dest_amount}")
        actual_amount_received = dest_amount
    
    logger.info(f"Actual fee paid: {actual_fee_paid:.7f} XLM (Fee percentage: {fee_percentage * 100:.2f}%)")
    logger.info(f"Actual XLM spent: {actual_xlm_spent:.7f} XLM")
    logger.info(f"Actual amount received: {actual_amount_received:.7f} {asset_code}")
    
    await log_xlm_volume(telegram_id, float(actual_xlm_spent), response["hash"], db_pool)
    
    if has_referrer:
        await calculate_referral_shares(db_pool, telegram_id, float(actual_fee_paid))
    else:
        logger.info(f"Skipping referral shares calculation for user {telegram_id} (no referrer)")
    
    logger.info(f"Buy successful: {response['hash']}")
    return response, float(actual_xlm_spent), float(actual_amount_received), float(actual_fee_paid), float(fee_percentage * 100)

async def perform_sell(telegram_id, db_pool, asset_code, asset_issuer, amount, app_context):
    if not asset_issuer.startswith('G') or len(asset_issuer) != 56:
        raise ValueError(f"Invalid issuer: {asset_issuer}")
    
    logger.info(f"Asset code: {asset_code}")
    
    asset = parse_asset({"code": asset_code, "issuer": asset_issuer})
    if asset is None:
        raise ValueError(f"Invalid asset: {asset_code}:{asset_issuer}")
    native_asset = Asset.native()
    
    public_key = await app_context.load_public_key(telegram_id)
    account = await load_account_async(public_key, app_context)
    
    balance = float(next((b["balance"] for b in account["balances"] if b.get("asset_code") == asset_code and b.get("asset_issuer") == asset_issuer), "0"))
    available_xlm = calculate_available_xlm(account)
    logger.info(f"User balance: {available_xlm} XLM (available), {balance} {asset_code}")
    
    send_amount = Decimal(str(min(float(amount), balance) if balance > 0 else 0)).quantize(Decimal('0.0000001'))
    send_amount_str = format(send_amount, 'f')
    if float(send_amount) <= 0:
        raise ValueError(f"No {asset_code} available to sell")
    
    # Calculate the preliminary fee at 1% to ensure sufficient balance
    fee = await calculate_fee_and_check_balance(app_context, telegram_id, asset, float(send_amount))
    
    builder = StrictSendPathsCallBuilder(
        horizon_url=app_context.horizon_url,
        client=app_context.client,
        source_asset=asset,
        source_amount=send_amount_str,
        destination=[native_asset]
    ).limit(10)
    
    logger.info(f"Querying paths: {builder.horizon_url}/paths/strict-send with params: {builder.params}")
    paths_response = await builder.call()
    logger.info(f"Paths response: {paths_response}")
    
    paths = paths_response.get("_embedded", {}).get("records", [])
    if not paths:
        raise ValueError(f"No paths found to sell {send_amount} {asset_code} for XLM - insufficient liquidity")
    
    paths.sort(key=lambda p: (-float(p["destination_amount"]), len(p["path"])))
    
    selected_path = None
    for path in paths:
        max_dest_amount = Decimal(path["destination_amount"])
        logger.info(f"Evaluating path with destination amount: {max_dest_amount} XLM (hops: {len(path['path'])})")
        
        path_assets = [asset] + [Asset(p["asset_code"], p["asset_issuer"]) for p in path["path"]] + [native_asset]
        liquidity_ok = True
        if path["path"]:  # Skip orderbook check for direct paths
            for i in range(len(path_assets) - 1):
                selling_asset = path_assets[i]
                buying_asset = path_assets[i + 1]
                order_book_builder = OrderbookCallBuilder(
                    horizon_url=app_context.horizon_url,
                    client=app_context.client,
                    selling=selling_asset,
                    buying=buying_asset
                ).limit(10)
                order_book = await order_book_builder.call()
                bids = order_book.get("bids", [])
                if not bids:
                    logger.warning(f"No bids found for {selling_asset.code} -> {buying_asset.code} in path")
                    liquidity_ok = False
                    break
                total_dest_amount = Decimal('0.0')
                for bid in bids:
                    bid_price = Decimal(bid["price"])
                    bid_amount = Decimal(bid["amount"])
                    dest_received = (send_amount * bid_price).quantize(Decimal('0.0000001'))
                    total_dest_amount += dest_received if bid_amount >= dest_received else bid_amount
                    if total_dest_amount >= max_dest_amount:
                        break
                if total_dest_amount < max_dest_amount:
                    logger.warning(f"Insufficient liquidity in order book for path: {total_dest_amount} < {max_dest_amount}")
                    liquidity_ok = False
                    break
        
        if liquidity_ok:
            selected_path = path
            break
    
    if not selected_path:
        raise ValueError(f"No viable path found to sell {send_amount} {asset_code} for XLM - insufficient liquidity")
    
    max_dest_amount = Decimal(selected_path["destination_amount"])
    slippage = Decimal(str(getattr(app_context, 'slippage', 0.05)))
    if selected_path["path"]:
        slippage *= 2
    min_dest_amount = (max_dest_amount * (1 - slippage)).quantize(Decimal('0.0000001'))
    min_dest_amount_str = format(min_dest_amount, 'f')
    
    logger.info(f"Selected path destination amount: {max_dest_amount} XLM (hops: {len(selected_path['path'])})")
    logger.info(f"Expected to receive at least {min_dest_amount} XLM for selling {send_amount} {asset_code}")
    
    # Adjust the fee based on user status right before the transaction
    fee_percentage = 0.01  # Default: 1% for non-referred users
    has_referrer = False  # Initialize for use later
    is_founder_user = await is_founder(telegram_id, app_context.db_pool)
    if is_founder_user:
        fee_percentage = 0.001  # 0.1% for founders
        logger.info(f"User {telegram_id} is a founder, applying fee percentage: {fee_percentage * 100}%")
    else:
        async with db_pool.acquire() as conn:
            try:
                has_referrer = await conn.fetchval(
                    "SELECT COUNT(*) FROM referrals WHERE referee_id = $1",
                    telegram_id
                ) > 0
                if has_referrer:
                    fee_percentage = 0.009  # 0.9% for referred users
                    logger.info(f"User {telegram_id} has a referrer, applying fee percentage: {fee_percentage * 100}%")
                else:
                    logger.info(f"User {telegram_id} has no referrer, applying default fee percentage: {fee_percentage * 100}%")
            except Exception as e:
                logger.error(f"Error checking referrer status for user {telegram_id}: {str(e)}")
                has_referrer = False  # Default to no referrer if the query fails
    
    adjusted_fee = Decimal(str(round(fee_percentage * float(max_dest_amount), 7)))
    min_fee = Decimal(str(await get_recommended_fee(app_context) / 10000000))  # Convert stroops to XLM
    adjusted_fee = max(adjusted_fee, min_fee)
    
    logger.info(f"Adjusted fee for user {telegram_id}: {adjusted_fee:.7f} XLM (Fee percentage: {fee_percentage * 100}%)")
    
    # Build the transaction with the adjusted fee
    path = [Asset(p["asset_code"], p["asset_issuer"]) for p in selected_path["path"]]
    operations = [
        PathPaymentStrictSend(
            send_asset=asset,
            send_amount=send_amount_str,
            destination=public_key,
            dest_asset=native_asset,
            dest_min=min_dest_amount_str,
            path=path
        ),
        Payment(
            destination=app_context.fee_wallet,
            asset=native_asset,
            amount=str(adjusted_fee)
        )
    ]
    
    logger.info(f"Sell {send_amount} {asset_code} for min {min_dest_amount_str} XLM + fee {adjusted_fee} XLM (PPSS, slippage {slippage * 100}%)")
    
    response, xdr = await build_and_submit_transaction(telegram_id, db_pool, operations, app_context, memo=f"Sell {asset_code}")
    await wait_for_transaction_confirmation(response["hash"], app_context)
    
    # Fetch the actual fee paid, XLM received, and amount sent from transaction effects
    effects_builder = AsyncEffectsCallBuilder(horizon_url=app_context.horizon_url, client=app_context.client).for_transaction(response["hash"])
    effects_response = await effects_builder.call()
    actual_fee_paid = 0.0
    actual_xlm_received = 0.0
    actual_amount_sent = 0.0
    
    for effect in effects_response["_embedded"]["records"]:
        if effect["type"] == "account_credited" and effect["account"] == app_context.fee_wallet and effect["asset_type"] == "native":
            actual_fee_paid = float(effect["amount"])
        if effect["type"] == "account_credited" and effect["account"] == public_key and effect["asset_type"] == "native":
            actual_xlm_received = float(effect["amount"])
        if effect["type"] == "account_debited" and effect["account"] == public_key and effect.get("asset_code") == asset_code and effect.get("asset_issuer") == asset_issuer:
            actual_amount_sent = float(effect["amount"])
    
    if actual_fee_paid == 0.0:
        logger.warning(f"Could not determine actual fee paid for transaction {response['hash']}, using adjusted fee: {adjusted_fee}")
        actual_fee_paid = adjusted_fee
    
    if actual_xlm_received == 0.0:
        logger.warning(f"Could not determine actual XLM received for transaction {response['hash']}, using max destination amount: {max_dest_amount}")
        actual_xlm_received = max_dest_amount
    
    if actual_amount_sent == 0.0:
        logger.warning(f"Could not determine actual amount sent for transaction {response['hash']}, using send amount: {send_amount}")
        actual_amount_sent = float(send_amount)
    
    logger.info(f"Actual fee paid: {actual_fee_paid:.7f} XLM (Fee percentage: {fee_percentage * 100:.2f}%)")
    logger.info(f"Actual XLM received: {actual_xlm_received:.7f} XLM")
    logger.info(f"Actual amount sent: {actual_amount_sent:.7f} {asset_code}")
    
    await log_xlm_volume(telegram_id, actual_xlm_received, response["hash"], db_pool)
    
    if has_referrer:
        await calculate_referral_shares(db_pool, telegram_id, actual_fee_paid)
    else:
        logger.info(f"Skipping referral shares calculation for user {telegram_id} (no referrer)")
    
    logger.info(f"Sell successful (PPSS): {response['hash']}")
    return response, actual_xlm_received, actual_amount_sent, actual_fee_paid, fee_percentage * 100

async def perform_withdraw(telegram_id, db_pool, asset, amount, destination, app_context):
    public_key = await app_context.load_public_key(telegram_id)
    account = await load_account_async(public_key, app_context)

    try:
        Keypair.from_public_key(destination)
    except:
        raise ValueError("Invalid destination address")

    fee = await get_recommended_fee(app_context) / 10000000
    if asset.is_native():
        current_xlm = float(next((b["balance"] for b in account["balances"] if b["asset_type"] == "native"), "0"))
        base_reserve = 2 + (account["subentry_count"] + account.get("num_sponsoring", 0) - account.get("num_sponsored", 0)) * 0.5
        max_withdrawable = current_xlm - base_reserve - fee
        if amount > max_withdrawable:
            raise ValueError(f"Insufficient XLM: maximum withdrawable is {max_withdrawable} XLM")
    else:
        asset_balance = float(next((b["balance"] for b in account["balances"] if b["asset_code"] == asset.code and b["asset_issuer"] == asset.issuer), "0"))
        if amount > asset_balance:
            raise ValueError(f"Insufficient {asset.code} balance: {asset_balance}")
        available_xlm = calculate_available_xlm(account)
        if available_xlm < fee:
            raise ValueError("Insufficient XLM for transaction fee")

    operations = [Payment(
        destination=destination,
        asset=asset,
        amount=str(round(amount, 7))
    )]

    response, xdr = await build_and_submit_transaction(telegram_id, db_pool, operations, app_context, memo="Withdrawal")
    await wait_for_transaction_confirmation(response["hash"], app_context)
    return response

async def get_estimated_xlm_value(asset, amount, app_context):
    if asset.is_native():
        return amount
    try:
        # Convert amount to Decimal for precise formatting
        amount_decimal = Decimal(str(amount)).quantize(Decimal('0.0000001'))
        # Format as fixed-point string to avoid scientific notation (e.g., "0.0000005", not "5e-07")
        amount_str = format(amount_decimal, 'f')
        builder = StrictSendPathsCallBuilder(
            horizon_url=app_context.horizon_url,
            client=app_context.client,
            source_asset=asset,
            source_amount=amount_str,
            destination=[Asset.native()]
        ).limit(1)
        paths_response = await builder.call()
        paths = paths_response.get("_embedded", {}).get("records", [])
        if paths:
            best_path = paths[0]
            return float(best_path["destination_amount"])
        else:
            logger.warning(f"No paths found for {asset.code}:{asset.issuer} to XLM. Using default fee estimation.")
            return 0.0  # Fallback to avoid blocking; fee will be minimal
    except Exception as e:
        logger.error(f"Error fetching paths for {asset.code}:{asset.issuer}: {str(e)}", exc_info=True)
        return 0.0  # Fallback to avoid blocking

async def calculate_fee_and_check_balance(app_context, telegram_id, send_asset, send_amount, is_send_max=False):
    public_key = await app_context.load_public_key(telegram_id) if telegram_id else app_context.fee_wallet
    account = await load_account_async(public_key, app_context)
    
    if send_asset.is_native():
        fee = round(0.01 * send_amount, 7)
    else:
        estimated_xlm = await get_estimated_xlm_value(send_asset, send_amount, app_context)
        fee = round(0.01 * estimated_xlm, 7)
    
    available_xlm = calculate_available_xlm(account)
    total_required = send_amount + fee if send_asset.is_native() and is_send_max else fee
    
    if available_xlm < total_required:
        raise ValueError(f"Insufficient XLM: required {total_required}, available {available_xlm}")
    
    return fee

async def perform_add_trustline(telegram_id, db_pool, asset_code, asset_issuer, app_context):
    asset = parse_asset({"code": asset_code, "issuer": asset_issuer})
    if asset is None:
        raise ValueError(f"Invalid asset: {asset_code}:{asset_issuer}")
    
    public_key = await app_context.load_public_key(telegram_id)
    account = await load_account_async(public_key, app_context)
    
    if await has_trustline(account, asset):
        raise ValueError(f"Trustline already exists for {asset_code}:{asset_issuer}")
    
    available_xlm = calculate_available_xlm(account)
    fee = await get_recommended_fee(app_context) / 10000000
    if available_xlm < fee + 0.5:
        raise ValueError(f"Insufficient XLM for trustline: need {fee + 0.5}, available {available_xlm}")
    
    operations = [ChangeTrust(asset=asset, limit="1000000000.0")]
    
    response, xdr = await build_and_submit_transaction(
        telegram_id, db_pool, operations, app_context, memo=f"Add Trust {asset_code}"
    )
    await wait_for_transaction_confirmation(response["hash"], app_context)
    return response

async def perform_remove_trustline(telegram_id, db_pool, asset_code, asset_issuer, app_context):
    asset = parse_asset({"code": asset_code, "issuer": asset_issuer})
    if asset is None:
        raise ValueError(f"Invalid asset: {asset_code}:{asset_issuer}")
    
    public_key = await app_context.load_public_key(telegram_id)
    account = await load_account_async(public_key, app_context)
    
    if not await has_trustline(account, asset):
        raise ValueError(f"No trustline exists for {asset_code}:{asset_issuer}")
    
    asset_balance = float(next(
        (b["balance"] for b in account["balances"] if b.get("asset_code") == asset_code and b.get("asset_issuer") == asset_issuer),
        "0"
    ))
    if asset_balance > 0:
        raise ValueError(f"Cannot remove trustline: {asset_balance} {asset_code} remaining")
    
    available_xlm = calculate_available_xlm(account)
    fee = await get_recommended_fee(app_context) / 10000000
    if available_xlm < fee:
        raise ValueError(f"Insufficient XLM for transaction fee: need {fee}, available {available_xlm}")
    
    operations = [ChangeTrust(asset=asset, limit="0")]
    
    response, xdr = await build_and_submit_transaction(
        telegram_id, db_pool, operations, app_context, memo=f"Remove Trust {asset_code}"
    )
    await wait_for_transaction_confirmation(response["hash"], app_context)
    return response
