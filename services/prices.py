# services/prices.py
import asyncio
import redis.asyncio as redis
from stellar_sdk import Asset, scval, Network
from stellar_sdk.contract.contract_client_async import ContractClientAsync
from stellar_sdk.soroban_server_async import SorobanServerAsync
from stellar_sdk.call_builder.call_builder_async import OrderbookCallBuilder as AsyncOrderbookCallBuilder
from stellar_sdk.call_builder.call_builder_async.strict_receive_paths_call_builder import StrictReceivePathsCallBuilder
from stellar_sdk.client.aiohttp_client import AiohttpClient
from aiohttp_client_cache import CachedSession
from tenacity import retry, stop_after_attempt, wait_exponential
import logging
from typing import Optional, List, Tuple

logger = logging.getLogger(__name__)

# Hardcode the network passphrase for mainnet
NETWORK_PASSPHRASE = Network.PUBLIC_NETWORK_PASSPHRASE  # "Public Global Stellar Network ; September 2015"

# Initialize Redis client with error handling
try:
    redis_client = redis.Redis(host="localhost", port=6379, decode_responses=True)
except Exception as e:
    logger.warning(f"Failed to initialize Redis client: {e}. Proceeding without caching.")
    redis_client = None

@retry(stop=stop_after_attempt(3), wait=wait_exponential(min=1, max=10))
async def get_asset_price(
    app_context,
    asset_code: str,
    issuer: str,
    user_public_key: Optional[str] = None
) -> Optional[float]:
    """
    Fetch price (XLM per asset) using Reflector's lastprice.
    Uses user public key or fee wallet. Caches in Redis if available.
    Falls back to order book with path-based check.
    """
    cache_key = f"price:{asset_code}:{issuer}"
    cached_price = None
    if redis_client:
        try:
            cached_price = await redis_client.get(cache_key)
            if cached_price:
                logger.debug(f"Cache hit for {cache_key}: {cached_price}")
                return float(cached_price)
        except Exception as e:
            logger.warning(f"Redis cache get failed: {e}. Proceeding without cache.")

    contract_id = "CAFJZQWSED6YAWZU3GWRTOCNPPCGBN32L7QV43XX5LZLFTK6JLN34DLN"
    source_account = user_public_key or app_context.fee_wallet

    # Use the free mainnet RPC endpoint
    rpc_url = "https://mainnet.sorobanrpc.com"
    client = AiohttpClient()
    async with client:
        soroban_server = SorobanServerAsync(rpc_url, client=client)
        try:
            # Construct the ContractClientAsync with the standalone server
            contract_client = ContractClientAsync(
                contract_id=contract_id,
                rpc_url=rpc_url,
                network_passphrase=NETWORK_PASSPHRASE,
                request_client=client
            )

            async with contract_client:
                # Construct the Stellar asset parameter using a dict
                asset_struct = scval.to_struct({
                    "code": scval.to_string(asset_code),
                    "issuer": scval.to_address(issuer)
                })
                params = [scval.to_enum("Stellar", asset_struct)]

                # Invoke lastprice
                assembled_tx = await contract_client.invoke(
                    function_name="lastprice",
                    parameters=params,
                    source=source_account,
                    parse_result_xdr_fn=lambda v: v
                )

                # Simulate and parse result
                result = await assembled_tx.simulate()
                if not result.results or len(result.results) == 0:
                    logger.warning(f"Reflector: No price for {asset_code}:{issuer}")
                    raise ValueError("No result from lastprice")

                # Parse PriceData struct: { price: i128, timestamp: u64 }
                price_data = scval.from_scval(result.results[0].xdr)
                if not price_data.is_struct():
                    logger.warning(f"Reflector: Invalid price data for {asset_code}:{issuer}")
                    raise ValueError("Invalid price data format")

                price_struct = price_data.struct
                price = price_struct["price"].to_int128()
                # Fetch decimals to scale the price
                decimals_tx = await contract_client.invoke(
                    function_name="decimals",
                    source=source_account,
                    parse_result_xdr_fn=lambda v: v
                )
                decimals_result = await decimals_tx.simulate()
                decimals = scval.from_scval(decimals_result.results[0].xdr).to_uint32()

                price_float = price / (10 ** decimals)
                if redis_client:
                    try:
                        await redis_client.setex(cache_key, 10, price_float)
                    except Exception as e:
                        logger.warning(f"Redis cache set failed: {e}. Continuing without cache.")
                logger.info(f"Reflector price for {asset_code}:{issuer}: {price_float} XLM")
                return price_float

        except Exception as e:
            logger.error(f"Reflector failed for {asset_code}:{issuer} with RPC {rpc_url}: {e}")
        finally:
            await soroban_server.close()

    # Fallback to order book using path-based check
    try:
        asset = Asset(asset_code, issuer)
        native_asset = Asset.native()
        dest_amount = 1.0  # Use 1 unit to get the price per unit

        builder = StrictReceivePathsCallBuilder(
            horizon_url=app_context.horizon_url,
            client=app_context.client,
            source=[native_asset],
            destination_asset=asset,
            destination_amount=str(dest_amount)
        ).limit(10)

        logger.info(f"Querying paths for price check: {builder.horizon_url}/paths/strict-receive with params: {builder.params}")
        paths_response = await builder.call()
        logger.info(f"Paths response: {paths_response}")

        paths = paths_response.get("_embedded", {}).get("records", [])
        if not paths:
            logger.warning(f"No paths found to price {asset_code} with XLM - insufficient liquidity")
            # Fall back to direct order book check
            order_book_builder = AsyncOrderbookCallBuilder(
                horizon_url=app_context.horizon_url,
                client=app_context.client,
                selling=asset,
                buying=native_asset
            ).limit(1)
            order_book = await order_book_builder.call()
            bids = order_book.get("bids", [])
            if bids:
                price = float(bids[0]["price"])
                if redis_client:
                    try:
                        await redis_client.setex(cache_key, 10, price)
                    except Exception as e:
                        logger.warning(f"Redis cache set failed: {e}. Continuing without cache.")
                logger.info(f"Direct order book price for {asset_code}:{issuer}: {price} XLM")
                return price
            logger.warning(f"No bids found in direct order book for {asset_code}:{issuer}")
            return None

        paths.sort(key=lambda p: (float(p["source_amount"]), len(p["path"])))
        selected_path = None
        for path in paths:
            min_source_amount = float(path["source_amount"])
            logger.info(f"Evaluating path with source amount: {min_source_amount} XLM (hops: {len(path['path'])})")

            path_assets = [native_asset] + [Asset(p["asset_code"], p["asset_issuer"]) for p in path["path"]] + [asset]
            liquidity_ok = True
            for i in range(len(path_assets) - 1):
                selling_asset = path_assets[i]
                buying_asset = path_assets[i + 1]
                order_book_builder = AsyncOrderbookCallBuilder(
                    horizon_url=app_context.horizon_url,
                    client=app_context.client,
                    selling=selling_asset,
                    buying=buying_asset
                ).limit(10)
                order_book = await order_book_builder.call()
                asks = order_book.get("asks", [])
                if not asks:
                    logger.warning(f"No asks found for {selling_asset.code} -> {buying_asset.code} in path")
                    liquidity_ok = False
                    break

                total_dest_amount = 0.0
                for ask in asks:
                    ask_price = float(ask["price"])
                    ask_amount = float(ask["amount"])
                    total_dest_amount += ask_amount

                if total_dest_amount < dest_amount:
                    logger.warning(f"Insufficient ask amount for {selling_asset.code} -> {buying_asset.code}: available {total_dest_amount}, required {dest_amount}")
                    liquidity_ok = False
                    break

            if liquidity_ok:
                selected_path = path
                break

        if not selected_path:
            logger.warning(f"No viable path found to price {asset_code} with XLM - insufficient liquidity in all paths")
            # Fall back to direct order book check
            order_book_builder = AsyncOrderbookCallBuilder(
                horizon_url=app_context.horizon_url,
                client=app_context.client,
                selling=asset,
                buying=native_asset
            ).limit(1)
            order_book = await order_book_builder.call()
            bids = order_book.get("bids", [])
            if bids:
                price = float(bids[0]["price"])
                if redis_client:
                    try:
                        await redis_client.setex(cache_key, 10, price)
                    except Exception as e:
                        logger.warning(f"Redis cache set failed: {e}. Continuing without cache.")
                logger.info(f"Direct order book price for {asset_code}:{issuer}: {price} XLM")
                return price
            logger.warning(f"No bids found in direct order book for {asset_code}:{issuer}")
            return None

        min_source_amount = float(selected_path["source_amount"])
        logger.info(f"Selected path source amount for price: {min_source_amount} XLM (hops: {len(selected_path['path'])})")

        # Price per unit (XLM per asset) = source_amount / dest_amount
        price = min_source_amount / dest_amount
        if redis_client:
            try:
                await redis_client.setex(cache_key, 10, price)
            except Exception as e:
                logger.warning(f"Redis cache set failed: {e}. Continuing without cache.")
        logger.info(f"Order book price for {asset_code}:{issuer}: {price} XLM")
        return price

    except Exception as e:
        logger.error(f"Order book price check failed for {asset_code}:{issuer}: {e}")
        return None

async def get_supported_assets(app_context) -> List[Tuple[str, str]]:
    """
    Fetch the list of supported assets from Reflector.
    Returns a list of (code, issuer) tuples.
    """
    contract_id = "CAFJZQWSED6YAWZU3GWRTOCNPPCGBN32L7QV43XX5LZLFTK6JLN34DLN"
    source_account = app_context.fee_wallet

    # Use the free mainnet RPC endpoint
    rpc_url = "https://mainnet.sorobanrpc.com"
    client = AiohttpClient()
    async with client:
        soroban_server = SorobanServerAsync(rpc_url, client=client)
        try:
            contract_client = ContractClientAsync(
                contract_id=contract_id,
                rpc_url=rpc_url,
                network_passphrase=NETWORK_PASSPHRASE,
                request_client=client
            )

            async with contract_client:
                assembled_tx = await contract_client.invoke(
                    function_name="assets",
                    source=source_account,
                    parse_result_xdr_fn=lambda v: v
                )
                result = await assembled_tx.simulate()
                assets = scval.from_scval(result.results[0].xdr).to_vec()
                supported_assets = []
                for asset in assets:
                    code = asset.struct["code"].string.decode()
                    issuer_scval = asset.struct["issuer"]
                    issuer = issuer_scval.address.account_id.account_id.decode()
                    supported_assets.append((code, issuer))
                logger.info(f"Supported assets by Reflector: {supported_assets}")
                return supported_assets
        except Exception as e:
            logger.error(f"Failed to fetch supported assets from Reflector with RPC {rpc_url}: {e}")
            return []
        finally:
            await soroban_server.close()