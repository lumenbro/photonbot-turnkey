import aiohttp
import asyncio
import json
import logging
import os
from datetime import datetime, timedelta
import aiofiles

logger = logging.getLogger(__name__)

# Known stablecoins for special handling
STABLECOINS = {"USDC", "EURT", "GYEN", "yUSDC"}
STABLECOIN_USD_THRESHOLD = 0.1  # 10% deviation from 1 USD for stablecoins
RETRY_ATTEMPTS = 3  # Number of retries for Stellar Expert API
RETRY_BACKOFF = 2  # Base backoff time in seconds

class PriceService:
    def __init__(self, app_context):
        self.app_context = app_context
        self.cache_file = os.path.join(os.path.dirname(__file__), "price_cache.json")
        self.price_cache = {}
        self.xlm_usd_cache = None
        self.last_updated = None
        self.cache_duration = timedelta(minutes=5)  # Fetch every 5 minutes
        self.shutdown_flag = asyncio.Event()
        self.cache_lock = asyncio.Lock()
        self._load_cache_from_file()

    def _load_cache_from_file(self):
        """Load the price cache from the file (synchronous, called during init)."""
        try:
            if os.path.exists(self.cache_file):
                with open(self.cache_file, 'r') as f:
                    data = json.load(f)
                    if not isinstance(data, dict):
                        logger.warning(f"Invalid cache format in {self.cache_file}")
                        return
                    self.price_cache = {
                        k: (float(v["price"]), datetime.fromisoformat(v["timestamp"]))
                        for k, v in data.items()
                        if isinstance(v, dict) and "price" in v and "timestamp" in v
                    }
                logger.debug(f"Loaded cache with {len(self.price_cache)} entries")
        except Exception as e:
            logger.error(f"Error loading cache: {str(e)}")

    async def _save_cache_to_file(self):
        """Save the price cache to the file."""
        logger.debug("Saving cache")
        async with self.cache_lock:
            try:
                cache_data = {
                    k: {"price": v[0], "timestamp": v[1].isoformat()}
                    for k, v in self.price_cache.items()
                }
                with open(self.cache_file, 'w') as f:
                    json.dump(cache_data, f)
                logger.debug(f"Wrote cache: {len(cache_data)} entries")
            except Exception as e:
                logger.error(f"Error saving cache: {str(e)}")

    async def fetch_asset_price_in_xlm(self, asset_code, asset_issuer):
        """Fetch the price of an asset in XLM from Stellar Expert, cached for 5 minutes."""
        logger.debug(f"Fetching price for {asset_code}:{asset_issuer}")
        if asset_code == "XLM":
            return 1.0

        cache_key = f"{asset_code}:{asset_issuer}"

        # Check cache first
        if cache_key in self.price_cache:
            price, timestamp = self.price_cache[cache_key]
            if datetime.utcnow() - timestamp < self.cache_duration:
                logger.debug(f"Using cached price for {cache_key}: {price}")
                return price

        # Fetch from Stellar Expert
        price = await self._fetch_stellar_expert_price(asset_code, asset_issuer)
        logger.debug(f"Stellar Expert price for {asset_code}:{asset_issuer}: {price} XLM")
        is_stablecoin = asset_code in STABLECOINS

        if price > 0.0:
            if is_stablecoin:
                xlm_usd = await self.fetch_xlm_usd_price()
                usd_price = price * xlm_usd if xlm_usd > 0 else 0.0
                if abs(usd_price - 1.0) / 1.0 > STABLECOIN_USD_THRESHOLD:
                    logger.warning(f"USD price {usd_price} for {asset_code}:{asset_issuer} deviates from 1.0, using ~1 USD")
                    price = 1.0 / xlm_usd if xlm_usd > 0 else price
            logger.debug(f"Using Stellar Expert price {price} XLM for {asset_code}:{asset_issuer}")
        else:
            # Default price if Stellar Expert fails
            price = 0.001
            logger.warning(f"No Stellar Expert price for {asset_code}:{asset_issuer}, using default {price} XLM")

        # Cache the price
        self.price_cache[cache_key] = (price, datetime.utcnow())
        await self._save_cache_to_file()
        logger.debug(f"Cached price for {cache_key}: {price}")
        return price

    async def _fetch_stellar_expert_price(self, asset_code, asset_issuer):
        """Fetch the price of an asset in XLM from Stellar Expert API with retries."""
        logger.debug(f"Fetching Stellar Expert price for {asset_code}:{asset_issuer}")
        for attempt in range(RETRY_ATTEMPTS):
            async with aiohttp.ClientSession() as session:
                url = f"https://api.stellar.expert/explorer/public/asset/{asset_code}-{asset_issuer}"
                try:
                    async with session.get(url) as response:
                        logger.debug(f"Stellar Expert attempt {attempt+1}: HTTP {response.status}")
                        if response.status != 200:
                            logger.warning(f"Failed Stellar Expert fetch: HTTP {response.status}")
                            if response.status == 500 and attempt < RETRY_ATTEMPTS - 1:
                                await asyncio.sleep(RETRY_BACKOFF * (2 ** attempt))
                                continue
                            return 0.0
                        data = await response.json()
                        logger.debug(f"Stellar Expert response for {asset_code}:{asset_issuer}: {data}")
                        price_usd = float(data.get("price", 0.0))
                        logger.debug(f"Extracted price_usd: {price_usd}")
                        if price_usd == 0.0:
                            market = data.get("market", {})
                            price_usd = float(market.get("price", 0.0))
                            logger.debug(f"Extracted market price_usd: {price_usd}")
                        if price_usd == 0.0:
                            logger.debug(f"No price data for {asset_code}:{asset_issuer}")
                            return 0.0
                        xlm_usd = await self.fetch_xlm_usd_price()
                        logger.debug(f"Fetched XLM/USD: {xlm_usd}")
                        price_xlm = price_usd / xlm_usd if xlm_usd > 0 else 0.0
                        logger.debug(f"Fetched Stellar Expert price: {price_xlm} XLM (USD: {price_usd}, XLM/USD: {xlm_usd})")
                        return price_xlm
                except Exception as e:
                    logger.error(f"Error fetching Stellar Expert for {asset_code}:{asset_issuer}: {str(e)}")
                    if attempt < RETRY_ATTEMPTS - 1:
                        await asyncio.sleep(RETRY_BACKOFF * (2 ** attempt))
                    else:
                        return 0.0
        return 0.0

    async def fetch_xlm_usd_price(self):
        """Fetch the XLM/USD price from CoinGecko, cached for 5 minutes."""
        logger.debug("Fetching XLM/USD price")
        if self.xlm_usd_cache and (datetime.utcnow() - self.last_updated) < self.cache_duration:
            logger.debug(f"Returning cached XLM/USD: {self.xlm_usd_cache}")
            return self.xlm_usd_cache

        async with aiohttp.ClientSession() as session:
            try:
                async with session.get("https://api.coingecko.com/api/v3/simple/price?ids=stellar&vs_currencies=usd") as response:
                    if response.status != 200:
                        logger.warning(f"Failed CoinGecko fetch: HTTP {response.status}")
                        return 0.0
                    data = await response.json()
                    price = float(data.get("stellar", {}).get("usd", 0.0))
                    self.xlm_usd_cache = price
                    self.last_updated = datetime.utcnow()
                    logger.debug(f"Fetched XLM/USD: {price}")
                    return price
            except Exception as e:
                logger.error(f"Error fetching XLM/USD: {str(e)}")
                return 0.0

    async def get_asset_value(self, asset_code, asset_issuer, balance):
        """Get the value of an asset in XLM and USD."""
        logger.debug(f"Getting value for {asset_code}:{asset_issuer}, balance {balance}")
        price_in_xlm = await self.fetch_asset_price_in_xlm(asset_code, asset_issuer)
        value_in_xlm = float(balance) * price_in_xlm
        xlm_usd_price = await self.fetch_xlm_usd_price()
        value_in_usd = value_in_xlm * xlm_usd_price if xlm_usd_price else 0.0
        logger.debug(f"Value: {value_in_xlm} XLM, ${value_in_usd} USD")
        return value_in_xlm, value_in_usd

    async def get_asset_info(self, asset_code: str, asset_issuer: str):
        """Fetch comprehensive asset information from Stellar Expert API"""
        try:
            async with aiohttp.ClientSession() as session:
                url = f"https://api.stellar.expert/explorer/public/asset/{asset_code}-{asset_issuer}"
                async with session.get(url) as response:
                    if response.status != 200:
                        logger.warning(f"Failed to fetch asset info: HTTP {response.status}")
                        return None
                    
                    data = await response.json()
                    
                    # Get current price in XLM using existing method
                    price_xlm = await self.fetch_asset_price_in_xlm(asset_code, asset_issuer)
                    xlm_usd = await self.fetch_xlm_usd_price()
                    price_usd = price_xlm * xlm_usd if xlm_usd > 0 else 0.0
                    
                    return {
                        "asset_code": asset_code,
                        "asset_issuer": asset_issuer,
                        "name": data.get("name", asset_code),
                        "domain": data.get("domain", ""),
                        "price_usd": price_usd,
                        "price_xlm": price_xlm,
                        "market_cap_usd": data.get("market_cap", 0.0),
                        "volume_24h": data.get("volume_24h", 0.0),
                        "supply": data.get("supply", 0.0),
                        "holders_count": data.get("holders_count", 0),
                        "trustlines_count": data.get("trustlines_count", 0),
                        "tags": data.get("tags", [])
                    }
                    
        except Exception as e:
            logger.error(f"Error fetching asset info from Stellar Expert: {e}")
            return None

    async def calculate_tokens_for_xlm(self, asset_code: str, asset_issuer: str, xlm_amount: float) -> float:
        """Calculate approximate token amount for given XLM amount"""
        try:
            price_xlm = await self.fetch_asset_price_in_xlm(asset_code, asset_issuer)
            if price_xlm > 0:
                return xlm_amount / price_xlm
            else:
                # Fallback calculation if price is not available
                return xlm_amount * 1000  # Placeholder: 1 XLM = 1000 tokens
        except Exception as e:
            logger.error(f"Error calculating tokens for XLM: {e}")
            return xlm_amount * 1000  # Fallback

    async def shutdown(self):
        """Shut down the PriceService."""
        logger.debug("Shutting down PriceService")
        self.shutdown_flag.set()
        await self._save_cache_to_file()
        logger.debug("PriceService shutdown complete")
