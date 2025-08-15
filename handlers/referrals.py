from aiogram import types
from aiogram.types import InlineKeyboardMarkup, InlineKeyboardButton
from aiogram.fsm.state import State, StatesGroup
from aiogram.fsm.context import FSMContext
import logging
import asyncio
import re

logger = logging.getLogger(__name__)

class ReferralCodeStates(StatesGroup):
    waiting_for_custom_code = State()

async def referrals_menu(callback: types.CallbackQuery, app_context, state: FSMContext = None):
    try:
        action = callback.data
        telegram_id = callback.from_user.id
        
        if action == "back_to_main":
            # Delete the current message to keep the chat clean
            await callback.message.delete()
            # Simulate a /start command to return to the main menu
            await callback.message.answer("/start")
            await callback.answer()
            return
        
        if action == "set_custom_referral_code":
            await handle_set_custom_referral_code(callback, app_context, state)
            return
        
        logger.info(f"Referrals menu triggered for user {telegram_id}")
        
        # Get referral code and relationships from the Copy Trading database
        async with app_context.db_pool.acquire() as conn:
            referral_code = await conn.fetchval(
                "SELECT referral_code FROM users WHERE telegram_id = $1", telegram_id
            )
            logger.debug(f"Referral code for user {telegram_id}: {referral_code}")
            
            direct_referrals = await conn.fetchval(
                "SELECT COUNT(*) FROM referrals WHERE referrer_id = $1", telegram_id
            ) or 0
            logger.debug(f"Direct referrals for user {telegram_id}: {direct_referrals}")
            
            total_referrals = await conn.fetchval(""" 
                WITH RECURSIVE referral_tree AS (
                    SELECT referee_id FROM referrals WHERE referrer_id = $1
                    UNION ALL
                    SELECT r.referee_id FROM referrals r
                    JOIN referral_tree rt ON r.referrer_id = rt.referee_id
                )
                SELECT COUNT(*) FROM referral_tree
            """, telegram_id) or 0
            logger.debug(f"Total referrals for user {telegram_id}: {total_referrals}")
        
        # Get rewards from the Copy Trading database
        async with app_context.db_pool.acquire() as conn:
            total_rewards = await conn.fetchval(
                "SELECT SUM(amount) FROM rewards WHERE user_id = $1", telegram_id
            ) or 0
            logger.debug(f"Total rewards for user {telegram_id}: {total_rewards}")
            
            paid_rewards = await conn.fetchval(
                "SELECT SUM(amount) FROM rewards WHERE user_id = $1 AND status = 'paid'", telegram_id
            ) or 0
            logger.debug(f"Paid rewards for user {telegram_id}: {paid_rewards}")
            
            unpaid_rewards = await conn.fetchval(
                "SELECT SUM(amount) FROM rewards WHERE user_id = $1 AND status = 'unpaid'", telegram_id
            ) or 0
            logger.debug(f"Unpaid rewards for user {telegram_id}: {unpaid_rewards}")
        
        # Fetch the bot's username using get_me()
        bot_info = await app_context.bot.get_me()
        bot_username = bot_info.username
        # Use the referral_code directly without adding another "ref-" prefix
        referral_link = f"https://t.me/{bot_username}?start={referral_code or 'None'}"
        
        # Check if user can set custom referral code
        can_set_custom = total_referrals == 0
        
        # Construct the explainer and menu
        response = (
            "💰 Invite your friends to save 10% on trading fees! You'll earn a 25% share of the fees paid by your referees, across multiple tiers of referrals.\n\n"
            "Your Referrals (updated every 30 min)\n"
            f"• Users referred: {total_referrals} (direct: {direct_referrals}, indirect: {total_referrals - direct_referrals})\n"
            f"• Total rewards: {total_rewards:.7f} XLM\n"
            f"• Total paid: {paid_rewards:.7f} XLM\n"
            f"• Total unpaid: {unpaid_rewards:.7f} XLM\n\n"
            "Rewards are paid daily and airdropped directly to your wallet. You must have accrued at least 0.1 XLM in unpaid fees to be eligible for a payout.\n\n"
            "Our tiered referral system rewards you for direct and indirect referrals, encouraging community growth and increasing your share of fees as more users join.\n\n"
            "Stay tuned for more updates and happy trading!\n\n"
            f"Your Referral Link: {referral_link}"
        )
        
        # Create keyboard with conditional custom referral code button
        keyboard_buttons = []
        if can_set_custom:
            keyboard_buttons.append([InlineKeyboardButton(text="✏️ Set Custom Referral Code", callback_data="set_custom_referral_code")])
        keyboard_buttons.append([InlineKeyboardButton(text="Back", callback_data="back_to_main")])
        
        keyboard = InlineKeyboardMarkup(inline_keyboard=keyboard_buttons)
        await asyncio.sleep(0.1)  # Add a small delay to avoid rate limits
        await callback.message.edit_text(response, reply_markup=keyboard)
        await callback.answer()
    except Exception as e:
        logger.error(f"Error in referrals_menu for user {telegram_id}: {str(e)}", exc_info=True)
        await callback.message.edit_text("An error occurred while fetching your referral data. Please try again later.")
        await callback.answer()

async def handle_set_custom_referral_code(callback: types.CallbackQuery, app_context, state: FSMContext):
    """Handle the set custom referral code button"""
    telegram_id = callback.from_user.id
    
    try:
        # Double-check that user has no referrals
        async with app_context.db_pool.acquire() as conn:
            total_referrals = await conn.fetchval(""" 
                WITH RECURSIVE referral_tree AS (
                    SELECT referee_id FROM referrals WHERE referrer_id = $1
                    UNION ALL
                    SELECT r.referee_id FROM referrals r
                    JOIN referral_tree rt ON r.referrer_id = rt.referee_id
                )
                SELECT COUNT(*) FROM referral_tree
            """, telegram_id) or 0
        
        if total_referrals > 0:
            await callback.message.edit_text(
                "❌ **Cannot Set Custom Referral Code**\n\n"
                "You already have referrals in your network. Changing your referral code would break the existing referral chain.\n\n"
                "Your current referral code must remain the same to maintain the integrity of your referral network.",
                reply_markup=InlineKeyboardMarkup(inline_keyboard=[
                    [InlineKeyboardButton(text="🔙 Back to Referrals", callback_data="referrals")]
                ])
            )
            await callback.answer()
            return
        
        # Get current referral code
        async with app_context.db_pool.acquire() as conn:
            current_code = await conn.fetchval(
                "SELECT referral_code FROM users WHERE telegram_id = $1", telegram_id
            )
        
        # Show instructions for setting custom code
        instructions = (
            "✏️ **Set Custom Referral Code**\n\n"
            f"**Current Code:** {current_code}\n\n"
            "**Rules for Custom Codes:**\n"
            "• Must start with 'ref-'\n"
            "• Must be 4-23 characters long (including 'ref-')\n"
            "• After 'ref-', must start with a letter\n"
            "• Can contain letters, numbers, and hyphens\n"
            "• Cannot contain spaces or special characters\n"
            "• Cannot end with a hyphen\n"
            "• Must be unique (not used by another user)\n\n"
            "**Examples:**\n"
            "✅ ref-johndoe\n"
            "✅ ref-crypto-trader\n"
            "✅ ref-alice123\n"
            "❌ ref- (too short)\n"
            "❌ ref-123 (starts with number after ref-)\n"
            "❌ ref-john- (ends with hyphen)\n"
            "❌ ref john (contains space)\n\n"
            "**⚠️ Important:**\n"
            "• This can only be done once\n"
            "• You cannot change it after setting\n"
            "• Make sure it's easy to remember and share\n\n"
            "Enter your custom referral code (or type 'cancel' to go back):"
        )
        
        # Set state to wait for custom code
        await state.set_state(ReferralCodeStates.waiting_for_custom_code)
        
        await callback.message.edit_text(
            instructions,
            reply_markup=InlineKeyboardMarkup(inline_keyboard=[
                [InlineKeyboardButton(text="❌ Cancel", callback_data="referrals")]
            ]),
            parse_mode="Markdown"
        )
        await callback.answer()
        
    except Exception as e:
        logger.error(f"Error in handle_set_custom_referral_code for user {telegram_id}: {str(e)}", exc_info=True)
        await callback.message.edit_text("An error occurred. Please try again later.")
        await callback.answer()

async def process_custom_referral_code(message: types.Message, state: FSMContext, app_context):
    """Process the custom referral code input"""
    telegram_id = message.from_user.id
    custom_code = message.text.strip()
    
    try:
        # Check if user wants to cancel
        if custom_code.lower() in ['cancel', 'back', 'no']:
            await state.clear()
            await message.reply("❌ Custom referral code setup cancelled.", parse_mode=None)
            return
        
        # Validate the custom code format - must start with 'ref-'
        if not custom_code.startswith('ref-') or not re.match(r'^ref-[a-zA-Z][a-zA-Z0-9-]*[a-zA-Z0-9]$', custom_code) or len(custom_code) < 4 or len(custom_code) > 23:
            await message.reply(
                "❌ Invalid Referral Code Format\n\n"
                "Your code must:\n"
                "• Start with 'ref-'\n"
                "• Be 4-23 characters long (including 'ref-')\n"
                "• After 'ref-', start with a letter\n"
                "• Contain only letters, numbers, and hyphens\n"
                "• Not contain spaces or special characters\n"
                "• Cannot end with a hyphen\n\n"
                "Examples: ref-johndoe, ref-crypto-trader, ref-alice123\n\n"
                "Please try again or type 'cancel' to go back:",
                parse_mode=None
            )
            return
        
        # Check if code is already taken
        async with app_context.db_pool.acquire() as conn:
            existing_user = await conn.fetchval(
                "SELECT telegram_id FROM users WHERE LOWER(referral_code) = LOWER($1)",
                custom_code
            )
            
            if existing_user and existing_user != telegram_id:
                await message.reply(
                    f"❌ Referral Code Already Taken\n\n"
                    f"The code {custom_code} is already in use by another user.\n\n"
                    f"Please choose a different code or type 'cancel' to go back:",
                    parse_mode=None
                )
                return
            
            # Double-check that user still has no referrals
            total_referrals = await conn.fetchval(""" 
                WITH RECURSIVE referral_tree AS (
                    SELECT referee_id FROM referrals WHERE referrer_id = $1
                    UNION ALL
                    SELECT r.referee_id FROM referrals r
                    JOIN referral_tree rt ON r.referrer_id = rt.referee_id
                )
                SELECT COUNT(*) FROM referral_tree
            """, telegram_id) or 0
            
            if total_referrals > 0:
                await message.reply(
                    "❌ Cannot Set Custom Referral Code\n\n"
                    "You now have referrals in your network. Changing your referral code would break the existing referral chain.\n\n"
                    "Your current referral code must remain the same to maintain the integrity of your referral network.",
                    parse_mode=None
                )
                await state.clear()
                return
            
            # Update the referral code
            await conn.execute(
                "UPDATE users SET referral_code = $1 WHERE telegram_id = $2",
                custom_code, telegram_id
            )
            
            # Get bot username for new link
            bot_info = await app_context.bot.get_me()
            bot_username = bot_info.username
            new_referral_link = f"https://t.me/{bot_username}?start={custom_code}"
            
            success_message = (
                f"✅ **Custom Referral Code Set Successfully!**\n\n"
                f"**Your New Code:** {custom_code}\n"
                f"**Your New Link:** {new_referral_link}\n\n"
                f"**What Changed:**\n"
                f"• Your referral link is now easier to share\n"
                f"• New users can use your custom code\n"
                f"• All existing functionality remains the same\n\n"
                f"**Next Steps:**\n"
                f"• Share your new referral link with friends\n"
                f"• Start earning referral rewards\n"
                f"• Your code cannot be changed again\n\n"
                f"🎉 Happy referring!"
            )
            
            await message.reply(success_message, parse_mode=None)
            await state.clear()
            
            logger.info(f"User {telegram_id} successfully set custom referral code: {custom_code}")
            
    except Exception as e:
        logger.error(f"Error processing custom referral code for user {telegram_id}: {str(e)}", exc_info=True)
        await message.reply("❌ An error occurred while setting your custom referral code. Please try again later.", parse_mode=None)
        await state.clear()

def register_referral_handlers(dp, app_context):
    logger.info("Registering referral handlers")
    
    async def referral_handler(callback: types.CallbackQuery, state: FSMContext):
        await referrals_menu(callback, app_context, state)
    
    async def custom_code_handler(message: types.Message, state: FSMContext):
        await process_custom_referral_code(message, state, app_context)
    
    # Register callback handlers
    dp.callback_query.register(
        referral_handler,
        lambda c: c.data in ["referrals", "set_custom_referral_code", "back_to_main", "wallets"]
    )
    
    # Register message handler for custom referral code input
    dp.message.register(
        custom_code_handler,
        ReferralCodeStates.waiting_for_custom_code
    )
    
    logger.info("Referral handlers registered successfully")
