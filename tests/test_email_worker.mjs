import assert from 'node:assert/strict';
import {createHash, createHmac} from 'node:crypto';
import test from 'node:test';
import worker from '../deploy/cloudflare-email-worker.mjs';

const raw = new TextEncoder().encode('From: fixture@example.test\r\nTo: forged@example.test\r\n\r\nSynthetic only');
const env = {INVOICE_INGRESS_URL:'https://invoices.example.test/api/inbound/email',
  INVOICE_INBOUND_SECRET:'test-only-receipt-key-at-least-32-characters'};
function message() {
  return {to:'Invoice@example.test',rawSize:raw.length,raw:new Blob([raw]).stream(),
    rejected:'',setReject(reason){this.rejected=reason;}};
}

test('Cloudflare-compatible fetch signs the trusted SMTP envelope for the server', async t => {
  const original = globalThis.fetch; t.after(() => {globalThis.fetch=original;});
  globalThis.fetch = async (url, options) => {
    // Workers rejects redirect:error before making any network request.
    assert.equal(options.redirect, 'manual');
    assert.equal(url, env.INVOICE_INGRESS_URL);
    assert.equal(options.headers['X-Invoice-Recipient'], 'invoice@example.test');
    const digest=createHash('sha256').update(raw).digest('hex');
    const input=options.headers['X-Invoice-Timestamp']+'\ninvoice@example.test\n'+digest;
    const expected=createHmac('sha256',env.INVOICE_INBOUND_SECRET).update(input).digest('hex');
    assert.equal(options.headers['X-Invoice-Signature'],expected);
    return new Response('',{status:202});
  };
  const mail=message();await worker.email(mail,env);assert.equal(mail.rejected,'');
});

test('a redirected ingress never counts as durable acceptance', async t => {
  const original = globalThis.fetch; t.after(() => {globalThis.fetch=original;});
  globalThis.fetch = async (_url, options) => {
    assert.equal(options.redirect,'manual');return new Response('',{status:302});
  };
  const mail=message();await worker.email(mail,env);assert.match(mail.rejected,/did not accept/);
});
