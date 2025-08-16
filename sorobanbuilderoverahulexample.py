import logging
import time
import aiohttp
import asyncio
import os
from dotenv import load_dotenv
from stellar_sdk import TransactionEnvelope, Network, SorobanServerAsync, Account, Address, Asset, ChangeTrust
from stellar_sdk.client.aiohttp_client import AiohttpClient
from stellar_sdk.call_builder.call_builder_async import EffectsCallBuilder as AsyncEffectsCallBuilder
from stellar_sdk.xdr import SCValType
from core.stellar import load_account_async, build_and_submit_transaction, wait_for_transaction_confirmation, has_trustline, get_recommended_fee
from services.referrals import log_xlm_volume, calculate_referral_shares
from globals import is_founder

load_dotenv()
logger = logging.getLogger(__name__)

API_BASE_URL = "https://swap.apis.xbull.app"  # From official docs

async def format_asset(code, issuer):
    if code == "XLM":
        return "native"
    return f"{code}:{issuer}" if issuer else code

async def get_xlm_equivalent(app_context, asset_code, asset_issuer, amount):
    # Your existing function remains unchanged
    pass  # ... (keep as is)

async def has_referrer(telegram_id, db_pool):
    # Your existing function remains unchanged
    pass  # ... (keep as is)

async def build_and_submit_soroban_transaction(telegram_id, soroban_ops, app_context, original_tx_hash, trader_wallet, use_rpc=False):
    public_key = await app_context.load_public_key(telegram_id)
    account_data = await load_account_async(public_key, app_context)
    sequence = int(account_data["sequence"])  # Not strictly needed for API, but useful for checks

    # Fetch user copy-trading settings (keep as is)
    async with app_context.db_pool_copytrading.acquire() as conn:
        user_data = await conn.fetchrow(
            "SELECT multiplier, fixed_amount, slippage FROM copy_trading WHERE user_id = $1 AND wallet_address = $2",
            telegram_id, trader_wallet
        )
    if not user_data:
        logger.error(f"No copy-trading settings for user_id {telegram_id} and wallet {trader_wallet}")
        raise ValueError(f"No copy-trading settings found for user {telegram_id}")
    multiplier = float(user_data['multiplier'])
    fixed_amount = float(user_data['fixed_amount']) if user_data['fixed_amount'] is not None else None
    slippage = float(user_data['slippage'])
    logger.info(f"User {telegram_id} settings - Multiplier: {multiplier}, Fixed Amount: {fixed_amount}, Slippage: {slippage}")

    # Parse effects from original tx to get input/output assets and base amounts (keep this part, as you need it for copying)
    input_asset_code = "Unknown"
    input_asset_issuer = None
    output_asset_code = "Unknown"
    output_asset_issuer = None
    credited_assets = []
    amount_in = 0.0  # Base input amount from trader
    amount_out_min = 0.0  # Base min output from trader
    try:
        effects_builder = AsyncEffectsCallBuilder(
            horizon_url=app_context.horizon_url, 
            client=app_context.client
        ).for_transaction(original_tx_hash).limit(50)
        start_time = time.time()
        effects_response = await effects_builder.call()
        query_time = time.time() - start_time
        logger.debug(f"Full effects query for {original_tx_hash} took {query_time:.3f}s, records: {len(effects_response['_embedded']['records'])}")
        logger.debug(f"Effects: {effects_response['_embedded']['records']}")
        
        # Find input (debited from trader)
        for effect in effects_response["_embedded"]["records"]:
            if effect["type"] == "account_debited" and effect["account"] == trader_wallet:
                amount_in = float(effect["amount"])
                if effect.get("asset_type") == "native":
                    input_asset_code = "XLM"
                    input_asset_issuer = None
                elif effect.get("asset_type") in ["credit_alphanum4", "credit_alphanum12"]:
                    input_asset_code = effect.get("asset_code", "Unknown")
                    input_asset_issuer = effect.get("asset_issuer", None)
                break
        
        # Collect all credited assets for trader
        credited_effects = [effect for effect in effects_response["_embedded"]["records"] 
                           if effect["type"] == "account_credited" and effect["account"] == trader_wallet]
        if credited_effects:
            for effect in credited_effects:
                asset_code = "XLM" if effect.get("asset_type") == "native" else effect.get("asset_code", "Unknown")
                asset_issuer = None if effect.get("asset_type") == "native" else effect.get("asset_issuer", None)
                credited_assets.append((asset_code, asset_issuer))
            # Set final output as the last credited asset
            last_credit = credited_effects[-1]
            amount_out_min = float(last_credit["amount"])  # Use actual received as base min
            if last_credit.get("asset_type") == "native":
                output_asset_code = "XLM"
                output_asset_issuer = None
            elif last_credit.get("asset_type") in ["credit_alphanum4", "credit_alphanum12"]:
                output_asset_code = last_credit.get("asset_code", "Unknown")
                output_asset_issuer = last_credit.get("asset_issuer", None)
        else:
            logger.error(f"No credited effects found for {trader_wallet} in tx {original_tx_hash}")
            raise ValueError(f"Could not determine output asset for tx {original_tx_hash} - no credited effects")

        if input_asset_code == "Unknown":
            logger.warning(f"Could not determine input asset for {trader_wallet} in tx {original_tx_hash}")
            raise ValueError(f"Could not determine input asset for tx {original_tx_hash}")

        logger.info(f"Detected input: {input_asset_code}, output: {output_asset_code}, credited assets: {credited_assets}")
    except Exception as e:
        logger.error(f"Failed to fetch or parse effects for original_tx_hash {original_tx_hash}: {str(e)}")
        raise

    # Trustlines for all credited assets (keep as is, API requires them)
    for asset_code, asset_issuer in credited_assets:
        asset = Asset(asset_code, asset_issuer) if asset_issuer else Asset.native()
        if not asset.is_native():
            has_trust = await has_trustline(account_data, asset)
            logger.debug(f"Trustline check for {asset.code}:{asset.issuer}: {has_trust}")
            if not has_trust:
                logger.info(f"Adding trustline for {asset.code}:{asset_issuer}")
                trust_op = ChangeTrust(asset=asset, limit="922337203685.4775807")
                trust_response, trust_xdr = await build_and_submit_transaction(
                    telegram_id=telegram_id,
                    db_pool=app_context.db_pool_nitro,
                    operations=[trust_op],
                    app_context=app_context,
                    memo=f"Trustline for {asset.code}"
                )
                await wait_for_transaction_confirmation(trust_response["hash"], app_context)
                account_data = await load_account_async(public_key, app_context)  # Refresh account data

    # Apply copy-trading settings and prepare amounts (adapted)
    send_amount = fixed_amount if fixed_amount is not None else amount_in * multiplier
    min_receive = amount_out_min * (send_amount / amount_in) * (1 - slippage)  # Apply slippage to min
    send_amount_str = str(round(send_amount, 7))  # API expects string amounts

    # Balance checks (keep as is, adjust for network fee estimate)
    recommended_fee = await get_recommended_fee(app_context)
    base_fee = max(recommended_fee, 300)  # Conservative estimate for Soroban tx
    balance = float(next((b["balance"] for b in account_data["balances"] if b.get("asset_type") == ("native" if input_asset_code == "XLM" else "credit_alphanum4") and (input_asset_code == "XLM" or (b["asset_code"] == input_asset_code and b["asset_issuer"] == input_asset_issuer))), "0"))
    xlm_balance = float(next((b["balance"] for b in account_data["balances"] if b["asset_type"] == "native"), "0"))

    if input_asset_code == "XLM":
        required_balance = send_amount + (base_fee / 10**7) + 1  # Reserve for fee + base
        if balance < required_balance:
            logger.warning(f"Insufficient {input_asset_code} balance ({balance} < {required_balance}). Using max.")
            send_amount = balance - (base_fee / 10**7) - 1
            if send_amount <= 0:
                raise ValueError(f"No {input_asset_code} available to trade")
            send_amount_str = str(round(send_amount, 7))
            min_receive = amount_out_min * (send_amount / amount_in) * (1 - slippage)
    else:
        required_xlm = base_fee / 10**7
        if xlm_balance < required_xlm:
            raise ValueError(f"Insufficient XLM for network fee: required {required_xlm}, available {xlm_balance}")
        if balance < send_amount:
            logger.warning(f"Insufficient {input_asset_code} balance ({balance} < {send_amount}). Using max.")
            send_amount = balance
            if send_amount <= 0:
                raise ValueError(f"No {input_asset_code} available to trade")
            send_amount_str = str(round(send_amount, 7))
            min_receive = amount_out_min * (send_amount / amount_in) * (1 - slippage)

    logger.info(f"Adjusted send_amount: {send_amount_str}, min_receive: {min_receive}")

    # API Integration: Get quote
    input_asset = await format_asset(input_asset_code, input_asset_issuer)
    output_asset = await format_asset(output_asset_code, output_asset_issuer)
    async with aiohttp.ClientSession() as session:
        try:
            quote_params = {
                "input_asset": input_asset,
                "output_asset": output_asset,
                "amount": send_amount_str,
                "source_account": public_key,
                "slippage_tolerance": str(slippage * 100)  # API likely expects percentage (e.g., "0.5" for 0.5%)
            }
            async with session.get(f"{API_BASE_URL}/swaps/quote", params=quote_params) as resp:
                if resp.status != 200:
                    raise ValueError(f"Quote failed: {await resp.text()}")
                quote_data = await resp.json()
                logger.info(f"Quote received: estimated_output={quote_data.get('estimated_output')}, route={quote_data.get('route')}")

                # Optionally confirm quote meets min_receive
                estimated_output = float(quote_data.get('estimated_output', 0))
                if estimated_output < min_receive:
                    raise ValueError(f"Quote output {estimated_output} below min {min_receive} after slippage")

            # Accept quote to get unsigned XDR
            accept_body = {
                "quote_id": quote_data.get("quote_id"),  # Assuming response has quote_id; adjust if it's full quote
                "source_account": public_key
            }
            async with session.post(f"{API_BASE_URL}/swaps/accept-quote", json=accept_body) as resp:
                if resp.status != 200:
                    raise ValueError(f"Accept quote failed: {await resp.text()}")
                accept_data = await resp.json()
                unsigned_xdr = accept_data.get("xdr")
                if not unsigned_xdr:
                    raise ValueError("No XDR in accept-quote response")

            # Sign the XDR (using your existing signer)
            async def telegram_signer(tx_xdr):
                return await app_context.transaction_signer(telegram_id, tx_xdr)
            signed_xdr = await telegram_signer(unsigned_xdr)

            # Submit signed XDR
            submit_body = {
                "signed_xdr": signed_xdr
            }
            async with session.post(f"{API_BASE_URL}/swaps/submit", json=submit_body) as resp:
                if resp.status != 200:
                    raise ValueError(f"Submit failed: {await resp.text()}")
                submit_data = await resp.json()
                swap_hash = submit_data.get("hash")
                logger.info(f"Swap submitted: hash={swap_hash}")

            if swap_hash:
                await wait_for_transaction_confirmation(swap_hash, app_context)
            else:
                raise ValueError("Failed to get transaction hash")

        except Exception as e:
            logger.error(f"API swap failed: {str(e)}")
            return None, None

    # Post-swap: Effects, fees, referrals (keep as is, with minor adaptations)
    soroban_network_fee = 0.0  # API handles submission, but you can query tx details for actual fee if needed
    network_fee = soroban_network_fee

    is_xlm_input = False
    is_xlm_output = False
    xlm_amount = 0.0
    input_amount = 0.0
    input_asset_code_effects = input_asset_code
    input_asset_issuer_effects = input_asset_issuer
    output_amount = 0.0
    output_asset_code_effects = output_asset_code
    output_asset_issuer_effects = output_asset_issuer
    try:
        effects_builder = AsyncEffectsCallBuilder(
            horizon_url=app_context.horizon_url, 
            client=app_context.client
        ).for_transaction(swap_hash).limit(50)
        effects_response = await effects_builder.call()
        logger.debug(f"Raw EFFECTS for {swap_hash}: {effects_response['_embedded']['records']}")
        user_effects = [effect for effect in effects_response["_embedded"]["records"] 
                        if effect["account"] == public_key and 
                           (effect["type"] == "account_debited" or effect["type"] == "account_credited")]
        logger.debug(f"Filtered EFFECTS for {swap_hash} and account {public_key}: {user_effects}")
        for effect in user_effects:
            if effect["type"] == "account_debited":
                amount = float(effect["amount"])
                if effect.get("asset_type") == "native":
                    is_xlm_input = True
                    xlm_amount = amount
                    input_amount = amount
                    input_asset_code_effects = "XLM"
                    input_asset_issuer_effects = None
                else:
                    input_amount = amount
                    input_asset_code_effects = effect.get("asset_code", "Unknown")
                    input_asset_issuer_effects = effect.get("asset_issuer", None)
            elif effect["type"] == "account_credited":
                amount = float(effect["amount"])
                if effect.get("asset_type") == "native":
                    is_xlm_output = True
                    xlm_amount = amount
                    output_amount = amount
                    output_asset_code_effects = "XLM"
                    output_asset_issuer_effects = None
                else:
                    output_amount = amount
                    output_asset_code_effects = effect.get("asset_code", "Unknown")
                    output_asset_issuer_effects = effect.get("asset_issuer", None)
        if is_xlm_input:
            amount_xlm = xlm_amount
        elif is_xlm_output:
            amount_xlm = xlm_amount
        elif output_amount > 0 and output_asset_code_effects != "Unknown":
            amount_xlm = await get_xlm_equivalent(app_context, output_asset_code_effects, output_asset_issuer_effects, output_amount)
        else:
            amount_xlm = await get_xlm_equivalent(app_context, input_asset_code_effects, input_asset_issuer_effects, input_amount)
    except Exception as e:
        logger.error(f"Failed to fetch effects for {swap_hash}: {str(e)}")
        amount_xlm = float(send_amount_str) if input_asset_code == "XLM" else await get_xlm_equivalent(app_context, input_asset_code, input_asset_issuer, float(send_amount_str))
        input_amount = float(send_amount_str)
        output_amount = min_receive  # Fallback estimate

    # Fee calculation and submission (keep as is)
    xlm_balance = float(next((b["balance"] for b in account_data["balances"] if b["asset_type"] == "native"), "0"))
    fee_percentage = 0.01
    has_referrer_flag = False
    is_founder_user = await is_founder(telegram_id, app_context.db_pool_copytrading)
    if is_founder_user:
        fee_percentage = 0.001
    else:
        has_referrer_flag = await has_referrer(telegram_id, app_context.db_pool_copytrading)
        if has_referrer_flag:
            fee_percentage = 0.009
    fee_amount = str(round(amount_xlm * fee_percentage, 7))
    if xlm_balance < float(fee_amount):
        raise ValueError(f"Insufficient XLM for fee: required {fee_amount}, available {xlm_balance}")

    logger.info(f"Fee: {fee_amount} XLM (input XLM: {is_xlm_input}, output XLM: {is_xlm_output}, amount: {amount_xlm} XLM)")

    network_fee = soroban_network_fee
    if float(fee_amount) > 0:
        fee_payment = Payment(
            destination=app_context.fee_wallet,
            asset=Asset.native(),
            amount=fee_amount
        )
        try:
            memo_text = f"Fee for swap {swap_hash[-8:]}"
            response, xdr = await build_and_submit_transaction(
                telegram_id=telegram_id,
                db_pool=app_context.db_pool_nitro,
                operations=[fee_payment],
                app_context=app_context,
                memo=memo_text
            )
            logger.info(f"Service fee transaction submitted successfully: {response['hash']}")
            await wait_for_transaction_confirmation(response['hash'], app_context)
        except Exception as e:
            logger.error(f"Failed to submit fee transaction: {str(e)}")
            logger.warning("Proceeding with swap response despite fee failure")

    # Log referral volume and calculate shares (keep as is)
    xlm_volume = amount_xlm
    await log_xlm_volume(telegram_id, xlm_volume, swap_hash, app_context.db_pool_copytrading)
    try:
        await calculate_referral_shares(app_context.db_pool_copytrading, telegram_id, float(fee_amount))
        logger.info(f"Successfully calculated referral shares for user {telegram_id} with fee {fee_amount} XLM")
    except Exception as e:
        logger.error(f"Failed to calculate referral shares for user {telegram_id}: {str(e)}", exc_info=True)

    return {
        "tx_status": "PENDING",
        "hash": swap_hash,
        "fee_amount": float(fee_amount),
        "xlm_volume": amount_xlm,
        "input_amount": input_amount,
        "input_asset_code": input_asset_code_effects,
        "output_amount": output_amount,
        "output_asset_code": output_asset_code_effects,
        "service_fee": float(fee_amount)
    }, signed_xdr  # Return signed XDR if needed elsewhere

    # No need for finally block with RPC close, as API handles it

# Your try_sdex_fallback can remain as a fallback if API calls fail, or remove if you trust the API's reliability.