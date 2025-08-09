import asyncio
import aiohttp
from decimal import Decimal
from stellar_sdk import Asset, PathPaymentStrictSend, PathPaymentStrictReceive, Payment, ChangeTrust
from stellar_sdk.call_builder.call_builder_async import OperationsCallBuilder as AsyncOperationsCallBuilder
from stellar_sdk.call_builder.call_builder_async import EffectsCallBuilder as AsyncEffectsCallBuilder
from stellar_sdk.call_builder.call_builder_async import StrictSendPathsCallBuilder
from stellar_sdk.call_builder.call_builder_async import TransactionsCallBuilder as AsyncTransactionsCallBuilder
import logging
from core.stellar import build_and_submit_transaction, has_trustline, load_account_async, parse_asset, get_recommended_fee
from services.trade_services import wait_for_transaction_confirmation, calculate_fee_and_check_balance
from services.referrals import log_xlm_volume, calculate_referral_shares
import os
from globals import is_founder
import html

logging.basicConfig(level=os.getenv('LOG_LEVEL', 'INFO'))  # Default to INFO
logger = logging.getLogger(__name__)

async def get_xlm_equivalent(app_context, asset, amount):
    if asset.is_native():
        return amount
    try:
        amount_decimal = Decimal(str(amount)).quantize(Decimal('0.0000001'))
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
            logger.debug(f"XLM equivalent for {amount} {asset.code}: {paths[0]['destination_amount']}")
            return float(paths[0]["destination_amount"])
        else:
            logger.warning(f"No paths found for {asset.code}:{asset.issuer} to XLM. Assuming 0 XLM volume.")
            return 0.0
    except Exception as e:
        logger.error(f"Error fetching paths for {asset.code}:{asset.issuer}: {str(e)}")
        return 0.0

async def get_market_receive_amount(app_context, send_asset, send_amount, dest_asset):
    try:
        amount_decimal = Decimal(str(send_amount)).quantize(Decimal('0.0000001'))
        amount_str = format(amount_decimal, 'f')
        builder = StrictSendPathsCallBuilder(
            horizon_url=app_context.horizon_url,
            client=app_context.client,
            source_asset=send_asset,
            source_amount=amount_str,
            destination=[dest_asset]
        ).limit(1)
        paths_response = await builder.call()
        paths = paths_response.get("_embedded", {}).get("records", [])
        if paths:
            logger.debug(f"Market receive amount for {send_amount} {send_asset.code} to {dest_asset.code}: {paths[0]['destination_amount']}")
            return float(paths[0]["destination_amount"])
        else:
            logger.warning(f"No paths found from {send_asset.code} to {dest_asset.code}. Falling back to proportional scaling.")
            return None
    except Exception as e:
        logger.error(f"Error fetching paths from {send_asset.code} to {dest_asset.code}: {str(e)}")
        return None

async def remove_zero_balance_trustlines(telegram_id, chat_id, app_context):
    """
    Remove trustlines for assets with zero balance to free up account reserves.
    """
    try:
        public_key = await app_context.load_public_key(telegram_id)
        account_dict = await load_account_async(public_key, app_context)
        operations_to_submit = []
        assets_to_remove = []

        # Calculate tradable XLM balance
        xlm_balance = Decimal("0")
        for b in account_dict["balances"]:
            if b.get("asset_type") == "native":
                xlm_balance = Decimal(str(b["balance"]))
                break
        subentry_count = account_dict.get("subentry_count", 0)
        num_sponsoring = account_dict.get("num_sponsoring", 0)
        num_sponsored = account_dict.get("num_sponsored", 0)
        base_reserve = Decimal('2.0')
        subentry_reserve = Decimal(str(subentry_count + num_sponsoring - num_sponsored)) * Decimal('0.5')
        minimum_reserve = base_reserve + subentry_reserve
        xlm_liabilities = Decimal(next((b["selling_liabilities"] for b in account_dict["balances"] if b["asset_type"] == "native"), "0"))
        tradable_xlm_balance = max(xlm_balance - xlm_liabilities - minimum_reserve, Decimal('0'))
        logger.debug(f"XLM Balance: {xlm_balance}, Liabilities: {xlm_liabilities}, Reserve: {minimum_reserve}, Tradable: {tradable_xlm_balance}")

        # Identify non-native assets with zero balance and no liabilities
        for balance in account_dict["balances"]:
            if balance["asset_type"] in ("credit_alphanum4", "credit_alphanum12"):
                try:
                    balance_amount = Decimal(str(balance["balance"]))
                    buying_liabilities = Decimal(str(balance.get("buying_liabilities", "0")))
                    selling_liabilities = Decimal(str(balance.get("selling_liabilities", "0")))
                    if balance_amount == Decimal("0") and buying_liabilities == Decimal("0") and selling_liabilities == Decimal("0"):
                        asset_code = balance["asset_code"]
                        asset_issuer = balance["asset_issuer"]
                        asset = Asset(asset_code, asset_issuer)
                        operations_to_submit.append(ChangeTrust(asset=asset, limit="0"))
                        assets_to_remove.append(asset_code)
                        logger.info(f"Preparing to remove trustline for {asset_code} (zero balance)")
                    else:
                        logger.debug(f"Skipping trustline removal for {balance['asset_code']}: balance={balance_amount}, buying_liabilities={buying_liabilities}, selling_liabilities={selling_liabilities}")
                except Exception as e:
                    logger.error(f"Failed to parse balance for {balance.get('asset_code', 'unknown')}: {str(e)}")

        if operations_to_submit:
            # Estimate fee for operations
            recommended_fee = await get_recommended_fee(app_context)
            base_fee = max(recommended_fee, 200 * len(operations_to_submit))
            total_xlm_required = Decimal(str(base_fee / 10000000.0))  # Convert stroops to XLM
            if tradable_xlm_balance < total_xlm_required:
                error_msg = f"Insufficient tradable XLM ({tradable_xlm_balance:.7f}) to cover trustline removal fee ({total_xlm_required:.7f})"
                logger.error(error_msg)
                await app_context.bot.send_message(
                    chat_id,
                    f"Failed to remove zero-balance trustlines: {error_msg}",
                    disable_web_page_preview=True
                )
                return

            try:
                response, xdr = await build_and_submit_transaction(
                    telegram_id,
                    app_context.db_pool,
                    operations_to_submit,
                    app_context,
                    memo="copied with t.me/lumenbrobot",
                    base_fee=base_fee
                )
                await wait_for_transaction_confirmation(response["hash"], app_context)
                logger.info(f"Removed trustlines for {assets_to_remove}")
                await app_context.bot.send_message(
                    chat_id,
                    f"Removed trustlines for assets with zero balance: {', '.join(assets_to_remove)}",
                    disable_web_page_preview=True
                )
            except Exception as e:
                logger.error(f"Failed to remove trustlines: {str(e)}")
                
                # Handle session-related errors gracefully for copy trading
                error_str = str(e)
                if "No active session" in error_str or "Please login first" in error_str:
                    await app_context.bot.send_message(
                        chat_id,
                        "🔴 **Copy Trading Login Required**\n\n"
                        "Your trading session expired during copy trading.\n"
                        "Use `/login` or Wallet Management to renew your session.",
                        parse_mode="Markdown",
                        disable_web_page_preview=True
                    )
                elif "Session expired" in error_str:
                    await app_context.bot.send_message(
                        chat_id,
                        "⏰ **Copy Trading Session Expired**\n\n"
                        "Your session expired during copy trading.\n"
                        "Use `/login` or Wallet Management to renew.",
                        parse_mode="Markdown",
                        disable_web_page_preview=True
                    )
                else:
                    await app_context.bot.send_message(
                        chat_id,
                        f"Failed to remove zero-balance trustlines: {html.escape(error_str)}",
                        disable_web_page_preview=True
                    )
        else:
            logger.debug("No zero-balance trustlines found to remove")

    except Exception as e:
        logger.error(f"Error in remove_zero_balance_trustlines: {str(e)}")
        await app_context.bot.send_message(
            chat_id,
            f"Error checking for zero-balance trustlines: {html.escape(str(e))}",
            disable_web_page_preview=True
        )

async def process_trade_signal(wallet, tx, chat_id, telegram_id, app_context):
    if "successful" not in tx or not tx["successful"]:
        logger.info(f"Transaction {tx['hash']} not successful, skipping.")
        return
    
    operations_builder = AsyncOperationsCallBuilder(horizon_url=app_context.horizon_url, client=app_context.client).for_transaction(tx["hash"])
    operations_response = await operations_builder.call()
    operations = operations_response["_embedded"]["records"]
    logger.info(f"Operations for transaction {tx['hash']}: {operations}")
    
    for op in operations:
        logger.info(f"Processing operation: {op}")
        
        op_source = op.get("source_account", tx["source_account"])
        if op_source != wallet:
            logger.info(f"Operation source {op_source} does not match wallet {wallet}, skipping.")
            continue
        
        account_dict = await load_account_async(await app_context.load_public_key(telegram_id), app_context)
        logger.debug(f"Account balances for processing: {account_dict['balances']}")
        
        async with app_context.db_pool.acquire() as conn:
            user_data = await conn.fetchrow(
                "SELECT multiplier, fixed_amount, slippage FROM copy_trading WHERE user_id = $1 AND wallet_address = $2",
                telegram_id, wallet
            )
        if not user_data:
            logger.error(f"No user data for user_id {telegram_id} and wallet {wallet}")
            await app_context.bot.send_message(chat_id, f"Failed to process trade from {wallet[-5:]}: No user data found.", disable_web_page_preview=True)
            return
        multiplier = float(user_data['multiplier'])
        fixed_amount = float(user_data['fixed_amount']) if user_data['fixed_amount'] is not None else None
        slippage = float(user_data['slippage'])
        logger.info(f"Slippage: {slippage}, Multiplier: {multiplier}, Fixed Amount: {fixed_amount}")
        
        operations_to_submit = []
        send_asset_code = "Unknown"
        dest_asset_code = "Unknown"
        original_send_amount = 0.0
        original_dest_min = 0.0
        original_received = 0.0
        send_amount_final = None
        dest_min_final = None
        send_max_final = None
        dest_amount_final = None
        
        if op["type"] == "path_payment_strict_send":
            send_asset = parse_asset({"asset_type": op["source_asset_type"], "asset_code": op.get("source_asset_code"), "asset_issuer": op.get("source_asset_issuer")})
            dest_asset = parse_asset({"asset_type": op["asset_type"], "asset_code": op.get("asset_code"), "asset_issuer": op.get("asset_issuer")})
            send_asset_code = "XLM" if send_asset.is_native() else send_asset.code
            dest_asset_code = "XLM" if dest_asset.is_native() else dest_asset.code
            
            original_send_amount = float(op["source_amount"])
            original_dest_min = float(op["destination_min"])
            original_received = float(op["amount"])
            path = [parse_asset(p) for p in op.get("path", [])]
            logger.info(f"PathPaymentStrictSend: send {original_send_amount} {send_asset_code}, receive at least {original_dest_min} {dest_asset_code}")
            
            send_amount = fixed_amount if fixed_amount is not None else original_send_amount * multiplier
            send_amount_final = Decimal(str(round(send_amount, 7)))
            intended_send_amount = send_amount_final  # Store the intended amount before adjusting for balance
            # Scale the destination minimum proportionally to the adjusted send amount
            scale_factor = send_amount_final / Decimal(str(original_send_amount))
            adjusted_dest_min = Decimal(str(original_dest_min)) * scale_factor
            # Ensure adjusted_dest_min is at least 1 stroop (0.0000001)
            adjusted_dest_min = max(adjusted_dest_min, Decimal('0.0000001'))
            
            # Simplified balance check with tradable XLM calculation
            balance = Decimal("0")
            xlm_balance = Decimal("0")  # Total XLM balance
            tradable_xlm_balance = Decimal("0")  # Tradable XLM balance after reserves
            has_asset = False
            has_xlm = False
            
            # First pass: Find XLM balance
            for b in account_dict["balances"]:
                if b.get("asset_type") == "native":
                    try:
                        xlm_balance = Decimal(str(b["balance"]))
                        has_xlm = True
                        logger.debug(f"Found XLM balance: {xlm_balance}")
                    except Exception as e:
                        logger.error(f"Failed to parse XLM balance: {str(e)}")
            
            # Second pass: Find send_asset balance
            for b in account_dict["balances"]:
                if send_asset.is_native() and b.get("asset_type") == "native":
                    balance = Decimal(str(b["balance"]))
                    has_asset = True
                    logger.debug(f"Found XLM balance (native asset): {balance}")
                elif not send_asset.is_native() and b.get("asset_type") in ["credit_alphanum4", "credit_alphanum12"]:
                    if b.get("asset_code") == send_asset.code and b.get("asset_issuer") == send_asset.issuer:
                        balance = Decimal(str(b["balance"]))
                        has_asset = True
                        logger.debug(f"Found {send_asset_code} balance: {balance}")
            
            if not has_asset and not send_asset.is_native():
                error_msg = (
                    f"Asset {send_asset_code} not found in wallet.\n"
                    f"Original trade: {original_send_amount:.7f} {send_asset_code} (multiplier: {multiplier:.2f})."
                )
                logger.error(error_msg)
                await app_context.bot.send_message(chat_id, f"Trade failed for wallet {wallet[-5:]}: {html.escape(error_msg)}", disable_web_page_preview=True)
                return  # Exit early without proceeding to XLM check

            if not has_xlm:
                error_msg = (
                    f"No XLM balance found in account balances: {account_dict['balances']}."
                    f"Original trade: {original_send_amount:.7f} {send_asset_code} (multiplier: {multiplier:.2f})."
                )
                logger.error(error_msg)
                await app_context.bot.send_message(chat_id, f"Trade failed for wallet {wallet[-5:]}: {html.escape(error_msg)}", disable_web_page_preview=True)
                return  # Exit early if no XLM balance

            if xlm_balance == Decimal("0") and send_asset.is_native():
                error_msg = (
                    f"No XLM balance found in wallet.\n"
                    f"Original trade: {original_send_amount:.7f} {send_asset_code} (multiplier: {multiplier:.2f})."
                )
                logger.error(error_msg)
                await app_context.bot.send_message(chat_id, f"Trade failed for wallet {wallet[-5:]}: {html.escape(error_msg)}", disable_web_page_preview=True)
                return  # Exit early if no XLM for XLM trade

            if balance < send_amount_final:
                logger.warning(f"Insufficient {send_asset_code} balance ({balance} < {send_amount_final}). Using max: {balance}")
                send_amount_final = Decimal(str(round(float(balance), 7)))
                
            # Adjust scale_factor and dest_min based on the new send_amount_final
            scale_factor = send_amount_final / Decimal(str(original_send_amount))
            adjusted_dest_min = Decimal(str(original_dest_min)) * scale_factor
            adjusted_dest_min = max(adjusted_dest_min, Decimal('0.0000001'))
            
            # Fetch the market rate for the adjusted send amount
            market_receive_amount = await get_market_receive_amount(app_context, send_asset, float(send_amount_final), dest_asset)
            if market_receive_amount is not None:
                # Use the market rate, adjusted for slippage
                dest_min_final = Decimal(str(market_receive_amount)) * (1 - Decimal(str(slippage)))
            else:
                # If market rate is unavailable and we're selling the entire balance, use minimal dest_min
                if balance < intended_send_amount:
                    dest_min_final = Decimal('0.0000001')
                    await app_context.bot.send_message(
                        chat_id,
                        f"Warning: Selling entire position of {float(send_amount_final):.7f} {send_asset_code} with minimal dest_min (0.0000001 {dest_asset_code}) due to insufficient liquidity.",
                        disable_web_page_preview=True
                    )
                else:
                    # Fall back to proportional scaling
                    dest_min_final = adjusted_dest_min * (1 - Decimal(str(slippage)))
            
            dest_min_final = dest_min_final.quantize(Decimal('0.0000001'))
            # Ensure a minimum viable destination amount
            dest_min_final = max(dest_min_final, Decimal('0.0000001'))
            logger.debug(f"PathPaymentStrictSend: Adjusted send_amount_final={send_amount_final}, dest_min_final={dest_min_final}, scale_factor={scale_factor}")
            
            # Calculate tradable XLM balance
            subentry_count = account_dict.get("subentry_count", 0)
            num_sponsoring = account_dict.get("num_sponsoring", 0)
            num_sponsored = account_dict.get("num_sponsored", 0)
            base_reserve = Decimal('2.0')  # Current Stellar base reserve
            subentry_reserve = Decimal(str(subentry_count + num_sponsoring - num_sponsored)) * Decimal('0.5')
            minimum_reserve = base_reserve + subentry_reserve
            xlm_liabilities = Decimal(next((b["selling_liabilities"] for b in account_dict["balances"] if b["asset_type"] == "native"), "0"))
            tradable_xlm_balance = max(xlm_balance - xlm_liabilities - minimum_reserve, Decimal('0'))
            logger.debug(f"XLM Balance: {xlm_balance}, Liabilities: {xlm_liabilities}, Reserve: {minimum_reserve}, Tradable: {tradable_xlm_balance}")
            
            # Calculate the initial fee (1% as per the current function)
            fee = await calculate_fee_and_check_balance(app_context, None, send_asset, float(send_amount_final))  # No keypair needed

            # Adjust the fee based on user status (founder or referrer)
            is_founder_user = await is_founder(telegram_id, app_context.db_pool)  # Assuming this function exists
            async with app_context.db_pool.acquire() as conn:
                has_referrer = await conn.fetchval(
                    "SELECT referrer_id FROM referrals WHERE referee_id = $1", telegram_id
                )

            # Adjust fee percentage based on user status
            if is_founder_user:
                fee_percentage = 0.001  # 0.1% for founders
            elif has_referrer:
                fee_percentage = 0.009  # 0.9% for referred users
            else:
                fee_percentage = 0.01   # 1% for non-referred, non-founder users

            # Recalculate the fee (current fee is 1% of send_amount, adjust it to the correct percentage)
            adjusted_fee = (fee / 0.01) * fee_percentage  # Scale the fee to the correct percentage
            fee = round(adjusted_fee, 7)  # Round to 7 decimal places for XLM precision

            # Log the adjusted fee for debugging
            logger.info(f"Adjusted service fee for user {telegram_id}: {fee} XLM (Fee percentage: {fee_percentage*100}%)")

            # Update total_xlm_required with the adjusted fee
            total_xlm_required = Decimal(str(fee))
            if send_asset.is_native():
                total_xlm_required += send_amount_final

            if tradable_xlm_balance < total_xlm_required:
                error_msg = (
                    f"Insufficient tradable XLM balance to cover the trade and service fee: "
                    f"{float(tradable_xlm_balance):.7f} available, {float(total_xlm_required):.7f} needed.\n"
                    f"Total XLM: {float(xlm_balance):.7f}, Reserved: {float(minimum_reserve):.7f} XLM "
                    f"(Base: {float(base_reserve):.1f}, Subentries: {float(subentry_reserve):.1f}, "
                    f"Subentry Count: {subentry_count}, Sponsoring: {num_sponsoring}, Sponsored: {num_sponsored}).\n"
                    f"Original trade: {original_send_amount:.7f} {send_asset_code} (multiplier: {multiplier:.2f})."
                )
                logger.error(error_msg)
                await app_context.bot.send_message(chat_id, f"Trade failed for wallet {wallet[-5:]}: {html.escape(error_msg)}", disable_web_page_preview=True)
                return  # Exit early without proceeding further

            for asset in [send_asset, dest_asset]:
                if not await has_trustline(account_dict, asset):
                    logger.info(f"Adding trustline for {asset.code}")
                    operations_to_submit.append(ChangeTrust(asset=asset))  # No limit, defaults to max
                    account_dict = await load_account_async(await app_context.load_public_key(telegram_id), app_context)
            operations_to_submit.extend([
                PathPaymentStrictSend(
                    destination=await app_context.load_public_key(telegram_id),
                    send_asset=send_asset,
                    send_amount=str(float(send_amount_final)),
                    dest_asset=dest_asset,
                    dest_min=str(float(dest_min_final)),
                    path=path
                ),
                Payment(
                    destination=app_context.fee_wallet,
                    asset=Asset.native(),
                    amount=str(fee)  # Now uses the adjusted fee
                )
            ])
            memo_text = "copied with t.me/lumenbrobot"  # 22 bytes
        
        elif op["type"] == "path_payment_strict_receive":
            send_asset = parse_asset({"asset_type": op["source_asset_type"], "asset_code": op.get("source_asset_code"), "asset_issuer": op.get("source_asset_issuer")})
            dest_asset = parse_asset({"asset_type": op["asset_type"], "asset_code": op.get("asset_code"), "asset_issuer": op.get("asset_issuer")})
            send_asset_code = "XLM" if send_asset.is_native() else send_asset.code
            dest_asset_code = "XLM" if dest_asset.is_native() else dest_asset.code
            
            original_send_max = float(op["source_max"])
            original_dest_amount = float(op["amount"])
            original_received = original_dest_amount
            path = [parse_asset(p) for p in op.get("path", [])]
            logger.info(f"PathPaymentStrictReceive: receive {original_dest_amount} {dest_asset_code}, send max {original_send_max} {send_asset_code}")
            
            dest_amount = fixed_amount if fixed_amount is not None else original_dest_amount * multiplier
            dest_amount_final = Decimal(str(round(dest_amount, 7)))
            send_max_final = Decimal(str(original_send_max)) * (dest_amount_final / Decimal(str(original_dest_amount))) * (1 + Decimal(str(slippage)))
            send_max_final = send_max_final.quantize(Decimal('0.0000001'))
            intended_send_max = send_max_final  # Store the intended send_max before adjustment
            
            # Simplified balance check with tradable XLM calculation
            balance = Decimal("0")
            xlm_balance = Decimal("0")  # Total XLM balance
            tradable_xlm_balance = Decimal("0")  # Tradable XLM balance after reserves
            has_asset = False
            has_xlm = False
            
            # First pass: Find XLM balance
            for b in account_dict["balances"]:
                if b.get("asset_type") == "native":
                    try:
                        xlm_balance = Decimal(str(b["balance"]))
                        has_xlm = True
                        logger.debug(f"Found XLM balance: {xlm_balance}")
                    except Exception as e:
                        logger.error(f"Failed to parse XLM balance: {str(e)}")
            
            # Second pass: Find send_asset balance
            for b in account_dict["balances"]:
                if send_asset.is_native() and b.get("asset_type") == "native":
                    balance = Decimal(str(b["balance"]))
                    has_asset = True
                    logger.debug(f"Found XLM balance (native asset): {balance}")
                elif not send_asset.is_native() and b.get("asset_type") in ["credit_alphanum4", "credit_alphanum12"]:
                    if b.get("asset_code") == send_asset.code and b.get("asset_issuer") == send_asset.issuer:
                        balance = Decimal(str(b["balance"]))
                        has_asset = True
                        logger.debug(f"Found {send_asset_code} balance: {balance}")
            
            if not has_asset and not send_asset.is_native():
                error_msg = (
                    f"Asset {send_asset_code} not found in wallet.\n"
                    f"Original trade: {original_send_max:.7f} {send_asset_code} (multiplier: {multiplier:.2f})."
                )
                logger.error(error_msg)
                await app_context.bot.send_message(chat_id, f"Trade failed for wallet {wallet[-5:]}: {html.escape(error_msg)}", disable_web_page_preview=True)
                return  # Exit early without proceeding to XLM check

            if not has_xlm:
                error_msg = (
                    f"No XLM balance found in account balances: {account_dict['balances']}."
                    f"Original trade: {original_send_max:.7f} {send_asset_code} (multiplier: {multiplier:.2f})."
                )
                logger.error(error_msg)
                await app_context.bot.send_message(chat_id, f"Trade failed for wallet {wallet[-5:]}: {html.escape(error_msg)}", disable_web_page_preview=True)
                return  # Exit early if no XLM balance

            if xlm_balance == Decimal("0") and send_asset.is_native():
                error_msg = (
                    f"No XLM balance found in wallet.\n"
                    f"Original trade: {original_send_max:.7f} {send_asset_code} (multiplier: {multiplier:.2f})."
                )
                logger.error(error_msg)
                await app_context.bot.send_message(chat_id, f"Trade failed for wallet {wallet[-5:]}: {html.escape(error_msg)}", disable_web_page_preview=True)
                return  # Exit early if no XLM for XLM trade

            # Check if balance is insufficient for PPSR; if so, switch to PPSS
            if balance < send_max_final:
                logger.warning(f"Insufficient {send_asset_code} balance ({balance} < {send_max_final}) for PPSR. Switching to PPSS to exhaust balance.")
                await app_context.bot.send_message(
                    chat_id,
                    f"Warning: Insufficient {send_asset_code} balance ({float(balance):.7f} < {float(send_max_final):.7f}). Switching to PathPaymentStrictSend to send {float(balance):.7f} {send_asset_code}.",
                    disable_web_page_preview=True
                )
                # Use PPSS with available balance
                send_amount_final = Decimal(str(round(float(balance), 7)))
                # Scale dest_min proportionally based on the original PPSR dest_amount
                scale_factor = send_amount_final / Decimal(str(original_send_max))
                adjusted_dest_min = Decimal(str(original_dest_amount)) * scale_factor
                adjusted_dest_min = max(adjusted_dest_min, Decimal('0.0000001'))
                
                # Fetch market rate for PPSS
                market_receive_amount = await get_market_receive_amount(app_context, send_asset, float(send_amount_final), dest_asset)
                if market_receive_amount is not None:
                    dest_min_final = Decimal(str(market_receive_amount)) * (1 - Decimal(str(slippage)))
                else:
                    dest_min_final = Decimal('0.0000001')
                    await app_context.bot.send_message(
                        chat_id,
                        f"Warning: Selling entire position of {float(send_amount_final):.7f} {send_asset_code} with minimal dest_min (0.0000001 {dest_asset_code}) due to insufficient liquidity.",
                        disable_web_page_preview=True
                    )
                
                dest_min_final = dest_min_final.quantize(Decimal('0.0000001'))
                dest_min_final = max(dest_min_final, Decimal('0.0000001'))
                logger.debug(f"Switched to PPSS: send_amount_final={send_amount_final}, dest_min_final={dest_min_final}, scale_factor={scale_factor}")
                
                # Calculate tradable XLM balance
                subentry_count = account_dict.get("subentry_count", 0)
                num_sponsoring = account_dict.get("num_sponsoring", 0)
                num_sponsored = account_dict.get("num_sponsored", 0)
                base_reserve = Decimal('2.0')
                subentry_reserve = Decimal(str(subentry_count + num_sponsoring - num_sponsored)) * Decimal('0.5')
                minimum_reserve = base_reserve + subentry_reserve
                xlm_liabilities = Decimal(next((b["selling_liabilities"] for b in account_dict["balances"] if b["asset_type"] == "native"), "0"))
                tradable_xlm_balance = max(xlm_balance - xlm_liabilities - minimum_reserve, Decimal('0'))
                logger.debug(f"XLM Balance: {xlm_balance}, Liabilities: {xlm_liabilities}, Reserve: {minimum_reserve}, Tradable: {tradable_xlm_balance}")
                
                # Calculate fee for PPSS
                fee = await calculate_fee_and_check_balance(app_context, None, send_asset, float(send_amount_final))
                # Adjust fee based on user status
                is_founder_user = await is_founder(telegram_id, app_context.db_pool)
                async with app_context.db_pool.acquire() as conn:
                    has_referrer = await conn.fetchval(
                        "SELECT referrer_id FROM referrals WHERE referee_id = $1", telegram_id
                    )
                if is_founder_user:
                    fee_percentage = 0.001
                elif has_referrer:
                    fee_percentage = 0.009
                else:
                    fee_percentage = 0.01
                adjusted_fee = (fee / 0.01) * fee_percentage
                fee = round(adjusted_fee, 7)
                logger.info(f"Adjusted service fee for user {telegram_id}: {fee} XLM (Fee percentage: {fee_percentage*100}%)")
                
                total_xlm_required = Decimal(str(fee))
                if send_asset.is_native():
                    total_xlm_required += send_amount_final
                if tradable_xlm_balance < total_xlm_required:
                    error_msg = (
                        f"Insufficient tradable XLM balance for PPSS: "
                        f"{float(tradable_xlm_balance):.7f} available, {float(total_xlm_required):.7f} needed.\n"
                        f"Total XLM: {float(xlm_balance):.7f}, Reserved: {float(minimum_reserve):.7f} XLM."
                    )
                    logger.error(error_msg)
                    await app_context.bot.send_message(chat_id, f"Trade failed for wallet {wallet[-5:]}: {html.escape(error_msg)}", disable_web_page_preview=True)
                    return
                
                for asset in [send_asset, dest_asset]:
                    if not await has_trustline(account_dict, asset):
                        logger.info(f"Adding trustline for {asset.code}")
                        operations_to_submit.append(ChangeTrust(asset=asset))
                        account_dict = await load_account_async(await app_context.load_public_key(telegram_id), app_context)
                operations_to_submit.extend([
                    PathPaymentStrictSend(
                        destination=await app_context.load_public_key(telegram_id),
                        send_asset=send_asset,
                        send_amount=str(float(send_amount_final)),
                        dest_asset=dest_asset,
                        dest_min=str(float(dest_min_final)),
                        path=path
                    ),
                    Payment(
                        destination=app_context.fee_wallet,
                        asset=Asset.native(),
                        amount=str(fee)
                    )
                ])
                memo_text = "copied with t.me/lumenbrobot"  # 22 bytes
            else:
                # Original PPSR logic
                if balance < send_max_final:
                    logger.warning(f"Insufficient {send_asset_code} balance ({balance} < {send_max_final}). Adjusting to max: {balance}")
                    send_max_final = Decimal(str(round(float(balance), 7)))
                    if send_max_final < Decimal(str(original_send_max)) * (dest_amount_final / Decimal(str(original_dest_amount))):
                        error_msg = (
                            f"Insufficient balance for {send_asset_code}: "
                            f"{float(balance):.7f} available, {float(send_max_final):.7f} needed.\n"
                            f"Original trade: {original_send_max:.7f} {send_asset_code} (multiplier: {multiplier:.2f})."
                        )
                        logger.error(error_msg)
                        await app_context.bot.send_message(chat_id, f"Trade failed for wallet {wallet[-5:]}: {html.escape(error_msg)}", disable_web_page_preview=True)
                        return
                    dest_amount_final = dest_amount_final * (send_max_final / Decimal(str(original_send_max)))
                    dest_amount_final = dest_amount_final.quantize(Decimal('0.0000001'))
                    dest_amount_final = max(dest_amount_final, Decimal('0.0000001'))
                
                # Calculate tradable XLM balance
                subentry_count = account_dict.get("subentry_count", 0)
                num_sponsoring = account_dict.get("num_sponsoring", 0)
                num_sponsored = account_dict.get("num_sponsored", 0)
                base_reserve = Decimal('2.0')
                subentry_reserve = Decimal(str(subentry_count + num_sponsoring - num_sponsored)) * Decimal('0.5')
                minimum_reserve = base_reserve + subentry_reserve
                xlm_liabilities = Decimal(next((b["selling_liabilities"] for b in account_dict["balances"] if b["asset_type"] == "native"), "0"))
                tradable_xlm_balance = max(xlm_balance - xlm_liabilities - minimum_reserve, Decimal('0'))
                logger.debug(f"XLM Balance: {xlm_balance}, Liabilities: {xlm_liabilities}, Reserve: {minimum_reserve}, Tradable: {tradable_xlm_balance}")
                
                # Calculate fee for PPSR
                fee = await calculate_fee_and_check_balance(app_context, None, send_asset, float(send_max_final))
                # Adjust fee based on user status
                is_founder_user = await is_founder(telegram_id, app_context.db_pool)
                async with app_context.db_pool.acquire() as conn:
                    has_referrer = await conn.fetchval(
                        "SELECT referrer_id FROM referrals WHERE referee_id = $1", telegram_id
                    )
                if is_founder_user:
                    fee_percentage = 0.001
                elif has_referrer:
                    fee_percentage = 0.009
                else:
                    fee_percentage = 0.01
                adjusted_fee = (fee / 0.01) * fee_percentage
                fee = round(adjusted_fee, 7)
                logger.info(f"Adjusted service fee for user {telegram_id}: {fee} XLM (Fee percentage: {fee_percentage*100}%)")
                
                total_xlm_required = Decimal(str(fee))
                if send_asset.is_native():
                    total_xlm_required += send_max_final
                if tradable_xlm_balance < total_xlm_required:
                    error_msg = (
                        f"Insufficient tradable XLM balance for PPSR: "
                        f"{float(tradable_xlm_balance):.7f} available, {float(total_xlm_required):.7f} needed.\n"
                        f"Total XLM: {float(xlm_balance):.7f}, Reserved: {float(minimum_reserve):.7f} XLM."
                    )
                    logger.error(error_msg)
                    await app_context.bot.send_message(chat_id, f"Trade failed for wallet {wallet[-5:]}: {html.escape(error_msg)}", disable_web_page_preview=True)
                    return
                
                for asset in [send_asset, dest_asset]:
                    if not await has_trustline(account_dict, asset):
                        logger.info(f"Adding trustline for {asset.code}")
                        operations_to_submit.append(ChangeTrust(asset=asset))
                        account_dict = await load_account_async(await app_context.load_public_key(telegram_id), app_context)
                operations_to_submit.extend([
                    PathPaymentStrictReceive(
                        destination=await app_context.load_public_key(telegram_id),
                        send_asset=send_asset,
                        send_max=str(float(send_max_final)),
                        dest_asset=dest_asset,
                        dest_amount=str(float(dest_amount_final)),
                        path=path
                    ),
                    Payment(
                        destination=app_context.fee_wallet,
                        asset=Asset.native(),
                        amount=str(fee)
                    )
                ])
                memo_text = "copied with t.me/lumenbrobot"  # 22 bytes
        
        else:
            logger.info(f"Operation type {op['type']} not supported for copying, skipping.")
            continue
        
        try:
            logger.info(f"Submitting: Send {send_amount_final or send_max_final} {send_asset_code} for target {dest_min_final or dest_amount_final} {dest_asset_code}, Service Fee: {fee} XLM")
            response, xdr = await build_and_submit_transaction(
                telegram_id,
                app_context.db_pool,
                operations_to_submit,
                app_context,
                memo=memo_text,
                base_fee=None
            )
            await wait_for_transaction_confirmation(response["hash"], app_context)
            # Fetch transaction details to get fee_charged
            tx_details = await AsyncTransactionsCallBuilder(
                horizon_url=app_context.horizon_url,
                client=app_context.client
            ).transaction(response["hash"]).call()
            response["fee_charged"] = int(tx_details["fee_charged"])
            
            # Calculate XLM volume for logging
            if send_amount_final is not None:
                xlm_volume = await get_xlm_equivalent(app_context, send_asset, float(send_amount_final))
            else:
                xlm_volume = await get_xlm_equivalent(app_context, send_asset, float(send_max_final))
            await log_xlm_volume(telegram_id, xlm_volume, response["hash"], app_context.db_pool)
            
            # Fetch actual sent, received, and fee amounts from effects
            effects_builder = AsyncEffectsCallBuilder(horizon_url=app_context.horizon_url, client=app_context.client).for_transaction(response["hash"])
            effects_response = await effects_builder.call()
            actual_sent_amount = 0.0
            actual_fee_amount = 0.0
            received_amount = 0.0
            public_key = await app_context.load_public_key(telegram_id)
            fee_account_public_key = app_context.fee_wallet
            logger.debug(f"Effects for transaction {response['hash']}: {effects_response['_embedded']['records']}")

            for effect in effects_response["_embedded"]["records"]:
                if effect["type"] == "account_debited" and effect["account"] == public_key:
                    if (effect.get("asset_type") == "native" and send_asset.is_native()) or \
                       (effect.get("asset_type") != "native" and effect.get("asset_code") == send_asset.code and effect.get("asset_issuer") == send_asset.issuer):
                        actual_sent_amount = float(effect["amount"])
                        logger.info(f"Matched account_debited effect for path payment: {effect}")
                elif effect["type"] == "account_credited" and effect["account"] == public_key:
                    if (dest_asset.is_native() and effect.get("asset_type") == "native") or \
                       (not dest_asset.is_native() and effect.get("asset_type") != "native" and effect.get("asset_code") == dest_asset.code and effect.get("asset_issuer") == dest_asset.issuer):
                        received_amount = float(effect["amount"])
                        logger.info(f"Matched account_credited effect: {effect}")
                elif effect["type"] == "account_credited" and effect["account"] == fee_account_public_key:
                    if effect.get("asset_type") == "native":
                        actual_fee_amount = float(effect["amount"])
                        logger.info(f"Matched account_credited effect for service fee payment to fee wallet: {effect}")
                        break

            if actual_fee_amount == 0.0:
                logger.warning(f"No account_credited effect found for fee wallet {fee_account_public_key} in transaction {response['hash']}")
            else:
                logger.info(f"Actual service fee paid for user {telegram_id}: {actual_fee_amount:.7f} XLM (Transaction: {response['hash']})")

            if actual_fee_amount > 0:
                await calculate_referral_shares(app_context.db_pool, telegram_id, actual_fee_amount)
            else:
                logger.warning(f"Skipping referral shares calculation for user {telegram_id} due to missing fee payment effect")

            sent_amount = send_amount_final if op["type"] == "path_payment_strict_send" or (op["type"] == "path_payment_strict_receive" and balance < send_max_final) else send_max_final
            target_amount = dest_min_final if op["type"] == "path_payment_strict_send" or (op["type"] == "path_payment_strict_receive" and balance < send_max_final) else dest_amount_final

            sent_amount = actual_sent_amount if actual_sent_amount > 0 else (float(sent_amount) if sent_amount is not None else 0.0)

            fee_percentage = 0.01
            if xlm_volume > 0 and actual_fee_amount > 0:
                fee_percentage = actual_fee_amount / xlm_volume
                fee_percentage_rounded = round(fee_percentage * 1000) / 1000
                if abs(fee_percentage_rounded - 0.001) < 0.0001:
                    fee_percentage = 0.001
                elif abs(fee_percentage_rounded - 0.009) < 0.0001:
                    fee_percentage = 0.009
                elif abs(fee_percentage_rounded - 0.01) < 0.0001:
                    fee_percentage = 0.01
                else:
                    logger.warning(f"Calculated fee percentage {fee_percentage_rounded} does not match expected values (0.001, 0.009, 0.01). Using actual: {fee_percentage}")
            else:
                logger.warning(f"Cannot calculate fee percentage: xlm_volume={xlm_volume}, actual_fee_amount={actual_fee_amount}. Defaulting to 1%.")

            network_fee = float(response["fee_charged"]) / 10000000
            total_fee = actual_fee_amount + network_fee if actual_fee_amount > 0 else network_fee

            stellar_expert_link = f"https://stellar.expert/explorer/public/tx/{response['hash']}"
            message = (
                f"Copied trade from {wallet[-5:]}\n"
                f"Original: Sent {original_send_amount or original_send_max} {send_asset_code}, for {original_dest_min or original_received} {dest_asset_code}\n"
                f"Copied: Sent {sent_amount:.7f} {send_asset_code}, Target: {(float(target_amount) if target_amount is not None else 0.0):.7f} {dest_asset_code}\n"
                f"Received: {received_amount:.7f} {dest_asset_code}\n"
                f"Fee ({fee_percentage * 100:.1f}%): {total_fee:.7f} XLM (Network: {network_fee:.7f} XLM, Service: {actual_fee_amount:.7f} XLM)\n"
                f"Tx: <a href='{stellar_expert_link}'>View on Explorer</a>\n"
            )
            await app_context.bot.send_message(chat_id, message, parse_mode="HTML", disable_web_page_preview=True)

            await remove_zero_balance_trustlines(telegram_id, chat_id, app_context)

        except Exception as e:
            error_msg = str(e) if str(e) else "Failed to submit transaction."
            logger.error(f"Error copying trade: {error_msg}")
            
            # Handle session-related errors gracefully for copy trading
            if "No active session" in error_msg or "Please login first" in error_msg:
                failure_msg = (
                    f"🔴 **Copy Trading Login Required**\n\n"
                    f"Copy trade from {wallet[-5:]} failed: Session expired\n\n"
                    f"**To Resume Copy Trading:**\n"
                    f"• Use `/login` command\n"
                    f"• Or use Wallet Management → Login\n\n"
                    f"Copy trading will resume automatically after login."
                )
                await asyncio.wait_for(
                    app_context.bot.send_message(chat_id, failure_msg, parse_mode="Markdown", disable_web_page_preview=True),
                    timeout=5
                )
            elif "Session expired" in error_msg:
                failure_msg = (
                    f"⏰ **Copy Trading Session Expired**\n\n"
                    f"Copy trade from {wallet[-5:]} failed: Session expired\n\n"
                    f"**To Resume Copy Trading:**\n"
                    f"• Use `/login` command\n"
                    f"• Or use Wallet Management → Login\n\n"
                    f"Copy trading will resume automatically after login."
                )
                await asyncio.wait_for(
                    app_context.bot.send_message(chat_id, failure_msg, parse_mode="Markdown", disable_web_page_preview=True),
                    timeout=5
                )
            else:
                # Generic error for other issues
                response = response if 'response' in locals() else {'hash': 'N/A'}
                stellar_expert_link = f"https://stellar.expert/explorer/public/tx/{response.get('hash', 'N/A')}"
                failure_msg = (
                    f"Copied trade from {wallet[-5:]}\n"
                    f"Original: Sent {original_send_amount or original_send_max} {send_asset_code}, for {original_dest_min or original_received} {dest_asset_code}\n"
                    f"Copied: Sent {(float(send_amount_final) if send_amount_final is not None else 0.0):.7f} {send_asset_code}, Target: {(float(dest_min_final) if dest_min_final is not None else 0.0):.7f} {dest_asset_code}\n"
                    f"Fee: 0.0000000 XLM (Network: Not applied, Service: Not applied)\n"
                    f"Tx: <a href='{stellar_expert_link}'>View on Explorer</a>\n"
                    f"Operation failed for wallet {wallet[-5:]}: {html.escape(error_msg)} This may be due to low liquidity; consider increasing slippage tolerance."
                )
                await asyncio.wait_for(
                    app_context.bot.send_message(chat_id, failure_msg, parse_mode="HTML", disable_web_page_preview=True),
                    timeout=5
                )
            raise
