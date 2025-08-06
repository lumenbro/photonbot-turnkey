# services/soroban_builder.py
import logging
import time
import aiohttp
import asyncio
import os
from dotenv import load_dotenv
from stellar_sdk import TransactionBuilder, Network, SorobanServerAsync, Account, Address, Asset, ChangeTrust, Payment, PathPaymentStrictSend
from stellar_sdk.contract import AssembledTransactionAsync
from stellar_sdk.operation import InvokeHostFunction
from stellar_sdk.client.aiohttp_client import AiohttpClient
from stellar_sdk.xdr import HostFunction, HostFunctionType, InvokeContractArgs, SCValType, SCAddressType, SCVal
from stellar_sdk.call_builder.call_builder_async import EffectsCallBuilder as AsyncEffectsCallBuilder
from stellar_sdk.call_builder.call_builder_async.strict_send_paths_call_builder import StrictSendPathsCallBuilder
from stellar_sdk.call_builder.call_builder_async import TransactionsCallBuilder as AsyncTransactionsCallBuilder
from core.stellar import load_account_async, build_and_submit_transaction, wait_for_transaction_confirmation, has_trustline, get_recommended_fee
from services.referrals import log_xlm_volume, calculate_referral_shares
from services.dex_config import DEX_ROUTERS
from globals import is_founder

load_dotenv()
logger = logging.getLogger(__name__)

async def get_xlm_equivalent(app_context, asset_code, asset_issuer, amount):
    if asset_code == "XLM":
        return amount
    asset = Asset(asset_code, asset_issuer)
    xlm_asset = Asset.native()
    if asset.is_native():
        selling_asset_type = "native"
        selling_asset_code = None
        selling_asset_issuer = None
    else:
        selling_asset_type = "credit_alphanum4" if len(asset.code) <= 4 else "credit_alphanum12"
        selling_asset_code = asset.code
        selling_asset_issuer = asset.issuer
    
    params = {
        "selling_asset_type": selling_asset_type,
        "buying_asset_type": "native"
    }
    if selling_asset_code:
        params["selling_asset_code"] = selling_asset_code
    if selling_asset_issuer:
        params["selling_asset_issuer"] = selling_asset_issuer
    
    url = f"{app_context.horizon_url}/order_book"
    async with aiohttp.ClientSession() as session:
        try:
            async with session.get(url, params=params) as response:
                if response.status != 200:
                    logger.warning(f"Failed to fetch order book for {asset.code}/XLM: HTTP {response.status}")
                    return 0.0
                order_book = await response.json()
                bids = order_book.get("bids", [])
                if not bids:
                    logger.warning(f"No bids found for {asset.code}/XLM. Assuming 0 XLM volume.")
                    return 0.0
                best_bid = bids[0]
                price = float(best_bid["price"])
                xlm_equivalent = amount * price
                return round(xlm_equivalent, 7)
        except Exception as e:
            logger.warning(f"Error fetching XLM equivalent for {asset.code}: {str(e)}")
            return 0.0

async def has_referrer(telegram_id, db_pool):
    async with db_pool.acquire() as conn:
        return await conn.fetchval("SELECT referrer_id FROM referrals WHERE referee_id = $1", telegram_id) is not None

async def build_and_submit_soroban_transaction(telegram_id, soroban_ops, app_context, original_tx_hash, trader_wallet, use_rpc=False):
    public_key = await app_context.load_public_key(telegram_id)
    account_data = await load_account_async(public_key, app_context)
    sequence = int(account_data["sequence"])

    # Fetch user copy-trading settings
    async with app_context.db_pool.acquire() as conn:
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

    # Use the free mainnet RPC endpoint
    rpc_url = "https://mainnet.sorobanrpc.com"
    client = AiohttpClient()
    async with client:
        soroban_server = SorobanServerAsync(rpc_url, client=client)

        try:
            for op in soroban_ops:
                # Extract args upfront
                original_host_function = op["original_host_function"]
                if original_host_function.type != HostFunctionType.HOST_FUNCTION_TYPE_INVOKE_CONTRACT:
                    logger.error("Expected InvokeContract HostFunction, got: %s", original_host_function.type)
                    raise ValueError("Invalid HostFunction type")
                invoke_args = original_host_function.invoke_contract
                args = invoke_args.args
                if len(args) < 1:
                    logger.error("Expected at least one argument in swap function, got: %d", len(args))
                    raise ValueError("Invalid number of arguments in swap function")

                # Full effects query with increased limit
                input_asset_code = "Unknown"
                input_asset_issuer = None
                output_asset_code = "Unknown"
                output_asset_issuer = None
                credited_assets = []
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

                # Trustlines for all credited assets
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
                                db_pool=app_context.db_pool,
                                operations=[trust_op],
                                app_context=app_context,
                                memo=f"Trustline for {asset.code}"
                            )
                            await wait_for_transaction_confirmation(trust_response["hash"], app_context)
                            account_data = await load_account_async(public_key, app_context)
                            sequence = int(account_data["sequence"])  # Update sequence
                
                # Parse amounts and apply copy-trading settings
                try:
                    amount_in_index = op["amount_in_arg"]
                    amount_out_min_index = op["amount_out_min_arg"]
                    amount_in_stroops = 0
                    amount_in = 0.0
                    amount_out_min_stroops = 0
                    amount_out_min = 0.0

                    # Parse amount_in
                    amount_in_arg = args[amount_in_index]
                    if amount_in_arg.type == SCValType.SCV_U128:
                        amount_in_stroops = int(amount_in_arg.u128.lo.uint64)
                    elif amount_in_arg.type == SCValType.SCV_I128:
                        hi = amount_in_arg.i128.hi.int64
                        lo = amount_in_arg.i128.lo.uint64
                        amount_in_stroops = lo if hi == 0 else (hi << 64) | lo
                    else:
                        logger.error(f"Invalid amount_in type at index {amount_in_index}: {amount_in_arg.type}")
                        raise ValueError(f"Unsupported amount_in type: {amount_in_arg.type}")
                    amount_in = amount_in_stroops / 10**7

                    # Parse amount_out_min
                    amount_out_min_arg = args[amount_out_min_index]
                    if amount_out_min_arg.type == SCValType.SCV_U128:
                        amount_out_min_stroops = int(amount_out_min_arg.u128.lo.uint64)
                    elif amount_out_min_arg.type == SCValType.SCV_I128:
                        hi = amount_out_min_arg.i128.hi.int64
                        lo = amount_out_min_arg.i128.lo.uint64
                        amount_out_min_stroops = lo if hi == 0 else (hi << 64) | lo
                    else:
                        logger.error(f"Invalid amount_out_min type at index {amount_out_min_index}: {amount_out_min_arg.type}")
                        raise ValueError(f"Unsupported amount_out_min type: {amount_out_min_arg.type}")
                    amount_out_min = amount_out_min_stroops / 10**7

                    # Get recommended fee for Soroban transaction
                    recommended_fee = await get_recommended_fee(app_context)
                    base_fee = max(recommended_fee, 300)  # Ensure minimum fee

                    # Apply copy-trading settings with user-set slippage
                    send_amount = fixed_amount if fixed_amount is not None else amount_in * multiplier
                    send_amount_final = round(send_amount * 10**7)
                    balance = float(next((b["balance"] for b in account_data["balances"] if b.get("asset_type") == ("native" if input_asset_code == "XLM" else "credit_alphanum4") and (input_asset_code == "XLM" or (b["asset_code"] == input_asset_code and b["asset_issuer"] == input_asset_issuer))), "0"))
                    xlm_balance = float(next((b["balance"] for b in account_data["balances"] if b["asset_type"] == "native"), "0"))

                    # Adjust balance check based on input asset
                    if input_asset_code == "XLM":
                        # For XLM, reserve network fee + 1 XLM for base reserve
                        required_balance = send_amount + (base_fee * 1 / 10**7) + 1
                        if balance < required_balance:
                            logger.warning(f"Insufficient {input_asset_code} balance ({balance} < {required_balance}) after fees and reserve. Using max: {balance - (base_fee * 1 / 10**7) - 1}")
                            send_amount_final = int((balance - (base_fee * 1 / 10**7) - 1) * 10**7)
                            if send_amount_final <= 0:
                                raise ValueError(f"No {input_asset_code} available to trade after fees and reserve")
                            dest_min_final = int((amount_out_min * (send_amount_final / amount_in_stroops)) * (1 - slippage) * 10**7)
                        else:
                            dest_min_final = int(amount_out_min * (send_amount_final / amount_in_stroops) * (1 - slippage) * 10**7)
                    else:
                        # For non-XLM assets, only check asset balance and ensure XLM for network fee
                        required_xlm = base_fee * 1 / 10**7  # Network fee in XLM
                        if xlm_balance < required_xlm:
                            raise ValueError(f"Insufficient XLM for network fee: required {required_xlm}, available {xlm_balance}")
                        if balance < send_amount:
                            logger.warning(f"Insufficient {input_asset_code} balance ({balance} < {send_amount}). Using max: {balance}")
                            send_amount_final = int(balance * 10**7)
                            if send_amount_final <= 0:
                                raise ValueError(f"No {input_asset_code} available to trade")
                            dest_min_final = int((amount_out_min * (send_amount_final / amount_in_stroops)) * (1 - slippage) * 10**7)
                        else:
                            dest_min_final = int(amount_out_min * (send_amount_final / amount_in_stroops) * (1 - slippage) * 10**7)

                    logger.info(f"Balance check: {input_asset_code} required {send_amount_final / 10**7}, available {balance}, adjusted for fees and reserve")
                    logger.info(f"Original amount_in: {amount_in}, Adjusted: {send_amount_final / 10**7}, Original amount_out_min: {amount_out_min}, Adjusted with slippage: {dest_min_final / 10**7}")

                    # Update SCVal objects with type checking
                    if args[amount_in_index].type == SCValType.SCV_U128:
                        args[amount_in_index].u128.lo.uint64 = send_amount_final
                    elif args[amount_in_index].type == SCValType.SCV_I128:
                        args[amount_in_index].i128.lo.uint64 = send_amount_final
                    else:
                        logger.error(f"Cannot update amount_in at index {amount_in_index}: unsupported type {args[amount_in_index].type}")
                        raise ValueError(f"Unsupported amount_in type for update: {args[amount_in_index].type}")

                    if args[amount_out_min_index].type == SCValType.SCV_U128:
                        args[amount_out_min_index].u128.lo.uint64 = dest_min_final
                    elif args[amount_out_min_index].type == SCValType.SCV_I128:
                        args[amount_out_min_index].i128.lo.uint64 = dest_min_final
                    else:
                        logger.error(f"Cannot update amount_out_min at index {amount_out_min_index}: unsupported type {args[amount_out_min_index].type}")
                        raise ValueError(f"Unsupported amount_out_min type for update: {args[amount_out_min_index].type}")
                except Exception as e:
                    logger.error(f"Failed to parse amounts or apply settings: {str(e)}")
                    raise

                # Build and submit transaction
                tx_builder = TransactionBuilder(
                    source_account=Account(public_key, sequence),
                    network_passphrase=Network.PUBLIC_NETWORK_PASSPHRASE,
                    base_fee=base_fee
                ).add_time_bounds(0, int(time.time()) + 900)

                function_name = invoke_args.function_name
                if invoke_args.contract_address.type != SCAddressType.SC_ADDRESS_TYPE_CONTRACT:
                    raise ValueError("Contract address is not of type SC_ADDRESS_TYPE_CONTRACT")
                contract_id = invoke_args.contract_address.contract_id.hash.hex()

                new_sender = Address(public_key)
                new_sender_scval = new_sender.to_xdr_sc_val()
                if op["sender_arg"] is not None:
                    args[op["sender_arg"]] = new_sender_scval
                if op["recipient_arg"] is not None:
                    args[op["recipient_arg"]] = new_sender_scval

                new_invoke_args = InvokeContractArgs(
                    contract_address=invoke_args.contract_address,
                    function_name=function_name,
                    args=args
                )
                new_host_function = HostFunction(
                    type=HostFunctionType.HOST_FUNCTION_TYPE_INVOKE_CONTRACT,
                    invoke_contract=new_invoke_args
                )

                operation = InvokeHostFunction(
                    host_function=new_host_function,
                    auth=None
                )
                tx_builder.append_operation(operation)

            assembled_tx = AssembledTransactionAsync(
                transaction_builder=tx_builder,
                server=soroban_server,
                transaction_signer=None,
                submit_timeout=300
            )

            max_retries = 3
            retry_delay = 2
            for attempt in range(max_retries):
                try:
                    logger.info(f"Attempting simulation with contract_id: {contract_id}")
                    assembled_tx = await assembled_tx.simulate(restore=True)
                    break
                except Exception as e:
                    if attempt < max_retries - 1:
                        logger.warning(f"Simulation attempt {attempt + 1} failed: {str(e)}. Retrying in {retry_delay} seconds...")
                        await asyncio.sleep(retry_delay)
                        retry_delay *= 2
                    else:
                        logger.error(f"Simulation failed after {max_retries} attempts: {str(e)}")
                        raise Exception(f"Simulation failed: {str(e)}")

            # Sign the transaction using the enclave
            async def telegram_signer(tx_xdr):
                return await app_context.transaction_signer(telegram_id, tx_xdr)

            # Manually sign the transaction
            signed_tx = await telegram_signer(assembled_tx.built_transaction.to_xdr())

            # Submit the signed transaction
            swap_result = None
            swap_hash = None
            for attempt in range(max_retries):
                try:
                    logger.info(f"Attempting submission with contract_id: {contract_id}")
                    # Submit the signed XDR directly via RPC
                    response = await soroban_server.send_transaction(signed_tx)
                    swap_result = response
                    swap_hash = response.hash
                    logger.info(f"Soroban transaction submitted successfully: {swap_result}")
                    break
                except Exception as e:
                    if attempt < max_retries - 1:
                        logger.warning(f"Submission attempt {attempt + 1} failed: {str(e)}. Retrying in {retry_delay} seconds...")
                        await asyncio.sleep(retry_delay)
                        retry_delay *= 2
                    else:
                        logger.error(f"Soroban transaction submission failed after {max_retries} attempts: {str(e)}")
                        logger.error(f"Full error details: {repr(e)}")
                        raise Exception(f"Soroban tx failed: {str(e)}")

            if swap_hash:
                await wait_for_transaction_confirmation(swap_hash, app_context)
            else:
                raise ValueError("Failed to get transaction hash after submission")

            # No network fee (handled by RPC submission)
            soroban_network_fee = 0.0
            network_fee = soroban_network_fee

            # Query effects for fee calculation
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
                user_effects = [effect for effect in effects_response['_embedded']['records'] 
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
                            logger.debug(f"Set xlm_amount to {xlm_amount} from account_debited")
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
                            logger.debug(f"Set xlm_amount to {xlm_amount} from account_credited")
                        else:
                            output_amount = amount
                            output_asset_code_effects = effect.get("asset_code", "Unknown")
                            output_asset_issuer_effects = effect.get("asset_issuer", None)
                if is_xlm_input:
                    amount_xlm = xlm_amount
                    logger.debug(f"Using input XLM: {amount_xlm}")
                elif is_xlm_output:
                    amount_xlm = xlm_amount
                    logger.debug(f"Using output XLM: {amount_xlm}")
                elif output_amount > 0 and output_asset_code_effects != "Unknown":
                    amount_xlm = await get_xlm_equivalent(app_context, output_asset_code_effects, output_asset_issuer_effects, output_amount)
                    logger.debug(f"Converted output {output_amount} {output_asset_code_effects} to {amount_xlm} XLM")
                else:
                    logger.warning(f"No direct XLM input/output for {swap_hash}, using input amount")
                    amount_xlm = await get_xlm_equivalent(app_context, input_asset_code_effects, input_asset_issuer_effects, input_amount)
                    logger.debug(f"Converted input {input_amount} {input_asset_code_effects} to {amount_xlm} XLM")
            except Exception as e:
                logger.error(f"Failed to fetch effects for {swap_hash}: {str(e)}")
                amount_xlm = send_amount_final / 10**7 if input_asset_code == "XLM" else await get_xlm_equivalent(app_context, input_asset_code, input_asset_issuer, send_amount_final / 10**7)
                input_amount = send_amount_final / 10**7
                output_amount = dest_min_final / 10**7

            # Fee calculation
            xlm_balance = float(next((b["balance"] for b in account_data["balances"] if b["asset_type"] == "native"), "0"))
            fee_percentage = 0.01  # Default: 1% for non-referred users
            has_referrer_flag = False  # Use a distinct name to avoid shadowing
            is_founder_user = await is_founder(telegram_id, app_context.db_pool)
            if is_founder_user:
                fee_percentage = 0.001  # 0.1% for founders
                logger.info(f"User {telegram_id} is a founder, applying fee percentage: {fee_percentage * 100}%")
            else:
                has_referrer_flag = await has_referrer(telegram_id, app_context.db_pool)
                if has_referrer_flag:
                    fee_percentage = 0.009  # 0.9% for referred users
                    logger.info(f"User {telegram_id} has a referrer, applying fee percentage: {fee_percentage * 100}%")
                else:
                    logger.info(f"User {telegram_id} has no referrer, applying default fee percentage: {fee_percentage * 100}%")
            fee_amount = str(round(amount_xlm * fee_percentage, 7))
            if xlm_balance < float(fee_amount):
                raise ValueError(f"Insufficient XLM for fee: required {fee_amount}, available {xlm_balance}")

            logger.info(f"Fee: {fee_amount} XLM (input XLM: {is_xlm_input}, output XLM: {is_xlm_output}, amount: {amount_xlm} XLM)")
            logger.info(f"has_referrer_flag: {has_referrer_flag}, type: {type(has_referrer_flag)}")  # Debug logging

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
                        db_pool=app_context.db_pool,
                        operations=[fee_payment],
                        app_context=app_context,
                        memo=memo_text
                    )
                    logger.info(f"Service fee transaction submitted successfully: {response['hash']}")
                    await wait_for_transaction_confirmation(response['hash'], app_context)
                except Exception as e:
                    logger.error(f"Failed to submit fee transaction: {str(e)}")
                    logger.warning("Proceeding with swap response despite fee failure")

            # Log referral volume and calculate shares for Soroban
            xlm_volume = amount_xlm  # Reuse existing calculation
            await log_xlm_volume(telegram_id, xlm_volume, swap_hash, app_context.db_pool)
            try:
                await calculate_referral_shares(app_context.db_pool, telegram_id, float(fee_amount))
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
            }, assembled_tx.built_transaction.to_xdr()

        except Exception as e:
            logger.error(f"Outer exception in Soroban transaction processing: {str(e)}")
            return None, None

        finally:
            await soroban_server.close()
            
async def try_sdex_fallback(telegram_id, tx, trader_wallet, chat_id, app_context):
    """Attempt SDEX PathPayment fallback when Soroban fails."""
    public_key = await app_context.load_public_key(telegram_id)
    account_data = await load_account_async(public_key, app_context)
    sequence = int(account_data["sequence"])

    # Fetch user copy-trading settings
    async with app_context.db_pool.acquire() as conn:
        user_data = await conn.fetchrow(
            "SELECT multiplier, fixed_amount, slippage FROM copy_trading WHERE user_id = $1 AND wallet_address = $2",
            telegram_id, trader_wallet
        )
    if not user_data:
        logger.error(f"No copy-trading settings for user_id {telegram_id} and wallet {trader_wallet}")
        return None, None
    multiplier = float(user_data['multiplier'])
    fixed_amount = float(user_data['fixed_amount']) if user_data['fixed_amount'] is not None else None
    slippage = float(user_data['slippage'])

    # Get trader's effects
    effects_builder = AsyncEffectsCallBuilder(
        horizon_url=app_context.horizon_url,
        client=app_context.client
    ).for_transaction(tx["hash"]).limit(50)
    effects_response = await effects_builder.call()
    effects = effects_response["_embedded"]["records"]

    send_code, send_issuer = None, None
    dest_code, dest_issuer = None, None
    send_amount = 0.0
    dest_amount = 0.0
    for effect in effects:
        if effect["type"] == "account_debited" and effect["account"] == trader_wallet:
            send_code = "XLM" if effect["asset_type"] == "native" else effect["asset_code"]
            send_issuer = None if effect["asset_type"] == "native" else effect["asset_issuer"]
            send_amount = float(effect["amount"])
        elif effect["type"] == "account_credited" and effect["account"] == trader_wallet:
            dest_code = "XLM" if effect["asset_type"] == "native" else effect["asset_code"]
            dest_issuer = None if effect["asset_type"] == "native" else effect["asset_issuer"]
            dest_amount = float(effect["amount"])

    if not send_code or not dest_code:
        logger.error(f"Failed to determine assets from effects for {tx['hash']}")
        return None, None

    # Get recommended fee for SDEX transaction
    recommended_fee = await get_recommended_fee(app_context)
    base_fee = max(recommended_fee, 200 * 2)  # 2 operations
    
    # Apply copy-trading settings with user-set slippage
    send_amount_final = fixed_amount if fixed_amount is not None else send_amount * multiplier
    dest_min_final = dest_amount * (send_amount_final / send_amount) * (1 - slippage)
    send_amount_final_stroops = round(send_amount_final * 10**7)
    dest_min_final_stroops = int(dest_min_final * 10**7)

    # Balance check
    balance = float(next((b["balance"] for b in account_data["balances"] if b.get("asset_type") == ("native" if send_code == "XLM" else "credit_alphanum4") and (send_code == "XLM" or (b["asset_code"] == send_code and b["asset_issuer"] == send_issuer))), "0"))
    xlm_balance = float(next((b["balance"] for b in account_data["balances"] if b["asset_type"] == "native"), "0"))

    # Adjust balance check based on input asset
    if send_code == "XLM":
        # For XLM, reserve network fee + 1 XLM for base reserve
        required_balance = send_amount_final + (base_fee * 2 / 10**7) + 1
        if balance < required_balance:
            logger.warning(f"Insufficient {send_code} balance ({balance} < {required_balance}) after fees and reserve. Using max: {balance - (base_fee * 2 / 10**7) - 1}")
            send_amount_final = balance - (base_fee * 2 / 10**7) - 1
            send_amount_final_stroops = int(send_amount_final * 10**7)
            dest_min_final = dest_amount * (send_amount_final / send_amount) * (1 - slippage)
            dest_min_final_stroops = int(dest_min_final * 10**7)
    else:
        # For non-XLM assets, only check asset balance and ensure XLM for network fee
        required_xlm = base_fee * 2 / 10**7  # Network fee for 2 operations
        if xlm_balance < required_xlm:
            raise ValueError(f"Insufficient XLM for network fee: required {required_xlm}, available {xlm_balance}")
        if balance < send_amount_final:
            logger.warning(f"Insufficient {send_code} balance ({balance} < {send_amount_final}). Using max: {balance}")
            send_amount_final = balance
            send_amount_final_stroops = int(balance * 10**7)
            dest_min_final = dest_amount * (send_amount_final / send_amount) * (1 - slippage)
            dest_min_final_stroops = int(dest_min_final * 10**7)  

    # Fee calculation
    fee_percentage = 0.01  # Default: 1% for non-referred users
    has_referrer_flag = False
    is_founder_user = await is_founder(telegram_id, app_context.db_pool)
    if is_founder_user:
        fee_percentage = 0.001  # 0.1% for founders
        logger.info(f"User {telegram_id} is a founder, applying fee percentage: {fee_percentage * 100}%")
    else:
        has_referrer_flag = await has_referrer(telegram_id, app_context.db_pool)
        if has_referrer_flag:
            fee_percentage = 0.009  # 0.9% for referred users
            logger.info(f"User {telegram_id} has a referrer, applying fee percentage: {fee_percentage * 100}%")
        else:
            logger.info(f"User {telegram_id} has no referrer, applying default fee percentage: {fee_percentage * 100}%")
    fee_amount = round(send_amount_final * fee_percentage, 7)
    total_required_xlm = fee_amount + (send_amount_final if send_code == "XLM" else 0) + (base_fee * 2 / 10**7)
    if xlm_balance < total_required_xlm:
        logger.warning(f"Insufficient XLM for trade + fee + reserve: {xlm_balance} < {total_required_xlm}. Adjusting send amount.")
        available_xlm = xlm_balance - fee_amount - (base_fee * 2 / 10**7)
        send_amount_final = available_xlm if send_code == "XLM" else send_amount_final
        send_amount_final_stroops = int(send_amount_final * 10**7)
        dest_min_final = dest_amount * (send_amount_final / send_amount) * (1 - slippage)
        dest_min_final_stroops = int(dest_min_final * 10**7)

    send_asset = Asset(send_code, send_issuer) if send_issuer else Asset.native()
    dest_asset = Asset(dest_code, dest_issuer) if dest_issuer else Asset.native()

    # Build operations list with PathPayment and Fee
    operations = [
        PathPaymentStrictSend(
            send_asset=send_asset,
            send_amount=str(send_amount_final_stroops / 10**7),
            destination=public_key,
            dest_asset=dest_asset,
            dest_min=str(dest_min_final_stroops / 10**7),
            path=[]
        ),
        Payment(
            destination=app_context.fee_wallet,
            asset=Asset.native(),
            amount=str(fee_amount)
        )
    ]

    try:
        # Define fee_payment in this scope
        fee_payment = Payment(
            destination=app_context.fee_wallet,
            asset=Asset.native(),
            amount=str(fee_amount)
        )
        response_dict, xdr = await build_and_submit_transaction(
            telegram_id=telegram_id,
            db_pool=app_context.db_pool,
            operations=operations,  # Use the operations list defined above
            app_context=app_context,
            memo=f"PathPay fb for {tx['hash'][-6:]}"
        )
        swap_hash = response_dict["hash"]
        await wait_for_transaction_confirmation(swap_hash, app_context)

        # Fetch actual output and input
        effects_builder = AsyncEffectsCallBuilder(
            horizon_url=app_context.horizon_url,
            client=app_context.client
        ).for_transaction(swap_hash).limit(50)
        effects_response = await effects_builder.call()
        actual_output = 0.0
        input_amount = 0.0
        for effect in effects_response["_embedded"]["records"]:
            if effect["type"] == "account_credited" and effect["account"] == public_key:
                actual_output = float(effect["amount"])
                dest_code = "XLM" if effect["asset_type"] == "native" else effect["asset_code"]
            elif effect["type"] == "account_debited" and effect["account"] == public_key and effect.get("asset_type") != "native":
                input_amount = float(effect["amount"])
                send_code = "XLM" if effect["asset_type"] == "native" else effect["asset_code"]
            elif effect["type"] == "account_debited" and effect["account"] == public_key and effect.get("asset_type") == "native" and input_amount == 0:
                input_amount = float(effect["amount"]) - fee_amount  # Adjust for fee if XLM is input

        # Fetch the actual network fee
        tx_details = await AsyncTransactionsCallBuilder(
            horizon_url=app_context.horizon_url,
            client=app_context.client
        ).transaction(swap_hash).call()
        network_fee = float(tx_details["fee_charged"]) / 10000000  # Convert stroops to XLM

        response = {
            "tx_status": "PENDING",
            "hash": swap_hash,
            "fee_amount": fee_amount,  # Service fee
            "network_fee": network_fee,  # Actual network fee
            "xlm_volume": input_amount if send_code == "XLM" else actual_output if dest_code == "XLM" else await get_xlm_equivalent(app_context, dest_code, dest_issuer, actual_output),
            "input_amount": input_amount,
            "input_asset_code": send_code,
            "output_amount": actual_output,
            "output_asset_code": dest_code,
            "service_fee": fee_amount
        }

        # Log referral volume and calculate shares
        await log_xlm_volume(telegram_id, response["xlm_volume"], swap_hash, app_context.db_pool)
        await calculate_referral_shares(app_context.db_pool, telegram_id, response["service_fee"])

        return response, xdr

    except Exception as e:
        logger.error(f"SDEX fallback failed for tx {tx['hash']}: {str(e)}", exc_info=True)
        return None, None
