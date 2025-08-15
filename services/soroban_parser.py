# services/soroban_parser.py
import logging
import time
import requests
from stellar_sdk import TransactionEnvelope, Network, StrKey, MuxedAccount
from stellar_sdk.operation import InvokeHostFunction
from stellar_sdk.xdr import HostFunction, HostFunctionType, InvokeContractArgs, SCVal, SCValType, Uint64

logger = logging.getLogger(__name__)

# Define supported routers and their swap functions
SUPPORTED_ROUTERS = {
    "6033b4250e704e314fb064973d185db922cae0bd272ba5bff19aac570f12ac2f": {  # AQUA
        "swap_chained": {
            "sender_arg": 0,       # Index of sender address to replace
            "recipient_arg": 0,    # Index of recipient address (same as sender for AQUA)
            "deadline_arg": None,  # No deadline argument to update
            "amount_in_arg": 3,    # Index of amount_in in args
            "amount_out_min_arg": 4  # Index of amount_out_min in args
        }
    },
    "0dd5c710ea6a4a23b32207fd130eadf9c9ce899f4308e93e4ffe53fbaf108a04": {  # Soroswap
        "swap_exact_tokens_for_tokens": {
            "sender_arg": None,    # No sender argument to replace
            "recipient_arg": 3,    # Index of 'to' address
            "deadline_arg": 4,     # Index of deadline in args
            "amount_in_arg": 0,    # Index of amount_in in args
            "amount_out_min_arg": 1  # Index of amount_out_min in args
        },
        "swap_tokens_for_exact_tokens": {
            "sender_arg": None,    # No sender argument to replace
            "recipient_arg": 3,    # Index of 'to' address
            "deadline_arg": 4,     # Index of deadline in args
            "amount_out_arg": 0,   # Index of amount_out in args
            "amount_in_max_arg": 1 # Index of amount_in_max in args
        }
    },
    "4a07472d5713212713de5762c3b0223883b918c7ae2c4f64e7c0af65992a8aff": {  # Soroswap Aggregator
        "swap_exact_tokens_for_tokens": {
            "sender_arg": None,
            "recipient_arg": 5,    # 'to' address
            "deadline_arg": 6,     # deadline
            "amount_in_arg": 2,    # amount_in
            "amount_out_min_arg": 3  # amount_out_min
        },
        "swap_tokens_for_exact_tokens": {
            "sender_arg": None,
            "recipient_arg": 5,    # 'to' address
            "deadline_arg": 6,     # deadline
            "amount_out_arg": 2,   # amount_out
            "amount_in_max_arg": 3 # amount_in_max
        }
    }
    # Add more routers as needed, e.g., Phoenix, Blend
}

def resolve_muxed_account(muxed_address):
    """Fetch the underlying account ID from a muxed address via Horizon API."""
    try:
        response = requests.get(f"https://horizon.stellar.org/accounts/{muxed_address}")
        if response.status_code == 200:
            data = response.json()
            return data["account_id"]  # Returns the base public key (G...)
        else:
            logger.warning(f"Failed to resolve muxed account {muxed_address}: {response.status_code}")
            return None
    except Exception as e:
        logger.error(f"Error resolving muxed account {muxed_address}: {e}")
        return None

async def parse_soroban_transaction(tx, wallet, chat_id, telegram_id, app_context):
    """Parse a transaction for Soroban InvokeHostFunction operations, filtering for supported swaps."""
    if "successful" not in tx or not tx["successful"]:
        logger.info(f"Transaction {tx['hash']} not successful, skipping.")
        return None

    tx_envelope = TransactionEnvelope.from_xdr(
        tx["envelope_xdr"],
        network_passphrase=app_context.network_passphrase
    )
    operations = tx_envelope.transaction.operations
    soroban_ops = []

    for op in operations:
        if not isinstance(op, InvokeHostFunction):
            logger.info(f"Skipping non-InvokeHostFunction operation: {op.__class__.__name__}")
            continue

        # Extract the operation source account
        if op.source is None:
            op_source_account = tx["source_account"]
        elif isinstance(op.source, str):
            if op.source.startswith('M'):
                op_source_account = resolve_muxed_account(op.source)
                if op_source_account is None:
                    logger.info(f"Could not resolve muxed account {op.source}, skipping.")
                    continue
            else:
                op_source_account = op.source
        elif isinstance(op.source, MuxedAccount):
            if hasattr(op.source, 'account_muxed_id') and op.source.account_muxed_id is not None:
                logger.info(f"Skipping operation from muxed account with ID: {op.source.account_muxed_id}")
                continue
            else:
                # Standard account wrapped as MuxedAccount
                account_id = op.source.account_id
                if isinstance(account_id, str):
                    op_source_account = account_id
                elif hasattr(account_id, 'ed25519'):
                    op_source_account = StrKey.encode_ed25519_public_key(account_id.ed25519)
                else:
                    logger.warning(f"Unexpected account_id type in MuxedAccount: {type(account_id)}, skipping.")
                    continue
        else:
            logger.warning(f"Unexpected op.source type: {type(op.source)}, skipping.")
            continue

        # Compare the extracted source account with the wallet
        if op_source_account != wallet:
            logger.info(f"Soroban op source {op_source_account} does not match wallet {wallet}, skipping.")
            continue

        # Process the InvokeHostFunction operation
        if op.host_function.type != HostFunctionType.HOST_FUNCTION_TYPE_INVOKE_CONTRACT:
            logger.info(f"Skipping non-contract invocation: {op.host_function.type}")
            continue

        # Extract contract ID and function name
        contract_id = op.host_function.invoke_contract.contract_address.contract_id.hash.hex()
        function_name = op.host_function.invoke_contract.function_name.sc_symbol.decode()

        # Check if the contract and function are supported
        if contract_id not in SUPPORTED_ROUTERS:
            logger.info(f"Unsupported router contract: {contract_id}")
            continue

        if function_name not in SUPPORTED_ROUTERS[contract_id]:
            logger.info(f"Unsupported function on contract {contract_id}: {function_name}")
            continue

        # Extract original arguments
        args = op.host_function.invoke_contract.args

        # Preprocess arguments based on router config
        router_config = SUPPORTED_ROUTERS[contract_id][function_name]

        # Update deadline if applicable
        if router_config["deadline_arg"] is not None:
            new_deadline = SCVal(
                type=SCValType.SCV_U64,
                u64=Uint64(int(time.time()) + 300)  # 5 minutes from now
            )
            args[router_config["deadline_arg"]] = new_deadline
            logger.info(f"Updated deadline for {contract_id}.{function_name} to {int(time.time()) + 300}")

        # Rebuild the HostFunction with updated arguments
        new_host_function = HostFunction(
            type=HostFunctionType.HOST_FUNCTION_TYPE_INVOKE_CONTRACT,
            invoke_contract=InvokeContractArgs(
                contract_address=op.host_function.invoke_contract.contract_address,
                function_name=op.host_function.invoke_contract.function_name,
                args=args
            )
        )

        # Prepare the operation details for copying
        soroban_op = {
            "contract_id": contract_id,
            "function_name": function_name,
            "args": args,
            "auth": op.auth,
            "original_host_function": new_host_function,
            "original_auth": op.auth,
            "recipient_arg": router_config["recipient_arg"],
            "sender_arg": router_config["sender_arg"]
        }

        # Add amount arguments based on the function type
        if "amount_in_arg" in router_config:
            soroban_op["amount_in_arg"] = router_config["amount_in_arg"]
        if "amount_out_min_arg" in router_config:
            soroban_op["amount_out_min_arg"] = router_config["amount_out_min_arg"]
        if "amount_out_arg" in router_config:
            soroban_op["amount_out_arg"] = router_config["amount_out_arg"]
        if "amount_in_max_arg" in router_config:
            soroban_op["amount_in_max_arg"] = router_config["amount_in_max_arg"]

        soroban_ops.append(soroban_op)

        # Log stringified args for readability
        arg_strings = [str(arg) for arg in args]
        logger.info(f"Detected Soroban op: {contract_id}.{function_name}({arg_strings}) from {wallet}")

    if soroban_ops:
        stellar_expert_link = f"https://stellar.expert/explorer/public/tx/{tx['hash']}"
        message = (
            f"Incoming Soroban tx from {wallet[-5:]}\n"
            f"Tx: <a href='{stellar_expert_link}'>Target Swap</a>\n"
        )
        await app_context.bot.send_message(chat_id, message, parse_mode="HTML", disable_web_page_preview=True)
        return soroban_ops
    return None
