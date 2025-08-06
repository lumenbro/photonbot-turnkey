# services/streaming.py
import asyncio
import logging
from stellar_sdk import Server
from stellar_sdk.call_builder.call_builder_async import TransactionsCallBuilder as AsyncTransactionsCallBuilder
from services.copy_trading import process_trade_signal
from services.soroban_parser import parse_soroban_transaction
from services.soroban_builder import build_and_submit_soroban_transaction
from globals import AppContext
from services.referrals import log_xlm_volume, calculate_referral_shares
from services.soroban_builder import try_sdex_fallback

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# Silence aiohttp_sse_client chatter
logging.getLogger('aiohttp_sse_client').setLevel(logging.WARNING)

class StreamingService:
    def __init__(self, app_context: 'AppContext'):
        self.app_context = app_context
        self.tasks = {}
        self.shutdown_flag = app_context.shutdown_flag
        self.cursor_store = {}

    class AsyncStreamIterator:
        def __init__(self, wallet, shutdown_flag, cursor_store, server):
            self.wallet = wallet
            self.shutdown_flag = shutdown_flag
            self.stream = None
            self.cursor_store = cursor_store
            self.cursor = self.cursor_store.get(wallet, "now")
            self.closing = False
            self.lock = asyncio.Lock()
            self.server = server

        def __aiter__(self):
            return self

        async def __anext__(self):
            if self.stream is None and not self.closing:
                self.stream = self.server.transactions().for_account(self.wallet).cursor(self.cursor).stream()
                logger.info(f"Started streaming for {self.wallet} with cursor {self.cursor}")

            while not self.shutdown_flag.is_set() and not self.closing:
                try:
                    async for tx in self.stream:
                        logger.info(f"Received transaction for {self.wallet}: {tx['hash']}")
                        self.cursor = tx["paging_token"]
                        self.cursor_store[self.wallet] = self.cursor
                        return tx
                except asyncio.CancelledError:
                    logger.info(f"Stream cancelled for {self.wallet} during iteration")
                    raise
                except Exception as e:
                    logger.error(f"Stream failed for {self.wallet}: {str(e)}", exc_info=True)
                    raise StopAsyncIteration  # No retries
                await asyncio.sleep(1.0)  # Gentle polling
            raise StopAsyncIteration

        async def close(self):
            async with self.lock:
                if not self.closing and self.stream is not None:
                    self.closing = True
                    logger.info(f"Closing stream for {self.wallet}")
                    try:
                        if hasattr(self.stream, 'aclose'):  # Check for async generator
                            await self.stream.aclose()  # Properly close the stream
                        await asyncio.sleep(0.5)  # Brief wait to ensure closure
                        logger.info(f"Stream closed for {self.wallet}")
                    except Exception as e:
                        logger.warning(f"Failed to close stream for {self.wallet}: {str(e)}", exc_info=True)
                    finally:
                        self.stream = None
                        self.closing = False

    async def async_stream_transactions(self, wallet):
        return self.AsyncStreamIterator(wallet, self.shutdown_flag, self.cursor_store, self.app_context.server)

    async def stream_wallet(self, wallet, chat_id, telegram_id, settings=None):
        stream_iter = await self.async_stream_transactions(wallet)
        try:
            async for tx in stream_iter:
                if not tx.get("successful", False):
                    logger.warning(f"Skipping failed tx {tx['hash']}")
                    continue
                try:
                    soroban_ops = await parse_soroban_transaction(tx, wallet, chat_id, telegram_id, self.app_context)
                    if soroban_ops:
                        response, xdr = await build_and_submit_soroban_transaction(
                            telegram_id, soroban_ops, self.app_context, original_tx_hash=tx["hash"], trader_wallet=wallet, use_rpc=False
                        )
                        if response:
                            # Fetch network fee for Soroban trade
                            tx_details = await AsyncTransactionsCallBuilder(
                                horizon_url=self.app_context.horizon_url,
                                client=self.app_context.client
                            ).transaction(response["hash"]).call()
                            network_fee = float(tx_details["fee_charged"]) / 10000000
                            total_fee = response["service_fee"] + network_fee

                            # Soroban succeeded
                            stellar_expert_link = f"https://stellar.expert/explorer/public/tx/{response['hash']}"
                            message = (
                                f"Copied Soroban trade from {wallet[-5:]}\n"
                                f"Sent: {response['input_amount']} {response['input_asset_code']}\n"
                                f"Received: {response['output_amount']} {response['output_asset_code']}\n"
                                f"Fee: {total_fee:.7f} XLM (Network: {network_fee:.7f} XLM, Service: {response['service_fee']:.7f} XLM)\n"
                                f"Tx: <a href='{stellar_expert_link}'>View on explorer</a>\n"
                            )
                            await self.app_context.bot.send_message(chat_id, message, parse_mode="HTML", disable_web_page_preview=True)
                        else:
                            # Soroban failed, try SDEX
                            logger.warning(f"Soroban failed for tx {tx['hash']}. Attempting SDEX fallback.")
                            response, xdr = await try_sdex_fallback(telegram_id, tx, wallet, chat_id, self.app_context)
                            if response:
                                # Fetch network fee for SDEX fallback trade
                                tx_details = await AsyncTransactionsCallBuilder(
                                    horizon_url=self.app_context.horizon_url,
                                    client=self.app_context.client
                                ).transaction(response["hash"]).call()
                                network_fee = float(tx_details["fee_charged"]) / 10000000
                                total_fee = response["service_fee"] + network_fee

                                stellar_expert_link = f"https://stellar.expert/explorer/public/tx/{response['hash']}"
                                message = (
                                    f"Copied trade via SDEX fallback from {wallet[-5:]}\n"
                                    f"Sent: {response['input_amount']} {response['input_asset_code']}\n"
                                    f"Received: {response['output_amount']} {response['output_asset_code']}\n"
                                    f"Fee: {total_fee:.7f} XLM (Network: {network_fee:.7f} XLM, Service: {response['service_fee']:.7f} XLM)\n"
                                    f"Tx: <a href='{stellar_expert_link}'>View on Explorer</a>\n"
                                )
                                await self.app_context.bot.send_message(chat_id, message, parse_mode="HTML", disable_web_page_preview=True)
                            else:
                                # Define response as a default dict in case try_sdex_fallback didn't set it
                                response = response if 'response' in locals() else {'hash': 'N/A'}
                                stellar_expert_link = f"https://stellar.expert/explorer/public/tx/{response.get('hash', 'N/A')}"
                                message = (
                                    f"Copied trade via SDEX fallback from {wallet[-5:]}\n"
                                    f"Operation failed for wallet {wallet[-5:]}: SDEX fallback failed.\n"
                                    f"Tx: <a href='{stellar_expert_link}'>View on Explorer</a>\n"
                                    f"This may be due to low liquidity; consider increasing slippage tolerance."
                                )
                                await self.app_context.bot.send_message(chat_id, message, parse_mode="HTML", disable_web_page_preview=True)
                                logger.warning(f"SDEX fallback also failed for tx {tx['hash']}.")
                    else:
                        await process_trade_signal(wallet, tx, chat_id, telegram_id, self.app_context)
                except Exception as e:
                    logger.error(f"Error processing transaction {tx.get('hash', 'unknown')} for wallet {wallet}: {str(e)}", exc_info=True)
                    continue
        except asyncio.CancelledError:
            logger.info(f"Streaming cancelled for {wallet}")
            raise
        except Exception as e:
            logger.error(f"Streaming loop failed for {wallet}: {str(e)}", exc_info=True)
        finally:
            await stream_iter.close()

    async def start_streaming(self, chat_id, telegram_id):
        async with self.app_context.stream_lock:
            if chat_id in self.tasks and any(not t.done() for t in self.tasks[chat_id].values()):
                logger.info(f"Streaming already active for chat_id: {chat_id}")
                return False
            async with self.app_context.db_pool.acquire() as conn:
                try:
                    wallets = await conn.fetch(
                        "SELECT wallet_address FROM copy_trading WHERE user_id = $1 AND status = 'active'",
                        telegram_id
                    )
                    wallets = set(row['wallet_address'] for row in wallets)
                    logger.info(f"Fetched wallets for user_id {telegram_id}: {wallets}")
                except Exception as e:
                    logger.error(f"Database error: {str(e)}", exc_info=True)
                    wallets = set()
            
            if not wallets:
                await self.app_context.bot.send_message(chat_id, "No active wallets to stream.")
                return False
            
            self.tasks[chat_id] = {}
            for wallet in wallets:
                if wallet not in self.tasks.get(chat_id, {}):
                    task = asyncio.create_task(self.stream_wallet(wallet, chat_id, telegram_id))
                    self.app_context.tasks.append(task)
                    self.tasks[chat_id][wallet] = task
                    logger.info(f"Started streaming task for {wallet}")
            return True

    async def stop_streaming(self, chat_id):
        async with self.app_context.stream_lock:
            if chat_id not in self.tasks:
                logger.info(f"No streaming tasks to stop for chat_id: {chat_id}")
                return False
            tasks_to_cancel = []
            for wallet, task in list(self.tasks[chat_id].items()):
                if not task.done():
                    logger.info(f"Cancelling streaming task for wallet {wallet}")
                    task.cancel()
                    tasks_to_cancel.append(task)
            if tasks_to_cancel:
                try:
                    await asyncio.gather(*tasks_to_cancel, return_exceptions=True)
                except Exception as e:
                    logger.error(f"Error cancelling tasks for chat_id {chat_id}: {str(e)}", exc_info=True)
            if chat_id in self.tasks:
                del self.tasks[chat_id]
            logger.info(f"Stopped streaming for chat_id: {chat_id}")
            return True
