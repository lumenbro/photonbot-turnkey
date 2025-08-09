-- Targeted fix for user 5014800072 with stale organization ID
-- This user has both legacy session data (bad org ID) and new Turnkey wallet (good org ID)

-- BEFORE: Show current state
SELECT 'BEFORE FIX - User 5014800072 state' as status;

SELECT 'Users table data' as source,
       telegram_id,
       turnkey_session_id as org_id,
       session_expiry,
       kms_encrypted_session_key IS NOT NULL as has_kms_session,
       temp_api_public_key IS NOT NULL as has_temp_keys
FROM users 
WHERE telegram_id = 5014800072;

SELECT 'Turnkey wallets data' as source,
       telegram_id,
       turnkey_sub_org_id as org_id,
       is_active,
       public_key
FROM turnkey_wallets 
WHERE telegram_id = 5014800072;

-- THE FIX: Clear stale session data from users table
-- This will force the user to use the new Turnkey wallet path
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

-- AFTER: Verify the fix
SELECT 'AFTER FIX - User 5014800072 state' as status;

SELECT 'Users table data (should have no session)' as source,
       telegram_id,
       turnkey_session_id as org_id,
       session_expiry,
       kms_encrypted_session_key IS NOT NULL as has_kms_session,
       temp_api_public_key IS NOT NULL as has_temp_keys,
       source_old_db IS NOT NULL as is_migrated_user
FROM users 
WHERE telegram_id = 5014800072;

SELECT 'Turnkey wallets data (should be active)' as source,
       telegram_id,
       turnkey_sub_org_id as org_id,
       is_active,
       public_key
FROM turnkey_wallets 
WHERE telegram_id = 5014800072;

-- VERIFICATION: Check that problematic org ID is gone
SELECT 'Verification - Should return 0 rows' as status,
       count(*) as remaining_bad_org_records
FROM users 
WHERE turnkey_session_id = 'ca28fe57-85c2-4649-9499-bd56404f473d';
