-- SQL Investigation Script for Turnkey Organization ID Issue
-- Problem: organization ID "ca28fe57-85c2-4649-9499-bd56404f473d" not found by Turnkey
-- This script helps identify where stale organization data might be coming from

-- 1. Check for the problematic organization ID in both tables
SELECT 'USERS TABLE - turnkey_session_id' as source, 
       telegram_id, 
       turnkey_session_id as org_id,
       public_key,
       source_old_db,
       session_expiry,
       kms_encrypted_session_key IS NOT NULL as has_kms_session
FROM users 
WHERE turnkey_session_id = 'ca28fe57-85c2-4649-9499-bd56404f473d'

UNION ALL

SELECT 'TURNKEY_WALLETS TABLE - turnkey_sub_org_id' as source,
       telegram_id::text,
       turnkey_sub_org_id as org_id,
       public_key,
       'N/A' as source_old_db,
       'N/A' as session_expiry,
       is_active::text as has_kms_session
FROM turnkey_wallets 
WHERE turnkey_sub_org_id = 'ca28fe57-85c2-4649-9499-bd56404f473d';

-- 2. Show all organization IDs in the database to see what's valid
SELECT 'All Org IDs in users table' as info, 
       count(*) as count,
       array_agg(DISTINCT turnkey_session_id) as org_ids
FROM users 
WHERE turnkey_session_id IS NOT NULL

UNION ALL

SELECT 'All Org IDs in turnkey_wallets table' as info,
       count(*) as count, 
       array_agg(DISTINCT turnkey_sub_org_id) as org_ids
FROM turnkey_wallets;

-- 3. Find recently active users who might be triggering this issue
SELECT 'Recent users with session data' as info,
       telegram_id,
       turnkey_session_id,
       session_expiry,
       session_created_at,
       kms_encrypted_session_key IS NOT NULL as has_kms_session,
       source_old_db IS NOT NULL as is_migrated
FROM users 
WHERE turnkey_session_id IS NOT NULL 
   OR kms_encrypted_session_key IS NOT NULL
ORDER BY session_created_at DESC NULLS LAST;

-- 4. Check turnkey_wallets for recent activity
SELECT 'Recent Turnkey wallets' as info,
       telegram_id,
       turnkey_sub_org_id,
       turnkey_key_id,
       public_key,
       is_active,
       created_at
FROM turnkey_wallets 
ORDER BY created_at DESC;

-- 5. Look for users with stale session data (expired sessions)
SELECT 'Users with expired sessions' as info,
       telegram_id,
       turnkey_session_id,
       session_expiry,
       kms_encrypted_session_key IS NOT NULL as has_kms_session,
       source_old_db IS NOT NULL as is_migrated
FROM users 
WHERE session_expiry IS NOT NULL 
  AND session_expiry < NOW()
  AND (turnkey_session_id IS NOT NULL OR kms_encrypted_session_key IS NOT NULL);

-- 6. Check for users who might have been partially unregistered
SELECT 'Partially cleared users' as info,
       telegram_id,
       public_key IS NOT NULL as has_public_key,
       turnkey_session_id IS NOT NULL as has_session_id,
       temp_api_public_key IS NOT NULL as has_temp_keys,
       kms_encrypted_session_key IS NOT NULL as has_kms_session,
       source_old_db IS NOT NULL as is_migrated
FROM users 
WHERE telegram_id IS NOT NULL;

-- 7. Find any inconsistent state between users and turnkey_wallets
SELECT 'Users with mixed state' as info,
       u.telegram_id,
       u.turnkey_session_id as users_org_id,
       tw.turnkey_sub_org_id as wallets_org_id,
       u.public_key as users_public_key,
       tw.public_key as wallets_public_key,
       tw.is_active as wallet_active
FROM users u
LEFT JOIN turnkey_wallets tw ON u.telegram_id = tw.telegram_id
WHERE u.turnkey_session_id IS NOT NULL OR tw.turnkey_sub_org_id IS NOT NULL;

-- 8. Specific check: Find the user triggering the ca28fe57 org ID
SELECT 'User causing the issue' as info,
       telegram_id,
       'Legacy path via turnkey_session_id' as source,
       turnkey_session_id as org_id,
       public_key,
       session_expiry,
       kms_encrypted_session_key IS NOT NULL as has_kms_session
FROM users 
WHERE turnkey_session_id = 'ca28fe57-85c2-4649-9499-bd56404f473d'

UNION ALL

SELECT 'User causing the issue' as info,
       telegram_id::text,
       'New user path via turnkey_wallets' as source,
       turnkey_sub_org_id as org_id,
       public_key,
       created_at::text as session_expiry,
       is_active::text as has_kms_session
FROM turnkey_wallets 
WHERE turnkey_sub_org_id = 'ca28fe57-85c2-4649-9499-bd56404f473d';
