import logging
from aiogram import types
from aiogram.fsm.state import State, StatesGroup
from aiogram.fsm.context import FSMContext
from aiogram.types import InlineKeyboardMarkup, InlineKeyboardButton, WebAppInfo, Message
from aiogram.filters import Command, CommandStart
from core.stellar import build_and_submit_transaction, has_trustline, parse_asset, load_account_async
from stellar_sdk import Asset, PathPaymentStrictReceive, ChangeTrust, Payment, Keypair
from stellar_sdk.exceptions import NotFoundError
from handlers.copy_trading import copy_trade_menu_command
from services.streaming import StreamingService
from services.trade_services import perform_buy, perform_sell
from services.referrals import log_xlm_volume, calculate_referral_shares, export_unpaid_rewards, daily_payout
from globals import is_founder
import secrets
import json
import os
import asyncio
import base64
from datetime import datetime
import uuid
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.hazmat.primitives import serialization, hashes
from sessions import create_or_refresh_session
from functools import partial
import jwt
import time
from handlers.walletmanagement import process_wallet_management_callback, process_main_menu_callback

logging.basicConfig(level=logging.DEBUG)
logger = logging.getLogger(__name__)

class RegisterStates(StatesGroup):
    waiting_for_email = State()

class BuySellStates(StatesGroup):
    waiting_for_asset = State()
    waiting_for_amount = State()

class WithdrawStates(StatesGroup):
    waiting_for_asset = State()
    waiting_for_address = State()
    waiting_for_amount = State()
    waiting_for_confirmation = State()

class ReferralStates(StatesGroup):
    referral_code = State()

class TrustlineStates(StatesGroup):
    waiting_for_asset_to_add = State()
    waiting_for_asset_to_remove = State()


welcome_text = """
Welcome to @lumenbrobot!
Trade assets on Stellar with ease.
Use the buttons below to buy, sell, check balance, or manage copy trading.
"""

main_menu_keyboard = InlineKeyboardMarkup(inline_keyboard=[
    [InlineKeyboardButton(text="Buy", callback_data="buy"),
     InlineKeyboardButton(text="Sell", callback_data="sell")],
    [InlineKeyboardButton(text="Check Balance", callback_data="balance"),
     InlineKeyboardButton(text="Copy Trading", callback_data="copy_trading")],
    [InlineKeyboardButton(text="Withdraw", callback_data="withdraw"),
     InlineKeyboardButton(text="Referrals", callback_data="wallets")],
    [InlineKeyboardButton(text="Add Trustline", callback_data="add_trustline"),
     InlineKeyboardButton(text="Remove Trustline", callback_data="remove_trustline")],
    [InlineKeyboardButton(text="Wallet Management", callback_data="wallet_management")],
    [InlineKeyboardButton(text="Help/FAQ", callback_data="help_faq")]
])

async def get_referral_link(telegram_id: int, bot, db_pool) -> str:
    """
    Retrieve or generate a referral link for the user.
    Returns a link in the format: https://t.me/{bot_username}?start={referral_code}
    """
    bot_info = await bot.get_me()
    bot_username = bot_info.username

    async with db_pool.acquire() as conn:
        referral_code = await conn.fetchval(
            "SELECT referral_code FROM users WHERE telegram_id = $1",
            telegram_id
        )
        if not referral_code:
            # Since we don't have access to the username here, use a random code as a fallback
            # Ideally, the referral code should always be set during registration
            referral_code = f"ref-{secrets.token_urlsafe(8)}"
            await conn.execute(
                "UPDATE users SET referral_code = $1 WHERE telegram_id = $2",
                referral_code, telegram_id
            )
    return f"https://t.me/{bot_username}?start={referral_code}"

async def generate_welcome_message(telegram_id, app_context):
    bot_info = await app_context.bot.get_me()
    bot_username = bot_info.username
    try:
        public_key = await app_context.load_public_key(telegram_id)
        # Fetch the referral link for the user
        referral_link = await get_referral_link(telegram_id, app_context.bot, app_context.db_pool)

        # Check if the user is a pioneer
        async with app_context.db_pool.acquire() as conn:
            is_pioneer = await conn.fetchval(
                "SELECT 1 FROM founders WHERE telegram_id = $1", telegram_id
            )
            # Check if the user was referred (i.e., has a referrer in the referrals table)
            is_referred = await conn.fetchval(
                "SELECT 1 FROM referrals WHERE referee_id = $1", telegram_id
            )

        pioneer_status = "\n*Status*: You are a pioneer! 🎉\n" if is_pioneer else ""
        # Add disclaimer for referred users
        referral_disclaimer = (
            "\n*Referral Discount*: You've received a 10% discount on fees because you were referred! "
            "Note: If your referrer unregisters, you may lose this discount.\n"
        ) if is_referred else ""

        try:
            account = await load_account_async(public_key, app_context)
            xlm_balance = float(next((b["balance"] for b in account["balances"] if b["asset_type"] == "native"), "0"))
            welcome_text = (
                f"*Welcome to @{bot_username}!*\n"
                f"Jump into Stellar trading with ease!\n\n"
                f"*Your Wallet:* `{public_key}`\n"
                f"*XLM Balance:* {xlm_balance:.7f}\n"
                f"{pioneer_status}"  # Add pioneer status here
                f"{referral_disclaimer}"  # Add referral disclaimer here
                f"Trade issued assets and Soroban SAC, stream copy trade wallets, and earn rewards with referrals.\n"
                f"Invite friends and earn rewards! Your referral link: `{referral_link}`\n\n"
                f"Use the buttons below to get started.\n\n"
                f"*New Users* Fund your wallet with XLM to trade. See /help for wallet and security tips.\n"
                f"*Note:* Soroban supported for copy trades!"
            )
        except NotFoundError:
            welcome_text = (
                f"*Welcome to @{bot_username}!*\n"
                f"Jump into Stellar trading with ease!\n\n"
                f"*Your Wallet:* `{public_key}`\n"
                f"*XLM Balance:* Not funded\n"
                f"{pioneer_status}"  # Add pioneer status here
                f"{referral_disclaimer}"  # Add referral disclaimer here
                f"Your wallet needs XLM to start trading. Send XLM to your public key from an exchange "
                f"(e.g., Coinbase, Kraken, Lobstr).\n\n"
                f"Trade issued assets and Soroban SAC, stream copy trade wallets, and earn rewards with referrals.\n"
                f"Invite friends and earn rewards! Your referral link: `{referral_link}`\n\n"
                f"Use the buttons below to get started. See /help for wallet and security tips.\n"
                f"*Note:* Soroban supported for copy trades!"
            )
    except Exception as e:
        logger.error(f"Error fetching wallet info for welcome message: {str(e)}", exc_info=True)
        welcome_text = (
            f"*Welcome to @{bot_username}!*\n"
            f"Jump into Stellar trading with ease!\n\n"
            f"Trade issued assets and Soroban SAC, stream copy trade wallets, and earn rewards with referrals.\n"
            f"Use the buttons below to get started.\n\n"
            f"*New Users* Fund your wallet with XLM to trade. See /help for wallet and security tips.\n"
            f"*Note:* Soroban supported for copy trades!"
        )
    return welcome_text

async def start_command(message: types.Message, app_context, streaming_service: StreamingService, state: FSMContext):
    telegram_id = message.from_user.id
    logger.info(f"Start command: from_user.id={telegram_id}, chat_id={message.chat.id}, is_group={message.chat.type == 'group'}")
    chat_id = message.chat.id

    # Log the raw message text
    text = message.text.strip()
    logger.debug(f"Raw start command text: '{text}'")

    # Parse the parameter from message.text
    parameter = None
    if text.startswith('/start'):
        parts = text.split(maxsplit=1)
        parameter = parts[1].strip().lower() if len(parts) > 1 else None
        logger.debug(f"Detected parameter from text: '{parameter}'")

    founder_signup = False
    # Check for founder-signup (to be updated to pioneer-signup)
    if parameter == 'pioneer-signup':
        founder_signup = True
        logger.info(f"Founder sign-up detected for user {telegram_id}")

    if founder_signup:
        logger.info(f"Founder sign-up detected for user {telegram_id}")
        async with app_context.db_pool.acquire() as conn:
            exists = await conn.fetchval("SELECT telegram_id FROM users WHERE telegram_id = $1", telegram_id)
        if not exists and message.from_user.is_bot:
            logger.info(f"Ignoring start command from bot itself for telegram_id {telegram_id}")
            return
        elif not exists:
            # User needs to register first; redirect to registration
            await message.reply("You’re not registered yet. Let’s get you set up as a founder. Use /register to proceed.")
            await state.update_data(founder_signup=True)
            logger.info(f"Set founder_signup=True in state for user {telegram_id}")
            return
        else:
            # User is registered; attempt to add them as a founder
            try:
                await add_founder(telegram_id, app_context.db_pool)
                await message.reply(
                "🎉 Congratulations! You've been registered as a pioneer.\n\n"
                "Please use /start to proceed to the main menu."
                )
            except ValueError as e:
                # Handle the case where the founder limit is reached or other validation errors
                await message.reply(str(e))
            except Exception as e:
                # Handle other errors (e.g., database issues)
                await message.reply("An error occurred while registering you as a pioneer. Please try again later.")
                logger.error(f"Error adding pioneer: {str(e)}", exc_info=True)
            return  # Exit after handling pioneer sign-up

    # Existing referral logic
    if parameter and 'ref-' in parameter:
        referral_code = parameter  # Store the full referral code including 'ref-' prefix
        await state.update_data(referral_code=referral_code)
        logger.info(f"Stored referral code {referral_code} in state for user {telegram_id}")
    else:
        logger.info("No parameter or unrecognized parameter provided with /start command")

    # Existing logic for registered users
    async with app_context.db_pool.acquire() as conn:
        exists = await conn.fetchval("SELECT telegram_id FROM users WHERE telegram_id = $1", telegram_id)
    if not exists and message.from_user.is_bot:
        logger.info(f"Ignoring start command from bot itself for telegram_id {telegram_id}")
        return
    elif not exists:
        await message.reply("You’re not registered yet. Use /register to get started.")
    else:
        welcome_text = await generate_welcome_message(telegram_id, app_context)
        await message.reply(welcome_text, reply_markup=main_menu_keyboard, parse_mode="Markdown")

async def cancel_command(message: types.Message, state: FSMContext):
    await state.clear()
    await message.reply("Action cancelled. Use /start to begin again.")

async def get_founder_count(db_pool):
    async with db_pool.acquire() as conn:
        return await conn.fetchval("SELECT COUNT(*) FROM founders")

async def add_founder(telegram_id, db_pool):
    async with db_pool.acquire() as conn:
        # Check if the user was referred (is a referee)
        referral_exists = await conn.fetchval(
            "SELECT 1 FROM referrals WHERE referee_id = $1", telegram_id
        )
        if referral_exists:
            raise ValueError("Users who were referred cannot become pioneers. Click /start to proceed.")

        # Check the current number of founders (for user-friendly error handling)
        founder_count = await conn.fetchval("SELECT COUNT(*) FROM founders")
        if founder_count >= 25:
            raise ValueError("Sorry, the founder program is full! Only 25 slots are available.")

        # Ensure the user exists in the users table
        user_exists = await conn.fetchval(
            "SELECT 1 FROM users WHERE telegram_id = $1", telegram_id
        )
        if not user_exists:
            # Add the user with a placeholder referral code and public key
            await conn.execute(
                "INSERT INTO users (telegram_id, referral_code, public_key) VALUES ($1, $2, $3)",
                telegram_id, f"FOUNDER_{telegram_id}", f"PUBLIC_KEY_{telegram_id}"
            )

        # Attempt the insert into founders (the database trigger will enforce the limit)
        try:
            await conn.execute(
                "INSERT INTO founders (telegram_id) VALUES ($1) ON CONFLICT (telegram_id) DO NOTHING",
                telegram_id
            )
        except Exception as e:
            if "Cannot add more founders" in str(e):
                raise ValueError("Sorry, the founder program is full! Only 25 slots are available.")
            raise  # Re-raise other errors

def canonicalize_json(obj):
    """Canonicalize JSON per RFC 8785."""
    return json.dumps(obj, sort_keys=True, separators=(",", ":"))

async def register_command(message: types.Message, app_context, state: FSMContext):
    telegram_id = message.from_user.id
    username = message.from_user.username
    logger.info(f"Register command: from_user.id={telegram_id}, chat_id={message.chat.id}, is_group={message.chat.type == 'group'}")
    chat_id = message.chat.id

    # Fetch username if None
    if not username:
        try:
            chat = await app_context.bot.get_chat(telegram_id)
            username = chat.username
            logger.info(f"Fetched username for {telegram_id}: {username}")
        except Exception as e:
            logger.error(f"Failed to fetch username for {telegram_id}: {str(e)}")
            username = None

    # Check if user exists
    async with app_context.db_pool.acquire() as conn:
        exists = await conn.fetchval("SELECT telegram_id FROM users WHERE telegram_id = $1", telegram_id)
        if exists:
            await message.reply("You’re already registered!")
            return

    # Handle referral code
    referral_code = None
    referrer_id = None
    if state:
        data = await state.get_data()
        referral_code = data.get('referral_code')

    if not referral_code:
        text = message.text.strip()
        if text.startswith('/start') or text.startswith('/register'):
            parts = text.split(maxsplit=1)
            if len(parts) > 1:
                args = parts[1]
                if 'ref-' in args:
                    referral_code = args.split('ref-')[1]
                else:
                    referral_code = args.strip()

    if not referral_code:
        await message.reply("Do you have a referral code? Enter it (e.g., ref-tgusername) or reply 'none'.")
        await state.set_state(ReferralStates.referral_code)
        return

    if referral_code and referral_code.lower() != 'none':
        async with app_context.db_pool.acquire() as conn:
            referrer_id = await conn.fetchval(
                "SELECT telegram_id FROM users WHERE LOWER(referral_code) = LOWER($1)",
                referral_code
            )
        if referrer_id:
            logger.info(f"Found referrer {referrer_id} for referral_code {referral_code}")
        else:
            logger.warning(f"No referrer found for {referral_code}")
            await message.reply("Invalid referral code. Proceeding without a referrer.")

    bot_id = app_context.bot.id
    if telegram_id == bot_id:
        logger.error(f"Attempted registration with bot ID {telegram_id}")
        await message.reply("Bot cannot register itself!")
        await state.clear()
        return

    # Generate JWT token
    jwt_secret = os.getenv('JWT_SECRET')  # Set in .env
    token = jwt.encode({
        'telegram_id': telegram_id,
        'referrer_id': referrer_id,
        'exp': time.time() + 600  # 10min expiry
    }, jwt_secret, algorithm='HS256')

    # Send link button (to lumenbro.com or local for test)
    mini_app_url = f"https://lumenbro.com/mini-app/index.html?action=register&referrer_id={referrer_id or ''}"
    keyboard = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="Link Telegram to Turnkey", web_app=WebAppInfo(url=mini_app_url))]
    ])
    await message.reply("Open Mini App to save Turnkey API keys securely to Telegram Cloud:", reply_markup=keyboard)
    await state.clear()

async def process_referral_code(message: types.Message, state: FSMContext, app_context):
    referral_code = message.text.strip()
    telegram_id = message.from_user.id
    username = message.from_user.username
    chat_id = message.chat.id

    # Fetch username if None
    if not username:
        try:
            chat = await app_context.bot.get_chat(telegram_id)
            username = chat.username
            logger.info(f"Fetched username for {telegram_id}: {username}")
        except Exception as e:
            logger.error(f"Failed to fetch username for {telegram_id}: {str(e)}")
            username = None

    if referral_code.lower() == 'none':
        referral_code = None

    referrer_id = None
    if referral_code:
        async with app_context.db_pool.acquire() as conn:
            referrer_id = await conn.fetchval(
                "SELECT telegram_id FROM users WHERE LOWER(referral_code) = LOWER($1)",
                referral_code
            )
        if referrer_id:
            logger.info(f"Found referrer {referrer_id} for {referral_code}")
        else:
            logger.warning(f"No referrer found for {referral_code}")
            await message.reply("Invalid referral code. Proceeding without a referrer.")

    bot_id = app_context.bot.id
    if telegram_id == bot_id:
        logger.error(f"Attempted registration with bot ID {telegram_id}")
        await message.reply("Bot cannot register itself!")
        await state.clear()
        return

    # Generate JWT token
    jwt_secret = os.getenv('JWT_SECRET')  # Set in .env
    token = jwt.encode({
        'telegram_id': telegram_id,
        'referrer_id': referrer_id,
        'exp': time.time() + 600  # 10min expiry
    }, jwt_secret, algorithm='HS256')

    # Send link button (to lumenbro.com or local for test)
    mini_app_url = f"https://lumenbro.com/mini-app/index.html?action=register&referrer_id={referrer_id or ''}"
    keyboard = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="Link Telegram to Turnkey", web_app=WebAppInfo(url=mini_app_url))]
    ])
    await message.reply("Open Mini App to save Turnkey API keys securely to Telegram Cloud:", reply_markup=keyboard)
    await state.clear()

async def confirm_seed_saved(callback: types.CallbackQuery, app_context, state: FSMContext):
    telegram_id = callback.from_user.id
    logger.info(f"Received callback: {callback.data}")
    try:
        if f"seed_saved_{telegram_id}" in callback.data:
            logger.info(f"Confirmed seed saved for user {telegram_id}")
            await callback.message.delete()

            # Fetch the bot username dynamically
            bot_info = await callback.message.bot.get_me()
            bot_username = bot_info.username
            founder_link = f"https://t.me/{bot_username}?start=pioneer-signup"

            # Escape special characters for MarkdownV2
            def escape_markdown_v2(text: str) -> str:
                reserved_chars = r"_*[]()~`>#+-=|{}.!"
                for char in reserved_chars:
                    text = text.replace(char, f"\\{char}")
                return text

            escaped_link = escape_markdown_v2(founder_link)
            message_text = (
                f"Great\\! The Message with your secret seed has been deleted, and your wallet is ready\\.\n\n"
                f"To complete your registration, please click below to confirm your pioneer status:\n"
                f"[{escaped_link}]({escaped_link})\n\n"
                f"Note: Pioneer slots are limited to 25 users\\. If the limit is reached, you’ll be notified after clicking the link\\."
            )
            try:
                await callback.message.answer(message_text, parse_mode="MarkdownV2")
            except Exception as send_error:
                logger.error(f"Failed to send pioneer link message with MarkdownV2: {str(send_error)}")
                # Fallback: Send the message without Markdown parsing
                await callback.message.answer(
                    f"Great! The Message with your secret seed has been deleted, and your wallet is ready.\n"
                    f"To complete your registration, please click below to confirm your pioneer status:\n"
                    f"{founder_link}\n\n"
 f"Note: Pioneer slots are limited to 25 users. If the limit is reached, you’ll be notified after clicking the link.",
                    parse_mode=None
                )

            # Schedule the seed reminder
            asyncio.create_task(send_reminder(callback.message.bot, telegram_id, state))
    except Exception as e:
        logger.error(f"Error in confirm_seed_saved: {str(e)}", exc_info=True)
        # Fallback message in case of any other error
        await callback.message.answer(
            f"Your secret seed has been deleted, and your wallet is ready",
            parse_mode="Markdown"
        )
    await callback.answer()

async def send_reminder(bot, telegram_id, state):
    await asyncio.sleep(10)
    try:
        await bot.send_message(telegram_id, "Reminder: Ensure your seed is securely stored offline!")
        logger.info(f"Sent seed reminder to user {telegram_id}")
    except Exception as e:
        logger.error(f"Failed to send reminder to user {telegram_id}: {str(e)}")

async def process_email(message: types.Message, state: FSMContext, app_context):
    email = message.text.strip()
    data = await state.get_data()
    sub_org_id = data['sub_org_id']
    # Send Web App button
    keyboard = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="Set Up Passkey", web_app=WebAppInfo(url=f"https://lumenbro.com/turnkey-auth?orgId={sub_org_id}&email={email}"))]
    ])
    await message.reply("Tap to set up secure passkey:", reply_markup=keyboard)
    await state.clear()  # Continue after in separate callback

async def login_command(message: types.Message, app_context):
    telegram_id = message.from_user.id
    async with app_context.db_pool.acquire() as conn:
        row = await conn.fetchrow(
            "SELECT turnkey_sub_org_id FROM turnkey_wallets WHERE telegram_id = $1 AND is_active = TRUE", 
            telegram_id
        )
        if not row:
            await message.reply("No wallet—register first.")
            return
        sub_org_id = row['turnkey_sub_org_id']
        email = (await conn.fetchval("SELECT user_email FROM users WHERE telegram_id = $1", telegram_id)) or "unknown@lumenbro.com"
    login_url = f"https://lumenbro.com/login?orgId={sub_org_id}&email={email}"
    keyboard = InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="Establish Session", url=login_url)]])
    await message.reply("Open to login/establish session:", reply_markup=keyboard)

async def unregister_command(message: types.Message, app_context, streaming_service: StreamingService):
    telegram_id = message.from_user.id
    logger.info(f"Unregister command: from_user.id={telegram_id}, chat_id={message.chat.id}, is_group={message.chat.type == 'group'}")
    chat_id = message.chat.id
    async with app_context.db_pool.acquire() as conn:
        existing = await conn.fetchval("SELECT telegram_id FROM users WHERE telegram_id = $1", telegram_id)
        if not existing:
            await message.reply("No wallet registered.")
            return
        warning_message = (
            "Warning: Unregistering will delete your wallet details, session data, and associated records. "
            "Since your wallet is non-custodial (controlled via Turnkey passkey), ensure you've noted your Sub-Org ID and Key ID for recovery if you have funds.\n\n"
            "Are you sure you want to proceed?"
        )
        confirm_keyboard = InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="Yes, Unregister", callback_data=f"confirm_unregister_{telegram_id}"),
             InlineKeyboardButton(text="No, Cancel", callback_data=f"cancel_unregister_{telegram_id}")]
        ])
        await message.reply(warning_message, reply_markup=confirm_keyboard)

async def confirm_unregister(callback: types.CallbackQuery, app_context, streaming_service: StreamingService):
    telegram_id = callback.from_user.id
    chat_id = callback.message.chat.id
    logger.info(f"Confirm unregister: telegram_id={telegram_id}, chat_id={chat_id}")
    try:
        if f"confirm_unregister_{telegram_id}" in callback.data:
            logger.info(f"Proceeding with unregister for user {telegram_id}")
            async with app_context.db_pool.acquire() as conn:
                # Clear session fields from users
                await conn.execute(
                    "UPDATE users SET turnkey_session_id = NULL, temp_api_public_key = NULL, temp_api_private_key = NULL, session_expiry = NULL WHERE telegram_id = $1",
                    telegram_id
                )
                await conn.execute("DELETE FROM users WHERE telegram_id = $1", telegram_id)
                result = await conn.fetchval("SELECT telegram_id FROM users WHERE telegram_id = $1", telegram_id)
                if result:
                    logger.error(f"Deletion failed: User {telegram_id} still exists in database")
                else:
                    logger.info(f"User {telegram_id} successfully deleted from database")

                # Delete from turnkey_wallets (sub_org, key_id, etc.)
                await conn.execute("DELETE FROM turnkey_wallets WHERE telegram_id = $1", telegram_id)
                result = await conn.fetchval("SELECT telegram_id FROM turnkey_wallets WHERE telegram_id = $1", telegram_id)
                if result:
                    logger.error(f"Deletion failed: User {telegram_id} still exists in turnkey_wallets table")
                else:
                    logger.info(f"User {telegram_id} successfully deleted from turnkey_wallets table")

                await conn.execute("DELETE FROM trades WHERE user_id = $1", telegram_id)
                result = await conn.fetchval("SELECT user_id FROM trades WHERE user_id = $1", telegram_id)
                if result:
                    logger.error(f"Deletion failed: User {telegram_id} still exists in trades table")
                else:
                    logger.info(f"User {telegram_id} successfully deleted from trades table")

                await conn.execute("DELETE FROM rewards WHERE user_id = $1", telegram_id)
                result = await conn.fetchval("SELECT user_id FROM rewards WHERE user_id = $1", telegram_id)
                if result:
                    logger.error(f"Deletion failed: User {telegram_id} still exists in rewards table")
                else:
                    logger.info(f"User {telegram_id} successfully deleted from rewards table")

                await conn.execute("DELETE FROM copy_trading WHERE user_id = $1", telegram_id)
                result = await conn.fetchval("SELECT user_id FROM copy_trading WHERE user_id = $1", telegram_id)
                if result:
                    logger.error(f"Deletion failed: User {telegram_id} still exists in copy_trading table")
                else:
                    logger.info(f"User {telegram_id} successfully deleted from copy_trading table")

                await conn.execute("DELETE FROM referrals WHERE referee_id = $1 OR referrer_id = $1", telegram_id)
                result = await conn.fetchval("SELECT referee_id FROM referrals WHERE referee_id = $1", telegram_id)
                if result:
                    logger.error(f"Deletion failed: User {telegram_id} still exists in referrals table as referee")
                else:
                    logger.info(f"User {telegram_id} successfully deleted from referrals table as referee")
                result = await conn.fetchval("SELECT referrer_id FROM referrals WHERE referrer_id = $1", telegram_id)
                if result:
                    logger.error(f"Deletion failed: User {telegram_id} still exists in referrals table as referrer")
                else:
                    logger.info(f"User {telegram_id} successfully deleted from referrals table as referrer")

                # Add deletion from the founders table
                await conn.execute("DELETE FROM founders WHERE telegram_id = $1", telegram_id)
                result = await conn.fetchval("SELECT telegram_id FROM founders WHERE telegram_id = $1", telegram_id)
                if result:
                    logger.error(f"Deletion failed: User {telegram_id} still exists in founders table")
                else:
                    logger.info(f"User {telegram_id} successfully deleted from founders table")

            await streaming_service.stop_streaming(chat_id)

            # Call backend to clear any server-side state (e.g., deactivate in Turnkey if applicable)
            import requests
            try:
                response = requests.post('https://lumenbro.com/mini-app/clear', json={'telegram_id': telegram_id})
                if not response.ok:
                    logger.error(f"Backend clear failed: {response.text}")
                else:
                    logger.info(f"Backend clear succeeded for {telegram_id}")
            except Exception as e:
                logger.error(f"Error calling backend clear: {str(e)}")

            # Launch Mini App to clear Telegram Cloud Storage
            mini_app_url = f"https://lumenbro.com/mini-app/index.html?action=unregister"
            keyboard = InlineKeyboardMarkup(inline_keyboard=[
                [InlineKeyboardButton(text="Complete Unregister in Mini App", web_app=WebAppInfo(url=mini_app_url))]
            ])
            await callback.message.edit_text("DB cleared. Open Mini App to clear cloud storage keys.", reply_markup=keyboard)

        elif f"cancel_unregister_{telegram_id}" in callback.data:
            await callback.message.edit_text("Unregistration cancelled. Your wallet remains active.")
    except Exception as e:
        logger.error(f"Error in confirm_unregister: {str(e)}", exc_info=True)
        await callback.message.edit_text(f"Error during unregistration: {str(e)}")
    await callback.answer()

async def process_buy_sell(callback: types.CallbackQuery, state: FSMContext):
    logger.info(f"Processing buy/sell callback: {callback.data}")
    action = callback.data
    await state.update_data(action=action)
    await callback.message.reply(f"Please enter the asset code and issuer for {action} in the format: code:issuer")
    await state.set_state(BuySellStates.waiting_for_asset)
    await callback.answer()

async def process_asset(message: types.Message, state: FSMContext):
    asset_input = message.text.strip()
    try:
        code, issuer = asset_input.split(':')
        if not issuer.startswith('G') or len(issuer) != 56:
            raise ValueError("Issuer must be a valid Stellar public key")
        await state.update_data(asset_code=code, asset_issuer=issuer)
        await message.reply("Enter the amount to buy/sell:")
        await state.set_state(BuySellStates.waiting_for_amount)
    except ValueError as e:
        logger.error(f"Invalid asset format: {str(e)}", exc_info=True)
        await message.reply(f"Invalid format: {str(e)}. Use: code:issuer")

async def process_amount(message: types.Message, state: FSMContext, app_context):
    try:
        data = await state.get_data()
        action = data['action']
        asset_code = data['asset_code']
        asset_issuer = data['asset_issuer']
        amount = float(message.text)
        if amount <= 0:
            raise ValueError("Amount must be positive")

        if action == 'buy':
            response, actual_xlm_spent, actual_amount_received, actual_fee_paid, fee_percentage = await perform_buy(
                message.from_user.id, app_context.db_pool, asset_code, asset_issuer, amount, app_context
            )
            await message.reply(
                f"Buy successful. Bought {actual_amount_received:.7f} {asset_code} for {actual_xlm_spent:.7f} XLM\n"
                f"Fee: {actual_fee_paid:.7f} XLM ({fee_percentage:.2f}%)\n"
                f"Tx Hash: {response['hash']}"
            )
        elif action == 'sell':
            response, actual_xlm_received, actual_amount_sent, actual_fee_paid, fee_percentage = await perform_sell(
                message.from_user.id, app_context.db_pool, asset_code, asset_issuer, amount, app_context
            )
            await message.reply(
                f"Sell successful. Sold {actual_amount_sent:.7f} {asset_code} for {actual_xlm_received:.7f} XLM\n"
                f"Fee: {actual_fee_paid:.7f} XLM ({fee_percentage:.2f}%)\n"
                f"Tx Hash: {response['hash']}"
            )
        else:
            raise ValueError("Invalid action")
    except Exception as e:
        logger.error(f"Error in {action}: {str(e)}", exc_info=True)
        error_msg = str(e) if str(e) else "An unexpected error occurred during the transaction."
        await message.reply(f"Error: {error_msg}")  # Add await here
    finally:
        await state.clear()
        await message.reply(welcome_text, reply_markup=main_menu_keyboard, parse_mode="Markdown")

async def process_balance(message_or_callback: types.Message | types.CallbackQuery, app_context):
    # Handle both Message (from command) and CallbackQuery (from button)
    if isinstance(message_or_callback, types.Message):
        target = message_or_callback
        user_id = target.from_user.id
        is_callback = False
        logger.debug(f"Processing balance command for user {user_id} via message")
    else:
        target = message_or_callback.message
        user_id = message_or_callback.from_user.id
        is_callback = True
        logger.debug(f"Processing balance callback for user {user_id} via button")

    try:
        logger.debug(f"Fetching public key for user {user_id}")
        public_key = await app_context.load_public_key(user_id)
        logger.debug(f"Public key retrieved: {public_key}")

        try:
            logger.debug(f"Loading account for public key {public_key}")
            account = await load_account_async(public_key, app_context)
            logger.debug(f"Account loaded successfully: {account['id']}")

            # Fetch balances, excluding XLM and non-standard assets like liquidity pool shares
            logger.debug("Extracting balances excluding XLM and non-standard assets")
            balance_lines = [
                {"code": b['asset_code'], "issuer": b['asset_issuer'] if b.get('asset_issuer') else 'Unknown', "balance": b['balance']}
                for b in account["balances"]
                if b['asset_type'] in ('credit_alphanum4', 'credit_alphanum12')
            ]
            xlm_balance = float(next((b["balance"] for b in account["balances"] if b["asset_type"] == "native"), "0"))
            logger.debug(f"XLM balance: {xlm_balance}, Number of other assets: {len(balance_lines)}")

            # Calculate XLM usage
            logger.debug("Calculating XLM usage")
            xlm_liabilities = float(next((b["selling_liabilities"] for b in account["balances"] if b["asset_type"] == "native"), "0"))
            subentry_count = account["subentry_count"]
            num_sponsoring = account.get("num_sponsoring", 0)
            num_sponsored = account.get("num_sponsored", 0)
            trustlines = [b for b in account["balances"] if b["asset_type"] in ('credit_alphanum4', 'credit_alphanum12')]
            num_trustlines = len(trustlines)
            base_reserve = 2.0
            subentry_reserve = (subentry_count + num_sponsoring - num_sponsored) * 0.5
            minimum_reserve = base_reserve + subentry_reserve
            available_xlm = max(xlm_balance - xlm_liabilities - minimum_reserve, 0)
            logger.debug(f"Available XLM: {available_xlm}, Minimum reserve: {minimum_reserve}")

            # Identify zero-balance trustlines, cap at 5 for display
            logger.debug("Checking for zero-balance trustlines")
            zero_balance_trustlines = [
                f"{b['asset_code']}:{b['asset_issuer'] if b.get('asset_issuer') else 'Unknown'}"
                for b in trustlines
                if float(b["balance"]) == 0
            ]
            if zero_balance_trustlines:
                display_trustlines = zero_balance_trustlines[:5]
                remaining = len(zero_balance_trustlines) - len(display_trustlines)
                zero_balance_note = (
                    f"\n\n*Note*: You have {len(zero_balance_trustlines)} trustlines with 0 balance, reserving {len(zero_balance_trustlines) * 0.5:.1f} XLM. "
                    f"Remove them to free up XLM:\n- " + "\n- ".join(display_trustlines)
                )
                if remaining > 0:
                    zero_balance_note += f"\n(and {remaining} more)"
                zero_balance_note += f"\nUse /removetrust to remove unused trustlines."
            else:
                zero_balance_note = ""
            logger.debug(f"Zero-balance trustlines: {len(zero_balance_trustlines)}")

            # Build XLM breakdown
            logger.debug("Building XLM breakdown text")
            xlm_breakdown = (
                f"XLM Breakdown:\n"
                f"- Total: {xlm_balance:.7f} XLM\n"
                f"- Available: {available_xlm:.7f} XLM\n"
                f"- Reserved: {minimum_reserve:.7f} XLM\n"
                f"  - Base: {base_reserve:.1f} XLM\n"
                f"  - Trustlines ({num_trustlines}): {num_trustlines * 0.5:.1f} XLM\n"
                f"  - Other Subentries: {(subentry_count - num_trustlines + num_sponsoring - num_sponsored) * 0.5:.1f} XLM"
            )
            if xlm_liabilities > 0:
                xlm_breakdown += f"\n- Liabilities (Offers): {xlm_liabilities:.7f} XLM"
            logger.debug("XLM breakdown constructed")

            # Fetch XLM/USD price
            logger.debug("Fetching XLM/USD price")
            total_value_xlm = xlm_balance  # Start with XLM balance
            total_value_usd = 0.0
            xlm_usd_price = await app_context.price_service.fetch_xlm_usd_price()
            logger.debug(f"XLM/USD price fetched: {xlm_usd_price}")

            # Add XLM balance USD value
            if xlm_usd_price:
                xlm_usd_value = xlm_balance * xlm_usd_price
                total_value_usd += xlm_usd_value
                logger.debug(f"XLM USD value: {xlm_balance} XLM * {xlm_usd_price} USD/XLM = {xlm_usd_value} USD")
            else:
                logger.warning("XLM/USD price unavailable, excluding XLM from USD total")

            # Track assets with zero price and the latest price timestamp
            zero_price_assets = []
            latest_price_timestamp = None

            logger.debug("Fetching asset values for other assets")
            for i, asset in enumerate(balance_lines):
                logger.debug(f"Processing asset {i+1}/{len(balance_lines)}: {asset['code']}:{asset['issuer']}")
                value_in_xlm, value_in_usd = await app_context.price_service.get_asset_value(
                    asset['code'], asset['issuer'], asset['balance']
                )
                logger.debug(f"Asset {asset['code']}: Value in XLM = {value_in_xlm}, Value in USD = {value_in_usd}")
                asset['value_in_xlm'] = value_in_xlm
                asset['value_in_usd'] = value_in_usd
                if value_in_xlm == 0.0:
                    zero_price_assets.append(asset['code'])
                # Update the latest price timestamp from the cache
                cache_key = f"{asset['code']}:{asset['issuer']}"
                if cache_key in app_context.price_service.price_cache:
                    timestamp = app_context.price_service.price_cache[cache_key][1]
                    if latest_price_timestamp is None or timestamp > latest_price_timestamp:
                        latest_price_timestamp = timestamp
                total_value_xlm += value_in_xlm
                total_value_usd += value_in_usd
            logger.debug(f"Total wallet value: {total_value_xlm} XLM, ${total_value_usd} USD")

            # Build content_text with asset values
            logger.debug("Building balance text with asset values")
            balance_text_lines = []
            for asset in balance_lines:
                value_display = f"≈ {asset['value_in_xlm']:.4f} XLM"
                if xlm_usd_price and asset['value_in_usd'] > 0:
                    value_display += f" (${asset['value_in_usd']:.2f})"
                asset_line = (
                    f"`{asset['code']}:{asset['issuer']}`: {asset['balance']} {value_display}"
                )
                balance_text_lines.append(asset_line)
            balance_text = "\n\n".join(balance_text_lines)  # Add extra newline between rows
            logger.debug("Balance text constructed")

            # Add total value
            logger.debug("Adding total wallet value to output")
            total_value_text = f"\n\n**Total Wallet Value**\n≈ {total_value_xlm:.4f} XLM"
            if xlm_usd_price and total_value_usd > 0:
                total_value_text += f" (${total_value_usd:.2f})"

            # Add timestamp and zero-price warning
            additional_notes = ""
            if latest_price_timestamp:
                additional_notes += f"\n\n*Prices updated at {latest_price_timestamp.strftime('%Y-%m-%d %H:%M:%S')} UTC*"
            if zero_price_assets:
                additional_notes += f"\n\n*Warning*: Price unavailable for {', '.join(zero_price_assets)}. Values may be inaccurate."

            # Construct message without header
            logger.debug("Constructing final message")
            max_message_length = 4096
            header = f"Your wallet: `{public_key}`\nYour balances:\n"
            footer = ""
            if available_xlm < 0.1:
                footer += f"\n\nYour available XLM is low ({available_xlm:.7f} XLM). Please fund your account to perform transactions."
            footer += zero_balance_note
            footer += total_value_text
            footer += additional_notes

            # Build content_text without the header
            content_text = f"{xlm_breakdown}\n\nOther Assets:\n{balance_text}" if balance_text else f"{xlm_breakdown}"

            # Use pagination logic
            logger.debug("Applying pagination logic")
            available_length = max_message_length - len(header) - len(footer)
            messages = []
            current_message = header
            lines = content_text.split("\n")

            for line in lines:
                if len(current_message) + len(line) + 1 > max_message_length - len(footer):
                    current_message += footer
                    messages.append(current_message)
                    current_message = header
                current_message += line + "\n"

            if current_message != header:
                current_message += footer
                messages.append(current_message)

            logger.debug(f"Sending {len(messages)} message(s) to user")
            for i, msg in enumerate(messages):
                if len(messages) > 1:
                    msg = f"Page {i+1}/{len(messages)}\n{msg}"
                logger.debug(f"Sending message {i+1}/{len(messages)}")
                await target.reply(msg, parse_mode="Markdown")
                logger.debug(f"Message {i+1}/{len(messages)} sent")

        except NotFoundError:
            logger.debug("Account not found, sending unfunded message")
            await target.reply(
                f"Your wallet: `{public_key}`\n"
                f"Your account isn’t funded yet. To activate it, send XLM to your public key from an exchange or wallet (e.g., Coinbase, Kraken, Lobstr).",
                parse_mode="Markdown"
            )
    except Exception as e:
        logger.error(f"Error fetching balance: {str(e)}", exc_info=True)
        await target.reply(f"Error fetching balance: {str(e)}")
    if is_callback:
        logger.debug("Acknowledging callback query")
        await message_or_callback.answer()

async def process_register_callback(callback: types.CallbackQuery, app_context, state: FSMContext):
    await register_command(callback.message, app_context, state)
    await callback.answer()

async def process_copy_trading_callback(callback: types.CallbackQuery, app_context, streaming_service: StreamingService):
    user_id = callback.from_user.id
    await copy_trade_menu_command(callback.message, streaming_service, user_id=user_id, app_context=app_context)
    await callback.answer()

async def process_withdraw(callback: types.CallbackQuery, state: FSMContext):
    await callback.message.reply("Please specify the asset you want to withdraw (e.g., XLM or USDC:issuer_address).")
    await state.set_state(WithdrawStates.waiting_for_asset)
    await callback.answer()

async def process_withdraw_asset(message: types.Message, state: FSMContext):
    asset_input = message.text.strip()
    if asset_input.lower() == "xlm":
        asset = Asset.native()
    else:
        try:
            code, issuer = asset_input.split(':')
            Keypair.from_public_key(issuer)
            asset = Asset(code, issuer)
        except:
            await message.reply("Invalid asset format. Use 'XLM' or 'code:issuer'")
            return
    await state.update_data(asset=asset)
    await message.reply("Please enter the destination address.")
    await state.set_state(WithdrawStates.waiting_for_address)

async def process_withdraw_address(message: types.Message, state: FSMContext):
    address = message.text.strip()
    try:
        Keypair.from_public_key(address)
    except:
        await message.reply("Invalid Stellar public key.")
        return
    await state.update_data(address=address)
    await message.reply("Please enter the amount to withdraw.")
    await state.set_state(WithdrawStates.waiting_for_amount)

async def process_withdraw_amount(message: types.Message, state: FSMContext):
    try:
        amount = float(message.text)
        if amount <= 0:
            raise ValueError("Amount must be positive")
    except ValueError as e:
        await message.reply(f"Invalid amount: {str(e)}")
        return
    data = await state.get_data()
    asset = data['asset']
    address = data['address']
    await state.update_data(amount=amount)
    asset_str = "XLM" if asset.is_native() else f"{asset.code}:{asset.issuer}"
    confirmation_text = f"Please confirm the withdrawal:\nAsset: {asset_str}\nAmount: {amount}\nDestination: {address}"
    keyboard = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="Confirm", callback_data="confirm_withdraw"),
         InlineKeyboardButton(text="Cancel", callback_data="cancel_withdraw")]
    ])
    await message.reply(confirmation_text, reply_markup=keyboard)
    await state.set_state(WithdrawStates.waiting_for_confirmation)

async def process_withdraw_confirmation(callback: types.CallbackQuery, state: FSMContext, app_context):
    if callback.data == "confirm_withdraw":
        data = await state.get_data()
        asset = data['asset']
        amount = data['amount']
        destination = data['address']
        try:
            from services.trade_services import perform_withdraw
            response = await perform_withdraw(callback.from_user.id, app_context.db_pool, asset, amount, destination, app_context)
            await callback.message.reply(f"Withdrawal successful. Tx Hash: {response['hash']}")
        except Exception as e:
            await callback.message.reply(f"Withdrawal failed: {str(e)}")
    else:
        await callback.message.reply("Withdrawal cancelled.")
    await state.clear()
    await callback.answer()

async def export_rewards_command(message: types.Message, app_context):
    telegram_id = message.from_user.id
    admin_id = os.getenv("ADMIN_TELEGRAM_ID")
    if str(telegram_id) != admin_id:
        await message.reply("You are not authorized to use this command.")
        return

    output_file = f"referral_rewards_export_{datetime.utcnow().strftime('%Y%m%d_%H%M%S')}.csv"
    try:
        exported_file_path, total_payout, payout_list = await export_unpaid_rewards(app_context.db_pool, app_context.db_pool, output_file)
        if exported_file_path:
            await message.reply(f"Referral rewards exported to {exported_file_path}")
        else:
            await message.reply("No unpaid rewards to export.")
    except Exception as e:
        logger.error(f"Error exporting unpaid rewards: {str(e)}", exc_info=True)
        await message.reply("An error occurred while exporting unpaid rewards. Please try again later.")

async def manual_payout_command(message: types.Message, app_context):
    telegram_id = message.from_user.id
    admin_id = os.getenv("ADMIN_TELEGRAM_ID")
    if str(telegram_id) != admin_id:
        await message.reply("You are not authorized to use this command.")
        return
    chat_id = message.chat.id
    await daily_payout(app_context.db_pool, app_context.db_pool, app_context.bot, chat_id, app_context)

async def rankings_command(message: types.Message):
    keyboard = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="View Wallet Rankings", web_app=WebAppInfo(url="https://lumenbro.com/"))]
    ])
    await message.reply("Click below to view wallet rankings:", reply_markup=keyboard)

async def help_faq_command(message: types.Message):
    faq_text = (
        "*Photon Bot Help & FAQ*\n\n"
        "*What is @lumenbrobot?*\n"
        "Your gateway to trading on the Stellar network! Buy, sell, manage assets, follow top traders with copy trading, "
        "and earn rewards by inviting friends.\n\n"
        "*How do I start?*\n"
        "Use /start to check your wallet or begin registration. You’ll get a dedicated wallet for bot trading.\n\n"
        "*How much are fees?*\n"
        "1% of all transactions for direct registration 10% discount if referred, wallet ranking report service is free. (dedicated Horizon and RPC servers are being used in both bot and walletrank)\n\n"
        "*What can I do?*\n"
        "- *Buy/Sell*: Trade assets like USDC, SHX, ETH (use buttons after /start).\n"
        "- *Check Balance*: View your XLM and asset balances, includes reserve calculation and net available XLM.\n"
        "- *Copy Trading*: Streams transactions from any G-address wallet with Horizon AIOHTTP and copies the trade. Multiplier, fixed-amount and slippage settings supported per copied wallet.\n"
        "- *Withdraw*: Send XLM or assets to another Stellar address.\n"
        "- *Referrals*: Invite friends with your referral code to earn rewards.\n"
        "- *Trustlines*: Add (/addtrust) or remove (/removetrust) assets to trade.\n"
        "- *Help*: Use /help for this guide.\n\n"
        "*How do I fund my wallet?*\n"
        "Send XLM to your wallet’s public key from an exchange (e.g., Coinbase, Kraken, Lobstr). "
        "Fund only what you plan to trade to keep your main wallets safe.\n\n"
        "*Do i manually have to add trustlines for copy-trading or buy/sell?*\n"
        "No, the bot will automatically add trustlines for you when you perform a buy/sell or copy-trade.\n\n"
        "*How do I recover my wallet?*\n"
        "During registration, you receive a 24-word mnemonic. Store it offline (e.g., paper, USB). "
        "To recover, import it into a Stellar wallet like Xbull or Lobstr.\n\n"
        "*Is my wallet secure?*\n"
        "Your wallet is generated in a secure, isolated environment with industry-standard encryption. "
        "Your funds are safe as long as you keep your mnemonic private and delete the registration message after saving it.\n\n"
        "*Tips*:\n"
        "- Never share your mnemonic.\n"
        "- Use /removetrust to free up XLM from unused trustlines.\n"
        "- Check /help anytime for guidance.\n\n"
        "- For better wallet managment import mnemonic into Xbull or Lobstr and use the bot as a trading tool.\n\n"
        "*What Soroban functions are supported?*:\n"
        "So far can copy trades from AQUA and Soroswap Routers, has a fallback to SDEX if Soroban copytrade fails. "
        "More functions will be added in the future, for now only issued assets with SAC contracts and copy trading only, no direct buy/sell.\n\n"
        "*Need more help?*\n"
        "Message @lumenbrobot support in Telegram."
    )
    await message.reply(faq_text, parse_mode="Markdown")

async def help_faq_callback(callback: types.CallbackQuery):
    faq_text = (
        "*Photon Bot Help & FAQ*\n\n"
        "*What is @lumenbrobot?*\n"
        "Your gateway to trading on the Stellar network! Buy, sell, manage assets, follow top traders with copy trading, "
        "and earn rewards by inviting friends.\n\n"
        "*How do I start?*\n"
        "Use /start to check your wallet or begin registration. You’ll get a dedicated wallet for bot trading.\n\n"
        "*How much are fees?*\n"
        "1% of all transactions for direct registration 10% discount if referred, wallet ranking report service is free. (dedicated Horizon and RPC servers are being used for both bot and walletrank)\n\n"
        "*What can I do?*\n"
        "- *Buy/Sell*: Trade assets like USDC, SHX, ETH (use buttons after /start).\n"
        "- *Check Balance*: View your XLM and asset balances, includes reserve calculation and net available XLM.\n"
        "- *Copy Trading*: Streams transactions from any G-address wallet with Horizon AIOHTTP and copies the trade. Multiplier, fixed-amount and slippage settings supported per copied wallet.\n"
        "- *Withdraw*: Send XLM or assets to another Stellar address.\n"
        "- *Referrals*: Invite friends with your referral code to earn rewards.\n"
        "- *Trustlines*: Add (/addtrust) or remove (/removetrust) assets to trade.\n"
        "- *Help*: Use /help for/living this guide.\n\n"
        "*How do I fund my wallet?*\n"
        "Send XLM to your wallet’s public key from an exchange (e.g., Coinbase, Kraken, Lobstr). "
        "Fund only what you plan to trade to keep your main wallets safe.\n\n"
        "*Do i manually have to add trustlines for copy-trading or buy/sell?*\n"
        "No, the bot will automatically add trustlines for you when you perform a buy/sell or copy-trade.\n\n"
        "*How do I recover my wallet?*\n"
        "During registration, you receive a 24-word mnemonic. Store it offline (e.g., paper, USB). "
        "To recover, import it into a Stellar wallet like Xbull or Lobstr.\n\n"
        "*Is my wallet secure?*\n"
        "Your wallet is generated in a secure, isolated environment with industry-standard encryption. "
        "Your funds are safe as long as you keep your mnemonic private and delete the registration message after saving it.\n\n"
        "*Tips*:\n"
        "- Never share your mnemonic.\n"
        "- Use /removetrust to free up XLM from unused trustlines.\n"
        "- Check /help anytime for guidance.\n\n"
        "- For better wallet managment import mnemonic into Xbull or Lobstr and use the bot as a trading tool.\n\n"
        "*What Soroban functions are supported?*:\n"
        "So far can copy trades from AQUA and Soroswap Routers, has a fallback to SDEX if Soroban copytrade fails. "
        "More functions will be added in the future, for now only issued assets with SAC contracts and copy trading only, no direct buy/sell.\n\n"
        "*Need more help?*\n"
        "Message @lumenbrobot support in Telegram."
    )
    await callback.message.reply(faq_text, parse_mode="Markdown")
    await callback.answer()

async def process_add_trustline(callback: types.CallbackQuery, state: FSMContext):
    await callback.message.reply("Please enter the asset to add trustline for in the format: code:issuer")
    await state.set_state(TrustlineStates.waiting_for_asset_to_add)
    await callback.answer()

async def process_remove_trustline(callback: types.CallbackQuery, state: FSMContext):
    await callback.message.reply("Please enter the asset to remove trustline for in the format: code:issuer")
    await state.set_state(TrustlineStates.waiting_for_asset_to_remove)
    await callback.answer()

async def add_trust_command(message: types.Message, state: FSMContext):
    await message.reply("Please enter the asset to add trustline for in the format: code:issuer")
    await state.set_state(TrustlineStates.waiting_for_asset_to_add)

async def remove_trust_command(message: types.Message, state: FSMContext):
    await message.reply("Please enter the asset to remove trustline for in the format: code:issuer")
    await state.set_state(TrustlineStates.waiting_for_asset_to_remove)


async def process_add_trustline_asset(message: types.Message, state: FSMContext, app_context):
    asset_input = message.text.strip()
    try:
        code, issuer = asset_input.split(':')
        if not issuer.startswith('G') or len(issuer) != 56:
            raise ValueError("Issuer must be a valid Stellar public key")

        from services.trade_services import perform_add_trustline
        response = await perform_add_trustline(message.from_user.id, app_context.db_pool, code, issuer, app_context)
        await message.reply(f"Trustline added successfully for {code}:{issuer}. Tx Hash: {response['hash']}")
    except Exception as e:
        logger.error(f"Error adding trustline: {str(e)}", exc_info=True)
        await message.reply(f"Error adding trustline: {str(e)}")
    finally:
        await state.clear()
        await message.reply(welcome_text, reply_markup=main_menu_keyboard, parse_mode="Markdown")

async def process_remove_trustline_asset(message: types.Message, state: FSMContext, app_context):
    asset_input = message.text.strip()
    try:
        code, issuer = asset_input.split(':')
        if not issuer.startswith('G') or len(issuer) != 56:
            raise ValueError("Issuer must be a valid Stellar public key")

        from services.trade_services import perform_remove_trustline
        response = await perform_remove_trustline(message.from_user.id, app_context.db_pool, code, issuer, app_context)
        await message.reply(f"Trustline removed successfully for {code}:{issuer}. Tx Hash: {response['hash']}")
    except Exception as e:
        logger.error(f"Error removing trustline: {str(e)}", exc_info=True)
        await message.reply(f"Error removing trustline: {str(e)}")
    finally:
        await state.clear()
        await message.reply(welcome_text, reply_markup=main_menu_keyboard, parse_mode="Markdown")

async def process_wallet_management(app_context, callback: types.CallbackQuery):
    await process_wallet_management_callback(callback, app_context)       

def register_main_handlers(dp, app_context, streaming_service):
    async def start_handler(message: types.Message, state: FSMContext):
        await start_command(message, app_context, streaming_service, state)
    dp.message.register(start_handler, Command("start"))

    dp.message.register(cancel_command, Command("cancel"))

    async def register_handler(message: types.Message, state: FSMContext):
        await register_command(message, app_context, state)
    dp.message.register(register_handler, Command("register"))

    dp.callback_query.register(process_buy_sell, lambda c: c.data in ["buy", "sell"])
    dp.message.register(process_asset, BuySellStates.waiting_for_asset)

    async def amount_handler(message: types.Message, state: FSMContext):
        await process_amount(message, state, app_context)
    dp.message.register(amount_handler, BuySellStates.waiting_for_amount)

    async def balance_callback_handler(callback: types.CallbackQuery):
        await process_balance(callback, app_context)
    dp.callback_query.register(balance_callback_handler, lambda c: c.data == "balance")

    async def balance_command_handler(message: types.Message):
        await process_balance(message, app_context)
    dp.message.register(balance_command_handler, Command("balance"))
    dp.message.register(balance_command_handler, Command("checkbalance"))

    async def register_callback_handler(callback: types.CallbackQuery, state: FSMContext):
        await process_register_callback(callback, app_context, state)
    dp.callback_query.register(register_callback_handler, lambda c: c.data == "register")

    async def copy_trading_handler(callback: types.CallbackQuery):
        await process_copy_trading_callback(callback, app_context, streaming_service)
    dp.callback_query.register(copy_trading_handler, lambda c: c.data == "copy_trading")

    async def unregister_handler(message: types.Message):
        await unregister_command(message, app_context, streaming_service)
    dp.message.register(unregister_handler, Command("unregister"))

    dp.callback_query.register(process_withdraw, lambda c: c.data == "withdraw")
    dp.message.register(process_withdraw_asset, WithdrawStates.waiting_for_asset)
    dp.message.register(process_withdraw_address, WithdrawStates.waiting_for_address)
    dp.message.register(process_withdraw_amount, WithdrawStates.waiting_for_amount)
    async def withdraw_confirmation_handler(callback: types.CallbackQuery, state: FSMContext):
        await process_withdraw_confirmation(callback, state, app_context)
    dp.callback_query.register(withdraw_confirmation_handler, WithdrawStates.waiting_for_confirmation)

    async def seed_saved_wrapper(callback: types.CallbackQuery, state: FSMContext):
        return await confirm_seed_saved(callback, app_context, state)
    dp.callback_query.register(
        seed_saved_wrapper,
        lambda c: c.data.startswith("seed_saved_")
    )

    async def unregister_wrapper(callback: types.CallbackQuery):
        return await confirm_unregister(callback, app_context, streaming_service)
    dp.callback_query.register(
        unregister_wrapper,
        lambda c: c.data.startswith(("confirm_unregister_", "cancel_unregister_"))
    )

    async def export_handler(message: types.Message):
        await export_rewards_command(message, app_context)
    dp.message.register(export_handler, Command("export_rewards"))

    async def referral_code_handler(message: types.Message, state: FSMContext):
        await process_referral_code(message, state, app_context)
    dp.message.register(referral_code_handler, ReferralStates.referral_code)

    async def manual_payout_handler(message: types.Message):
        await manual_payout_command(message, app_context)
    dp.message.register(manual_payout_handler, Command("manual_payout"))

    dp.message.register(help_faq_command, Command("help"))
    dp.callback_query.register(help_faq_callback, lambda c: c.data == "help_faq")

    dp.callback_query.register(process_add_trustline, lambda c: c.data == "add_trustline")
    dp.callback_query.register(process_remove_trustline, lambda c: c.data == "remove_trustline")

    dp.message.register(add_trust_command, Command("addtrust"))
    dp.message.register(remove_trust_command, Command("removetrust"))

    async def add_trustline_asset_handler(message: types.Message, state: FSMContext):
        await process_add_trustline_asset(message, state, app_context)
    dp.message.register(add_trustline_asset_handler, TrustlineStates.waiting_for_asset_to_add)

    async def remove_trustline_asset_handler(message: types.Message, state: FSMContext):
        await process_remove_trustline_asset(message, state, app_context)
    dp.message.register(remove_trustline_asset_handler, TrustlineStates.waiting_for_asset_to_remove)

    dp.message.register(rankings_command, Command("rankings"))

    # Process email handler
    async def process_email_handler(message: types.Message, state: FSMContext):
        await process_email(message, state, app_context)
    dp.message.register(process_email_handler, RegisterStates.waiting_for_email)

    dp.callback_query.register(partial(process_wallet_management, app_context), lambda c: c.data == "wallet_management")
    dp.callback_query.register(process_main_menu_callback, lambda c: c.data == "main_menu")  # No partial needed if no extra args

    async def login_handler(message: types.Message):
        await login_command(message, app_context)
    dp.message.register(login_handler, Command("login"))
