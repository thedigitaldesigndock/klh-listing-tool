# eBay Marketplace Account Deletion webhook (Cloudflare Worker)

One-page public HTTPS endpoint so we can submit the eBay App Growth
Check and lift our daily Trading API call limit.

## Deploy (first-time, ~10 min)

### 1. Install Wrangler

```bash
npm install -g wrangler
```

### 2. Log in to Cloudflare

```bash
wrangler login
```

Opens a browser — sign in / sign up (free tier is fine, you can use
an existing GitHub login). Grant Wrangler permission.

### 3. Deploy

From this directory:

```bash
cd cloudflare-worker
wrangler deploy
```

Wrangler prints the live URL, e.g.
`https://klh-ebay-notify.petercowgill.workers.dev`

### 4. Fix the endpoint URL

Open `wrangler.toml` and replace the `<YOUR-SUBDOMAIN>` placeholder in
`ENDPOINT_URL` with the real subdomain from step 3. Then redeploy:

```bash
wrangler deploy
```

(The ENDPOINT_URL must match the URL eBay calls, character-for-character,
because it's part of the challenge hash.)

### 5. Smoke-test the challenge response

```bash
curl "https://klh-ebay-notify.<sub>.workers.dev/?challenge_code=hello"
```

Should return something like:
`{"challengeResponse":"b7e2...a1f"}`

If you see that JSON, you're done on the Cloudflare side.

## Register with eBay

1. Go to <https://developer.ebay.com/my/push?tab=alerts>
2. Tab: **Marketplace Account Deletion/Closure Notifications**
3. Notification Endpoint URL: `https://klh-ebay-notify.<sub>.workers.dev/`
4. Verification Token: `0aTxRADdB6Ho3TXNacDK8A7xtLQcnuk5qzAe9AmO4L2etKMZ-GaXYA`
   (already set in wrangler.toml — paste this value into eBay's form)
5. Click **Save**. eBay fires a challenge, your Worker responds, status
   flips to "Subscribed".

## Then submit the Growth Check

`/docs/ebay-growth-check-draft.md` has all the pre-drafted answers.
Go to <https://developer.ebay.com/my/support/tickets?tab=app-check>,
paste the values, tick the license-agreement checkbox (must be Peter in
person), submit.

Approval is typically 1-3 business days.

## Verification token

The token in `wrangler.toml` is **not a secret** per eBay's design —
it's a shared value that ends up in the hash. Rotating it just requires
updating `wrangler.toml`, redeploying, and updating eBay's dashboard.

If you ever rotate it, run `python3 -c "import secrets; print(secrets.token_urlsafe(40))"`
for a fresh one.
