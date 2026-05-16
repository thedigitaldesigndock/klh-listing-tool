// eBay Marketplace Account Deletion notification endpoint.
//
// Required before submitting the App Growth Check to lift our daily
// Trading API call limit. eBay needs a public HTTPS URL that:
//
//   1. Responds to a GET challenge with a SHA-256 hex hash of
//      (challenge_code + verification_token + endpoint_url).
//   2. Accepts POST notifications containing account-deletion events
//      and returns HTTP 200.
//
// Both VERIFICATION_TOKEN and ENDPOINT_URL are Cloudflare Worker
// environment variables set in wrangler.toml / the dashboard. The
// endpoint URL must match exactly what's registered in eBay's Developer
// dashboard (full https:// URL, no trailing slash unless you registered
// with a trailing slash).
//
// We don't persist the POST payloads — this app has no user accounts
// beyond the single seller, so there's nothing to delete in response.
// Logging to `console.log` is enough for Cloudflare's tail UI and
// eBay's audit requirements.

export default {
  async fetch(request, env) {
    const url = new URL(request.url);

    // --- GET: challenge-response handshake ---
    if (request.method === "GET") {
      const challenge = url.searchParams.get("challenge_code");
      if (!challenge) {
        return new Response("missing challenge_code", { status: 400 });
      }
      const payload = challenge + env.VERIFICATION_TOKEN + env.ENDPOINT_URL;
      const digest = await crypto.subtle.digest(
        "SHA-256",
        new TextEncoder().encode(payload),
      );
      const hex = Array.from(new Uint8Array(digest))
        .map((b) => b.toString(16).padStart(2, "0"))
        .join("");
      return new Response(
        JSON.stringify({ challengeResponse: hex }),
        { headers: { "content-type": "application/json" } },
      );
    }

    // --- POST: account-deletion notification ---
    if (request.method === "POST") {
      const body = await request.text();
      console.log("ebay-account-deletion", body);
      // eBay requires HTTP 200 within 3s. No body needed.
      return new Response("", { status: 200 });
    }

    return new Response("Method not allowed", { status: 405 });
  },
};
