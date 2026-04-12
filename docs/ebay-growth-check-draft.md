# eBay Application Growth Check — saved form contents

Drafted 2026-04-11 in the eBay Developer dashboard for `klhautographs` /
`KLHAutographs-Listing-Tool`. Not submitted — gated on:

1. The seller account being subscribed to **eBay Marketplace Account
   Deletion notifications** (requires a public HTTPS webhook endpoint
   on our side — not yet wired).
2. Ticking the "I have read and understood the API License Agreement"
   checkbox (a personal legal acceptance — must be done by Peter in
   person, not by automation).

When both are ready, navigate to
<https://developer.ebay.com/my/support/tickets?tab=app-check> and
paste the field values below.

---

## Field: Application Title / Summary

```
KLHAutographs Listing Tool — internal single-seller listing management
```

## Field: Application Details (rich text)

```
What the application does

KLHAutographs Listing Tool is an internal, single-seller command-line tool built for the eBay seller klhautographs (a UK sole trader selling hand-signed memorabilia — signed photographs, signed cards, and framed signed displays). It is not a marketplace, not a multi-tenant SaaS, not resold, and not used by anyone other than this one seller. It runs locally on the seller's own computer, talks to eBay using an OAuth user token for the same klhautographs account, and has no end users other than the seller and her husband who built it.

The tool performs three jobs:

1. Catalogue hygiene / audit — sweeps the seller's own active listings via GetMyeBaySelling, caches them in a local SQLite file, and flags title typos (double spaces, trailing whitespace, literal underscore fragments from a legacy Photoshop script), missing keywords, and stale dead-wood listings. Fixes are applied via ReviseFixedPriceItem one-at-a-time with dry-run preview and explicit confirmation — no bulk unsupervised edits.

2. New listings — the seller photographs newly-signed items, runs them through a Pillow compositor that renders mount/frame mockups from templates, uploads pictures via UploadSiteHostedPictures, and creates listings via VerifyAddFixedPriceItem (always) followed by AddFixedPriceItem (on confirm). Uses Business Policies for shipping/payment/returns.

3. Out-of-stock control — one-of-one signed items go to Quantity=0 via ReviseInventoryStatus when sold (keeping item ID, search history and watchers) and come back via ReviseInventoryStatus when a new signed copy is obtained. SetUserPreferences was used once to enable OutOfStockControl on the seller's account.

Typical flow: GetMyeBaySelling → GetItem (deep details) → local cache → audit rules → ReviseFixedPriceItem for approved edits. Or: VerifyAddFixedPriceItem → UploadSiteHostedPictures (×4 per listing) → AddFixedPriceItem.

Call volume

The seller's active catalogue is ~13,840 listings on ebay.co.uk. Expected normal steady-state daily volume is well under the default 5,000 calls/day limit, but a full catalogue audit pass (GetItem for every active listing) requires one-time bursts of ~14,000 GetItem calls spread over a few days. We hit the 5,000/day limit today on a first audit sweep, which is the reason for this request.

Data handling

All data stays on the seller's Mac in a local SQLite file. No third-party data sharing. No end users other than the klhautographs seller account holder. No PII stored beyond what eBay itself returns for the seller's own listings. OAuth user token and refresh token stored with chmod 600 in ~/.klh/ on the seller's machine.

No eBay Partner Network / affiliate usage.
```

## Field: Products

`Trading API`

## Field: Purpose of Request

`Increase My Call Limit`

## Field: eBay Partner Network member

`No`

## Field: Application ID

```
KimCowgi-KLHAutog-PRD-36c1d3885-6addea6a
```

## Field: Application URL

```
https://www.ebay.co.uk/usr/klhautographs
```

## Field: Call Volume Estimate

```
Call Name                     Hourly   Daily
GetMyeBaySelling              ~200     ~800
GetItem                       ~600     ~3000
ReviseFixedPriceItem          ~100     ~500
ReviseInventoryStatus          ~50     ~200
VerifyAddFixedPriceItem        ~50     ~200
AddFixedPriceItem              ~50     ~200
UploadSiteHostedPictures      ~200     ~800
EndFixedPriceItem              ~10     ~50
SetUserPreferences              ~1     ~1
GetUserPreferences              ~1     ~5
GetApiAccessRules               ~1     ~5

Steady-state daily total: ~5,500 calls/day.

Peak bursts (first full catalogue audit sweep, and periodic refreshes ~monthly): up to 15,000 GetItem calls + 14,000 ReviseFixedPriceItem calls spread over 2–3 days. Rate limited on our side to 2 calls/sec max.

Note: this is a single-seller application. Only the klhautographs seller account holder makes calls. There are no end users beyond that one account.
```

## Field: Cc

*(empty — optional)*

## Field: Attach Documents

*(empty — optional)*

## Checkbox: I have read and understood the API License Agreement and Policies

**Must be ticked by Peter at submission time.**

---

## Prerequisite not yet met

Before this form can be submitted, the `klhautographs` production app must be subscribed to eBay's Marketplace Account Deletion notifications. This requires:

1. A public HTTPS endpoint that eBay can POST account-deletion notifications to.
2. The endpoint registered in the Developer dashboard under Application > Notifications > Marketplace Account Deletion.
3. A SHA-256-hex challenge-response handshake on the endpoint (eBay sends a `challenge_code`, we return `base64(sha256(challenge_code + verification_token + endpoint))`).

See `/grow/application-growth-check` landing page for the requirement and `https://developer.ebay.com/api-docs/commerce/notification/overview.html` for the notification docs.
