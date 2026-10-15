/** Email Routing -> server-owned durable inbox. No external storage binding.
 * The SMTP delivery is rejected unless the server confirms durable receipt.
 * Raw mail, recipients and service secrets must never be logged.
 */
const MAX_BYTES = 20 * 1024 * 1024;
const encoder = new TextEncoder();
const hex = value => Array.from(new Uint8Array(value), b => b.toString(16).padStart(2, '0')).join('');

export default {
  async email(message, env) {
    let endpoint;
    try {endpoint = new URL(env.INVOICE_INGRESS_URL);} catch {
      message.setReject('Invoice receipt service is unavailable; please resend later'); return;
    }
    if (endpoint.protocol !== 'https:' || endpoint.username || endpoint.password || endpoint.hash
        || !env.INVOICE_INBOUND_SECRET || env.INVOICE_INBOUND_SECRET.length < 32) {
      message.setReject('Invoice receipt service is unavailable; please resend later'); return;
    }
    if (message.rawSize > MAX_BYTES) {
      message.setReject('Invoice email exceeds 20 MB'); return;
    }
    const reader = message.raw.getReader();
    const chunks = []; let size = 0;
    while (true) {
      const {value, done} = await reader.read();
      if (done) break;
      size += value.byteLength;
      if (size > MAX_BYTES) {
        await reader.cancel(); message.setReject('Invoice email exceeds 20 MB'); return;
      }
      chunks.push(value);
    }
    if (!size) {message.setReject('Empty invoice email'); return;}
    const raw = new Uint8Array(size); let offset = 0;
    for (const chunk of chunks) {raw.set(chunk, offset); offset += chunk.byteLength;}
    const recipient = message.to.toLowerCase(); // Trusted SMTP envelope, never MIME To/Cc.
    const stamp = String(Math.floor(Date.now()/1000));
    const digest = hex(await crypto.subtle.digest('SHA-256', raw));
    const key = await crypto.subtle.importKey('raw', encoder.encode(env.INVOICE_INBOUND_SECRET),
      {name:'HMAC', hash:'SHA-256'}, false, ['sign']);
    const signature = hex(await crypto.subtle.sign('HMAC', key,
      encoder.encode(`${stamp}\n${recipient}\n${digest}`)));
    let response;
    try {
      response = await fetch(endpoint.href, {method:'POST',redirect:'manual',body:raw,
        signal:AbortSignal.timeout(30000), headers:{'Content-Type':'message/rfc822',
          'X-Invoice-Timestamp':stamp,'X-Invoice-Recipient':recipient,'X-Invoice-Signature':signature}});
    } catch {
      message.setReject('Invoice server is unavailable; email was not accepted, please resend later'); return;
    }
    if (response.status === 200 || response.status === 202) return;
    if (response.status === 404) {
      message.setReject('Invoice recipient is unknown or disabled'); return;
    }
    if (response.status === 400 || response.status === 413) {
      message.setReject('Invalid invoice email or email exceeds 20 MB'); return;
    }
    message.setReject(`Invoice server did not accept this email (HTTP ${response.status}); please resend later`);
  }
};
