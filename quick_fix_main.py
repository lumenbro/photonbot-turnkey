# Quick fix for main.py - Replace the problematic schema update section
# This version checks for existing columns before trying to add them

# Replace the recovery columns section in main.py with this safer version:

"""
            -- Add recovery support columns (safer version)
            DO $$
            DECLARE
                column_exists INTEGER;
            BEGIN
                -- Check and add recovery_mode
                SELECT COUNT(*) INTO column_exists
                FROM information_schema.columns 
                WHERE table_name = 'users' AND column_name = 'recovery_mode';
                
                IF column_exists = 0 THEN
                    ALTER TABLE users ADD COLUMN recovery_mode BOOLEAN DEFAULT FALSE;
                END IF;
                
                -- Check and add recovery_org_id
                SELECT COUNT(*) INTO column_exists
                FROM information_schema.columns 
                WHERE table_name = 'users' AND column_name = 'recovery_org_id';
                
                IF column_exists = 0 THEN
                    ALTER TABLE users ADD COLUMN recovery_org_id TEXT;
                END IF;
                
                -- Check and add recovery_session_expires
                SELECT COUNT(*) INTO column_exists
                FROM information_schema.columns 
                WHERE table_name = 'users' AND column_name = 'recovery_session_expires';
                
                IF column_exists = 0 THEN
                    ALTER TABLE users ADD COLUMN recovery_session_expires TIMESTAMP;
                END IF;
                
                -- Check and add recovery_api_key_id
                SELECT COUNT(*) INTO column_exists
                FROM information_schema.columns 
                WHERE table_name = 'users' AND column_name = 'recovery_api_key_id';
                
                IF column_exists = 0 THEN
                    ALTER TABLE users ADD COLUMN recovery_api_key_id TEXT;
                END IF;
            END $$;
"""




