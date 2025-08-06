from aiogram import types
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import InlineKeyboardMarkup, InlineKeyboardButton
from aiogram.filters import Command
import asyncio
import logging
from stellar_sdk import Keypair

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

class CopyTradeStates(StatesGroup):
    waiting_for_wallet = State()
    waiting_for_settings = State()

async def copy_trade_menu_command(message: types.Message, streaming_service, status_update=None, user_id=None, app_context=None):
    if app_context is None:
        logger.error("app_context is None in copy_trade_menu_command")
        await message.reply("An error occurred: app_context is missing. Please try again later.")
        return
    
    if user_id is None:
        user_id = message.from_user.id
    chat_id = message.chat.id
    
    async with app_context.db_pool.acquire() as conn:
        addresses = await conn.fetch(
            "SELECT id, wallet_address, status, multiplier, fixed_amount, slippage FROM copy_trading WHERE user_id = $1",
            user_id
        )
    
    logger.info(f"Fetched addresses for user_id {user_id}: {addresses}")
    
    streaming_active = chat_id in streaming_service.tasks and any(not t.done() for t in streaming_service.tasks[chat_id].values())
    response = f"**Copy Trade Addresses, max 20 total{' (Streaming)' if streaming_active else ''}:**\n\n"
    if status_update:
        response += f"{status_update}\n\n"
    if not addresses:
        response += "No copy trade addresses added.\n"
    else:
        response += "\n".join(
            f"{'🟢' if record['status'] == 'active' else '🟠'} Copy {i} — `{addr}`"
            for i, record in enumerate(addresses, 1)
            for addr in [record['wallet_address']]
        )
    
    # Create keyboard with address buttons in rows of 4
    keyboard_rows = []
    buttons_per_row = 4  # Adjust this to control how many buttons per row (max 8 for Telegram)
    for i in range(0, len(addresses), buttons_per_row):
        row = [
            InlineKeyboardButton(
                text=f"{'🟢' if record['status'] == 'active' else '🟠'} {i + j + 1}-",
                callback_data=f"settings_{record['id']}"
            )
            for j, record in enumerate(addresses[i:i + buttons_per_row])
        ]
        keyboard_rows.append(row)
    
    # Add the remaining buttons (Stop All/Copy Trade Global, Add Address, Back)
    keyboard_rows.extend([
        [InlineKeyboardButton(
            text=f"{'🟢' if streaming_active else '🟠'} {'Stop All' if streaming_active else 'Copy Trade Global'}",
            callback_data="toggle_global_stream"
        )],
        [InlineKeyboardButton(text="➕ Add Address", callback_data="add_copy")],
        [InlineKeyboardButton(text="⬅️ Back", callback_data="back_to_main")]
    ])
    
    keyboard = InlineKeyboardMarkup(inline_keyboard=keyboard_rows)
    await message.reply(response, parse_mode="Markdown", reply_markup=keyboard)

async def process_wallet_address(message: types.Message, state: FSMContext, streaming_service, app_context):
    if app_context is None:
        logger.error("app_context is None in process_wallet_address")
        await message.reply("An error occurred: app_context is missing. Please try again later.")
        return
    
    wallet_address = message.text.strip()
    telegram_id = message.from_user.id
    
    # Validate the wallet address as a Stellar public key
    try:
        Keypair.from_public_key(wallet_address)
    except Exception as e:
        await message.reply(f"Invalid Stellar public key: {str(e)}")
        return
    
    try:
        async with app_context.db_pool.acquire() as conn:
            # Check if the wallet address already exists for this user
            exists = await conn.fetchval(
                "SELECT COUNT(*) FROM copy_trading WHERE user_id = $1 AND wallet_address = $2",
                telegram_id, wallet_address
            )
            if exists > 0:
                await message.reply("This wallet address is already added for copy trading.")
                return
            
            await conn.execute(
                "INSERT INTO copy_trading (user_id, wallet_address, status, multiplier, slippage) VALUES ($1, $2, $3, $4, $5)",
                telegram_id, wallet_address, "active", 1.0, 0.01  # Default slippage 1%
            )
        await state.clear()
        await copy_trade_menu_command(message, streaming_service, status_update="Address added successfully!", app_context=app_context)
    except Exception as e:
        logger.error(f"Error adding wallet address: {str(e)}", exc_info=True)
        await message.reply("An error occurred while adding the wallet address. Please try again later.")

async def process_copy_trade_callback(callback: types.CallbackQuery, state: FSMContext, streaming_service, app_context):
    if app_context is None:
        logger.error("app_context is None in process_copy_trade_callback")
        await callback.message.reply("An error occurred: app_context is missing. Please try again later.")
        await callback.answer()
        return
    
    action = callback.data
    user_id = callback.from_user.id
    chat_id = callback.message.chat.id
    await callback.answer()

    if action.startswith("settings_"):
        addr_id = int(action.split("_")[1])
        try:
            async with app_context.db_pool.acquire() as conn:
                address_data = await conn.fetchrow(
                    "SELECT wallet_address, status, multiplier, fixed_amount, slippage FROM copy_trading WHERE id = $1 AND user_id = $2",
                    addr_id, user_id
                )
            
            if address_data:
                wallet = address_data['wallet_address']
                status = address_data['status']
                multiplier = address_data['multiplier']
                fixed_amount = address_data['fixed_amount']
                slippage = address_data['slippage']
                active_mode = "Fixed Amount" if fixed_amount is not None else "Multiplier"
                slippage_percent = round(float(slippage * 100), 2) if slippage is not None else 1.0
                multiplier_display = round(float(multiplier), 2) if multiplier is not None else 1.0
                keyboard = InlineKeyboardMarkup(inline_keyboard=[
                    [InlineKeyboardButton(text="Toggle Active", callback_data=f"toggle_{addr_id}")],
                    [InlineKeyboardButton(text=f"Multiplier: {multiplier_display}", callback_data=f"set_multiplier_{addr_id}")],
                    [InlineKeyboardButton(text=f"Fixed Amount: {fixed_amount or 'None'}", callback_data=f"set_fixed_{addr_id}")],
                    [InlineKeyboardButton(text=f"Slippage: {slippage_percent}%", callback_data=f"set_slippage_{addr_id}")],
                    [InlineKeyboardButton(text="Clear Fixed Amount", callback_data=f"clear_fixed_{addr_id}")],
                    [InlineKeyboardButton(text="Delete Address", callback_data=f"delete_{addr_id}")],
                    [InlineKeyboardButton(text="Back", callback_data=f"back_to_copy_trade_menu_{addr_id}")]
                ])
                await callback.message.reply(
                    f"**Settings for `{wallet[:7]}...{wallet[-7:]}`**\n"
                    f"Status: {status}\n"
                    f"Active Mode: {active_mode}\n"
                    f"Multiplier: {multiplier_display}\n"
                    f"Fixed Amount: {fixed_amount or 'None'}\n"
                    f"Slippage: {slippage_percent}%",
                    parse_mode="Markdown",
                    reply_markup=keyboard
                )
                await state.set_state(CopyTradeStates.waiting_for_settings)
            else:
                await callback.message.reply("Wallet address not found.")
        except Exception as e:
            logger.error(f"Error fetching wallet settings: {str(e)}", exc_info=True)
            await callback.message.reply("An error occurred while fetching wallet settings. Please try again later.")
    
    elif action == "toggle_global_stream":
        streaming_active = chat_id in streaming_service.tasks and any(not t.done() for t in streaming_service.tasks[chat_id].values())
        if not streaming_active:
            try:
                async with app_context.db_pool.acquire() as conn:
                    wallets = await conn.fetch(
                        "SELECT wallet_address FROM copy_trading WHERE user_id = $1 AND status = 'active'",
                        user_id
                    )
                wallets = [row['wallet_address'] for row in wallets]
                if wallets:
                    streaming_service.tasks[chat_id] = {}
                    for wallet in wallets:
                        task = asyncio.create_task(streaming_service.stream_wallet(wallet, chat_id, user_id))
                        streaming_service.tasks[chat_id][wallet] = task
                    status_update = "Global streaming initiated. Please note: Streaming must be restarted to apply updated wallet settings."
                else:
                    status_update = "No active wallets to stream."
            except Exception as e:
                logger.error(f"Error starting global streaming: {str(e)}", exc_info=True)
                status_update = "An error occurred while starting global streaming."
        else:
            if chat_id in streaming_service.tasks:
                for task in list(streaming_service.tasks[chat_id].values()):
                    if not task.done():
                        task.cancel()
                await asyncio.gather(*[t for t in streaming_service.tasks[chat_id].values() if not t.done()], return_exceptions=True)
                del streaming_service.tasks[chat_id]
            status_update = "Global streaming stopped."
        await copy_trade_menu_command(callback.message, streaming_service, status_update, user_id=user_id, app_context=app_context)
    
    elif action == "add_copy":
        await callback.message.reply("Enter the wallet address to copy trades from:")
        await state.set_state(CopyTradeStates.waiting_for_wallet)
    
    elif action.startswith("toggle_"):
        addr_id = int(action.split("_")[1])
        try:
            async with app_context.db_pool.acquire() as conn:
                current_status = await conn.fetchval(
                    "SELECT status FROM copy_trading WHERE id = $1 AND user_id = $2",
                    addr_id, user_id
                )
                new_status = "inactive" if current_status == "active" else "active"
                await conn.execute(
                    "UPDATE copy_trading SET status = $1 WHERE id = $2 AND user_id = $3",
                    new_status, addr_id, user_id
                )
            await copy_trade_menu_command(callback.message, streaming_service, f"Status toggled to {new_status} for address {addr_id}", user_id=user_id, app_context=app_context)
        except Exception as e:
            logger.error(f"Error toggling status: {str(e)}", exc_info=True)
            await callback.message.reply("An error occurred while toggling the status. Please try again later.")
    
    elif action.startswith("set_multiplier_"):
        addr_id = int(action.split("_")[2])
        await callback.message.reply("Enter new multiplier value (e.g., 1.5):")
        await state.update_data(addr_id=addr_id, setting="multiplier")
        await state.set_state(CopyTradeStates.waiting_for_settings)
    
    elif action.startswith("set_fixed_"):
        addr_id = int(action.split("_")[2])
        await callback.message.reply("Enter new fixed amount (e.g., 10.0) or 'None' to disable:")
        await state.update_data(addr_id=addr_id, setting="fixed_amount")
        await state.set_state(CopyTradeStates.waiting_for_settings)
    
    elif action.startswith("set_slippage_"):
        addr_id = int(action.split("_")[2])
        await callback.message.reply("Enter new slippage percentage (e.g., 1 for 1%):")
        await state.update_data(addr_id=addr_id, setting="slippage")
        await state.set_state(CopyTradeStates.waiting_for_settings)
    
    elif action.startswith("clear_fixed_"):
        addr_id = int(action.split("_")[2])
        try:
            async with app_context.db_pool.acquire() as conn:
                await conn.execute(
                    "UPDATE copy_trading SET fixed_amount = NULL WHERE id = $1 AND user_id = $2",
                    addr_id, user_id
                )
            await copy_trade_menu_command(callback.message, streaming_service, "Fixed amount cleared, using multiplier now.", user_id=user_id, app_context=app_context)
        except Exception as e:
            logger.error(f"Error clearing fixed amount: {str(e)}", exc_info=True)
            await callback.message.reply("An error occurred while clearing the fixed amount. Please try again later.")
    
    elif action.startswith("delete_"):
        addr_id = int(action.split("_")[1])
        try:
            async with app_context.db_pool.acquire() as conn:
                await conn.execute(
                    "DELETE FROM copy_trading WHERE id = $1 AND user_id = $2",
                    addr_id, user_id
                )
            await copy_trade_menu_command(callback.message, streaming_service, "Address deleted.", user_id=user_id, app_context=app_context)
        except Exception as e:
            logger.error(f"Error deleting wallet address: {str(e)}", exc_info=True)
            await callback.message.reply("An error occurred while deleting the wallet address. Please try again later.")
    
    elif action == "back_to_menu":
        await state.clear()
        await copy_trade_menu_command(callback.message, streaming_service, user_id=user_id, app_context=app_context)
    
    elif action == "back_to_main":
        # Return to main menu by simulating a /start command
        await state.clear()
        # Delete the current message to keep the chat clean
        await callback.message.delete()
        # Simulate a /start command to return to the main menu
        await callback.message.answer("/start")

    elif action.startswith("back_to_copy_trade_menu_"):
        # Return to copy trade menu
        await state.clear()
        addr_id = int(action.split("_")[-1])  # Extract addr_id from callback data
        await copy_trade_menu_command(callback.message, streaming_service, user_id=user_id, app_context=app_context)

async def process_settings_input(message: types.Message, state: FSMContext, streaming_service, app_context):
    if app_context is None:
        logger.error("app_context is None in process_settings_input")
        await message.reply("An error occurred: app_context is missing. Please try again later.")
        return
    
    data = await state.get_data()
    addr_id = data.get("addr_id")
    setting = data.get("setting")
    
    try:
        value = message.text.strip()
        # Fetch the wallet address for the given addr_id
        async with app_context.db_pool.acquire() as conn:
            wallet_address = await conn.fetchval(
                "SELECT wallet_address FROM copy_trading WHERE id = $1 AND user_id = $2",
                addr_id, message.from_user.id
            )
        
        if not wallet_address:
            await message.reply("Wallet address not found for this ID.")
            return
        
        # Truncate the wallet address to the last 7 characters
        truncated_address = f"...{wallet_address[-7:]}"
        
        if setting == "multiplier":
            multiplier = float(value) if float(value) > 0 else 1.0
            multiplier = round(multiplier, 7)  # Round to 7 decimal places
            async with app_context.db_pool.acquire() as conn:
                await conn.execute(
                    "UPDATE copy_trading SET multiplier = $1, fixed_amount = NULL WHERE id = $2 AND user_id = $3",
                    multiplier, addr_id, message.from_user.id
                )
            status_update = f"Multiplier set to {round(multiplier, 2)} for address {truncated_address}, fixed amount cleared."
        elif setting == "fixed_amount":
            fixed_amount = float(value) if value.lower() != "none" and float(value) > 0 else None
            async with app_context.db_pool.acquire() as conn:
                await conn.execute(
                    "UPDATE copy_trading SET fixed_amount = $1, multiplier = 1.0 WHERE id = $2 AND user_id = $3",
                    fixed_amount, addr_id, message.from_user.id
                )
            status_update = f"Fixed amount set to {fixed_amount or 'None'} for address {truncated_address}, multiplier reset to 1.0."
        elif setting == "slippage":
            slippage = float(value) / 100 if float(value) >= 0 else 0.01  # Convert percentage to decimal
            slippage = round(slippage, 7)  # Round to 7 decimal places
            async with app_context.db_pool.acquire() as conn:
                await conn.execute(
                    "UPDATE copy_trading SET slippage = $1 WHERE id = $2 AND user_id = $3",
                    slippage, addr_id, message.from_user.id
                )
            status_update = f"Slippage set to {round(slippage * 100, 2)}% for address {truncated_address}."
        
        await copy_trade_menu_command(message, streaming_service, status_update, app_context=app_context)
    except ValueError:
        await message.reply("Invalid input. Please enter a valid number or 'None' for fixed amount.")
    except Exception as e:
        logger.error(f"Error updating settings: {str(e)}", exc_info=True)
        await message.reply("An error occurred while updating the settings. Please try again later.")
    
    await state.clear()

def register_copy_handlers(dp, streaming_service, app_context):
    async def menu_handler(message: types.Message):
        await copy_trade_menu_command(message, streaming_service, app_context=app_context)
    dp.message.register(menu_handler, Command("copytrade_menu"))
    
    async def callback_handler(callback: types.CallbackQuery, state: FSMContext):
        await process_copy_trade_callback(callback, state, streaming_service, app_context)
    dp.callback_query.register(
        callback_handler,
        lambda c: any(c.data.startswith(prefix) for prefix in ["settings_", "toggle_", "set_multiplier_", "set_fixed_", "set_slippage_", "clear_fixed_", "delete_", "back_to_copy_trade_menu_"]) or 
                  c.data in ["toggle_global_stream", "add_copy", "back_to_menu", "back_to_main"]
    )
    
    async def wallet_handler(message: types.Message, state: FSMContext):
        await process_wallet_address(message, state, streaming_service, app_context)
    dp.message.register(wallet_handler, CopyTradeStates.waiting_for_wallet)
    
    async def settings_handler(message: types.Message, state: FSMContext):
        await process_settings_input(message, state, streaming_service, app_context)
    dp.message.register(settings_handler, CopyTradeStates.waiting_for_settings)
