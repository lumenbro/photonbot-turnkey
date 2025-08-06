// routes/login.js - Backend for login/session creation
const express = require('express');
const router = express.Router();
const pool = require('../db');
const fetch = require('node-fetch');
const crypto = require('crypto');
const { decryptCredentialBundle } = require('@turnkey/crypto');
const KMSService = require('../services/kmsService');

// Initialize KMS service
const kmsService = new KMSService();

// Fetch BOT_TOKEN from env
const BOT_TOKEN = process.env.TELEGRAM_BOT_TOKEN;

// Validate Telegram initData
function validateInitData(initData) {
  const parsed = new URLSearchParams(initData);
  const hash = parsed.get('hash');
  parsed.delete('hash');
  const dataCheckString = Array.from(parsed.entries())
    .sort((a, b) => a[0].localeCompare(b[0]))
    .map(([key, value]) => `${key}=${value}`)
    .join('\n');
  const secretKey = crypto.createHmac('sha256', 'WebAppData').update(BOT_TOKEN).digest();
  const computedHash = crypto.createHmac('sha256', secretKey).update(dataCheckString).digest('hex');
  return computedHash === hash;
}

// Fetch user email
router.get('/get-user-email', async (req, res) => {
  const { orgId } = req.query;
  try {
    const userRes = await pool.query(
      "SELECT user_email FROM users u JOIN turnkey_wallets tw ON u.telegram_id = tw.telegram_id WHERE tw.turnkey_sub_org_id = $1",
      [orgId]
    );
    if (userRes.rows.length === 0) {
      console.warn('No user found for orgId, using fallback email');
      return res.json({ email: 'bpeterscqa@gmail.com' });
    }
    res.json({ email: userRes.rows[0].user_email || 'bpeterscqa@gmail.com' });
  } catch (e) {
    console.error(e);
    res.status(500).json({ error: e.message });
  }
});

// Fetch userId
router.get('/get-user-id', async (req, res) => {
  const { orgId } = req.query;
  try {
    const userRes = await pool.query(
      "SELECT u.turnkey_user_id FROM turnkey_wallets tw JOIN users u ON tw.telegram_id = u.telegram_id WHERE tw.turnkey_sub_org_id = $1",
      [orgId]
    );
    if (userRes.rows.length === 0) throw new Error("User not found");
    res.json({ userId: userRes.rows[0].turnkey_user_id });
  } catch (e) {
    console.error(e);
    res.status(500).json({ error: e.message });
  }
});

// Legacy /login GET
router.get('/login', (req, res) => {
  const email = req.query.email || 'unknown@lumenbro.com';
  const orgId = req.query.orgId;
  if (!orgId) return res.status(400).json({ error: "Missing orgId" });
  res.render('login', { email, org_id: orgId });
});

// POST /mini-app/create-session
router.post('/mini-app/create-session', async (req, res) => {
  const { body: bodyStr, stamp: stampStr, ephemeralPrivateKey, initData } = req.body;

  console.log('Received request payload:', req.body);

  if (!validateInitData(initData)) {
    return res.status(403).json({ error: "Invalid initData" });
  }

  try {
    console.log('Validated initData. Sending to Turnkey...');
    console.log('Body sent to Turnkey:', bodyStr);
    console.log('Stamp sent to Turnkey:', stampStr);
    console.log('EphemeralPrivateKey received:', ephemeralPrivateKey, 'type:', typeof ephemeralPrivateKey);

    // Proxy to Turnkey
    const turnkeyRes = await fetch('https://api.turnkey.com/public/v1/submit/create_read_write_session', {
      method: 'POST',
      headers: {
        'Content-Type': 'application/json',
        'Accept': 'application/json',
        'X-Stamp': stampStr
      },
      body: bodyStr
    });

    const responseText = await turnkeyRes.text();
    console.log('Turnkey raw response:', responseText);

    if (!turnkeyRes.ok) {
      throw new Error(`Turnkey error: ${responseText}`);
    }

    const data = JSON.parse(responseText);
    const credentialBundle = data.activity.result.createReadWriteSessionResultV2.credentialBundle;
    const sessionId = data.activity.result.createReadWriteSessionResultV2.apiKeyId;

    // Validate ephemeralPrivateKey
    if (!ephemeralPrivateKey || typeof ephemeralPrivateKey !== 'string' || !/^[0-9a-fA-F]{64}$/.test(ephemeralPrivateKey)) {
      throw new Error('Invalid ephemeralPrivateKey format');
    }

    // Decrypt using official Turnkey SDK function (private key as hex string)
    const decrypted = decryptCredentialBundle(credentialBundle, ephemeralPrivateKey);

    // The decrypted is hex string of the private key
    const apiPrivateKey = decrypted;
    const privateKeyBuf = Buffer.from(decrypted, 'hex');

    // Derive public key
    const ecdh = crypto.createECDH('prime256v1');
    ecdh.setPrivateKey(privateKeyBuf);
    const apiPublicKey = ecdh.getPublicKey('hex', 'compressed');

    // Parse orgId from bodyStr
    const bodyObj = JSON.parse(bodyStr);
    const orgId = bodyObj.organizationId;

    // Get telegramId
    const userRes = await pool.query(
      "SELECT telegram_id FROM turnkey_wallets WHERE turnkey_sub_org_id = $1",
      [orgId]
    );
    if (userRes.rows.length === 0) throw new Error("User not found");
    const telegramId = userRes.rows[0].telegram_id;

    // Calculate expiry
    const expirationSeconds = parseInt(bodyObj.parameters.expirationSeconds, 10);
    const sessionExpiry = new Date(Date.now() + expirationSeconds * 1000).toISOString();

    // Encrypt session keys with KMS before storing
    const { encryptedData, keyId } = await kmsService.encryptSessionKeys(apiPublicKey, apiPrivateKey);

    // Store encrypted session keys in DB
    await pool.query(
      "UPDATE users SET kms_encrypted_session_key = $1, kms_key_id = $2, turnkey_session_id = $3, session_expiry = $4, session_created_at = NOW() WHERE telegram_id = $5",
      [encryptedData, keyId, sessionId, sessionExpiry, telegramId]
    );

    res.json({ success: true });
  } catch (e) {
    console.error(`Login auth failed: ${e.message}`);
    res.status(500).json({ error: e.message });
  }
});

module.exports = router;
