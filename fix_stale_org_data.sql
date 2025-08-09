-- Fix Script for Stale Turnkey Organization Data
-- Run this AFTER identifying the problematic records with investigate_org_id_issue.sql

-- OPTION 1: Clean up the specific problematic organization ID
-- (Run this if you find records with ca28fe57-85c2-4649-9499-bd56404f473d)

-- Clear stale session data from users table for the problematic org ID
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
WHERE turnkey_session_id = 'ca28fe57-85c2-4649-9499-bd56404f473d';

-- Delete stale records from turnkey_wallets for the problematic org ID
DELETE FROM turnkey_wallets 
WHERE turnkey_sub_org_id = 'ca28fe57-85c2-4649-9499-bd56404f473d';

-- OPTION 2: Clean up ALL expired sessions
-- (More aggressive cleanup - run this if you want to clear all stale data)

-- Clear expired session data from users table
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
WHERE session_expiry IS NOT NULL 
  AND session_expiry < NOW()
  AND (turnkey_session_id IS NOT NULL OR kms_encrypted_session_key IS NOT NULL);

-- OPTION 3: Clean up specific user (replace XXXXXX with actual telegram_id)
-- (Use this if you know which specific user is causing the issue)

-- UPDATE users SET 
--     turnkey_session_id = NULL,
--     temp_api_public_key = NULL,
--     temp_api_private_key = NULL,
--     kms_encrypted_session_key = NULL,
--     kms_key_id = NULL,
--     session_expiry = NULL,
--     session_created_at = NULL,
--     turnkey_user_id = NULL,
--     user_email = NULL
-- WHERE telegram_id = XXXXXX;

-- DELETE FROM turnkey_wallets WHERE telegram_id = XXXXXX;

-- VERIFICATION QUERIES (run after cleanup)
-- Verify the problematic org ID is gone
SELECT 'Verification: Should return 0 rows' as status,
       count(*) as remaining_records
FROM (
    SELECT telegram_id FROM users WHERE turnkey_session_id = 'ca28fe57-85c2-4649-9499-bd56404f473d'
    UNION ALL
    SELECT telegram_id FROM turnkey_wallets WHERE turnkey_sub_org_id = 'ca28fe57-85c2-4649-9499-bd56404f473d'
) as combined;

-- Show remaining active sessions
SELECT 'Remaining active users' as info,
       telegram_id,
       COALESCE(turnkey_session_id, 'No session') as org_id,
       session_expiry > NOW() as session_valid,
       kms_encrypted_session_key IS NOT NULL as has_kms_session
FROM users 
WHERE turnkey_session_id IS NOT NULL OR kms_encrypted_session_key IS NOT NULL;
