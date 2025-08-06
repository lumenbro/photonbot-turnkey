from aiogram import types
from aiogram.types import InlineKeyboardMarkup, InlineKeyboardButton
import logging
import asyncio

logger = logging.getLogger(__name__)

async def referrals_menu(callback: types.CallbackQuery, app_context):
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
        keyboard = InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="Back", callback_data="back_to_main")]
        ])
        await asyncio.sleep(0.1)  # Add a small delay to avoid rate limits
        await callback.message.edit_text(response, reply_markup=keyboard)
        await callback.answer()
    except Exception as e:
        logger.error(f"Error in referrals_menu for user {telegram_id}: {str(e)}", exc_info=True)
        await callback.message.edit_text("An error occurred while fetching your referral data. Please try again later.")
        await callback.answer()

def register_referral_handlers(dp, app_context):
    logger.info("Registering referral handlers")
    async def referral_handler(callback: types.CallbackQuery):
        await referrals_menu(callback, app_context)
    dp.callback_query.register(
        referral_handler,
        lambda c: c.data in ["wallets", "back_to_main"]
    )
    logger.info("Referral handlers registered successfully")
