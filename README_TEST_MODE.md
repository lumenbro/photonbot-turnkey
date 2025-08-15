Test Mode Configuration

Environment variables:

- TEST_MODE=true
- STELLAR_NETWORK=PUBLIC|TESTNET
- HORIZON_URL_PUBLIC=https://horizon.stellar.org
- HORIZON_URL_TESTNET=https://horizon-testnet.stellar.org
- TEST_SIGNER_SECRET=SB...
- DB_HOST=localhost
- DB_PORT=5434
- DB_NAME=postgres
- DB_USER=botadmin
- DB_PASSWORD=...
- DB_SSL=disable

Behavior:

- Signing uses services/local_signer.py with TEST_SIGNER_SECRET
- AppContext.network_passphrase and horizon_url are selected by STELLAR_NETWORK
- Builders and API parse/build XDR using app_context.network_passphrase
- Local DB is used if TEST_MODE=true or DB_SSL=disable
