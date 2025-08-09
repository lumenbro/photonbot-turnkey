-- RDS Permission Fix for Recovery Integration
-- Run this after connecting as postgres user to RDS

-- First, let's check current table ownership
\dt+ users

-- Grant botadmin full privileges on the database
GRANT ALL PRIVILEGES ON DATABASE postgres TO botadmin;

-- Grant schema usage and creation rights
GRANT USAGE, CREATE ON SCHEMA public TO botadmin;

-- Grant permissions on existing tables
GRANT ALL PRIVILEGES ON ALL TABLES IN SCHEMA public TO botadmin;
GRANT ALL PRIVILEGES ON ALL SEQUENCES IN SCHEMA public TO botadmin;

-- Grant future permissions (for any new tables/sequences)
ALTER DEFAULT PRIVILEGES IN SCHEMA public GRANT ALL ON TABLES TO botadmin;
ALTER DEFAULT PRIVILEGES IN SCHEMA public GRANT ALL ON SEQUENCES TO botadmin;

-- Change ownership of existing tables to botadmin
ALTER TABLE users OWNER TO botadmin;
ALTER TABLE turnkey_wallets OWNER TO botadmin;
ALTER TABLE referrals OWNER TO botadmin;
ALTER TABLE founders OWNER TO botadmin;
ALTER TABLE trades OWNER TO botadmin;
ALTER TABLE rewards OWNER TO botadmin;
ALTER TABLE copy_trading OWNER TO botadmin;

-- Now add the recovery columns (since botadmin will own the table)
ALTER TABLE users ADD COLUMN IF NOT EXISTS recovery_mode BOOLEAN DEFAULT FALSE;
ALTER TABLE users ADD COLUMN IF NOT EXISTS recovery_org_id TEXT;
ALTER TABLE users ADD COLUMN IF NOT EXISTS recovery_session_expires TIMESTAMP;
ALTER TABLE users ADD COLUMN IF NOT EXISTS recovery_api_key_id TEXT;

-- Verify the new columns were added
\d+ users

-- Show final table ownership
\dt+ users

-- Exit
\q


