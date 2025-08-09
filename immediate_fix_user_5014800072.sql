-- Immediate fix for user 5014800072 stale organization ID issue
-- This mimics what the improved logout function would do

-- Clear stale session data that's causing the invalid org ID error
UPDATE users SET 
    turnkey_session_id = NULL,
    temp_api_public_key = NULL,
    temp_api_private_key = NULL,
    kms_encrypted_session_key = NULL,
    kms_key_id = NULL,
    session_expiry = NULL,
    session_created_at = NULL,
    turnkey_user_id = NULL,
    user_email = NULL
WHERE telegram_id = 5014800072;

-- Verify the fix
SELECT 'AFTER FIX: User 5014800072 session data' as status,
       telegram_id,
       turnkey_session_id,
       kms_encrypted_session_key IS NOT NULL as has_kms_session,
       temp_api_public_key IS NOT NULL as has_temp_keys,
       source_old_db IS NOT NULL as is_legacy_user
FROM users 
WHERE telegram_id = 5014800072;

SELECT 'AFTER FIX: User 5014800072 Turnkey wallet' as status,
       telegram_id,
       turnkey_sub_org_id,
       is_active,
       public_key
FROM turnkey_wallets 
WHERE telegram_id = 5014800072;

-- Verify no more invalid org IDs exist
SELECT 'Verification: Invalid org ID should be gone' as status,
       count(*) as should_be_zero
FROM users 
WHERE turnkey_session_id = 'ca28fe57-85c2-4649-9499-bd56404f473d';
