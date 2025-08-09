-- Fix database permissions for recovery integration
-- Run this as the postgres superuser

-- Grant necessary permissions to botadmin user for schema modifications
GRANT ALL PRIVILEGES ON ALL TABLES IN SCHEMA public TO botadmin;
GRANT ALL PRIVILEGES ON ALL SEQUENCES IN SCHEMA public TO botadmin;
GRANT CREATE ON SCHEMA public TO botadmin;

-- Specifically grant ALTER permissions on existing tables
ALTER TABLE users OWNER TO botadmin;
ALTER TABLE turnkey_wallets OWNER TO botadmin;
ALTER TABLE referrals OWNER TO botadmin;
ALTER TABLE founders OWNER TO botadmin;
ALTER TABLE trades OWNER TO botadmin;
ALTER TABLE rewards OWNER TO botadmin;
ALTER TABLE copy_trading OWNER TO botadmin;

-- Add the recovery columns manually with proper permissions
ALTER TABLE users ADD COLUMN IF NOT EXISTS recovery_mode BOOLEAN DEFAULT FALSE;
ALTER TABLE users ADD COLUMN IF NOT EXISTS recovery_org_id TEXT;
ALTER TABLE users ADD COLUMN IF NOT EXISTS recovery_session_expires TIMESTAMP;
ALTER TABLE users ADD COLUMN IF NOT EXISTS recovery_api_key_id TEXT;

-- Grant future permissions
ALTER DEFAULT PRIVILEGES IN SCHEMA public GRANT ALL ON TABLES TO botadmin;
ALTER DEFAULT PRIVILEGES IN SCHEMA public GRANT ALL ON SEQUENCES TO botadmin;

-- Verify permissions
\dt+ users

