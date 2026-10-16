import {EmailMessage} from 'cloudflare:email';

/** Independent transport: SMTP -> private R2 -> scheduled signed delivery.
 * Queue metadata survives raw-mail expiry so admin notifications can retry independently.
 * Never log raw mail, addresses, service keys or third-party error messages.
 */
const MAX_BYTES = 20 * 1024 * 1024;
const RETENTION_MS = 7 * 24 * 60 * 60 * 1000;
const BATCH_SIZE = 3;
const encoder = new TextEncoder();
const hex = value => Array.from(new Uint8Array(value), b => b.toString(16).padStart(2, '0')).join('');
const sha256 = async value => hex(await crypto.subtle.digest('SHA-256', value));
const validEmail = value => typeof value === 'string' && value.length <= 320 && /^[^\s@<>]+@[^\s@<>]+$/.test(value);

function config(env) {
  const endpoint = new URL(env.INVOICE_INGRESS_URL);
  if (endpoint.protocol !== 'https:' || endpoint.username || endpoint.password || endpoint.hash
      || !env.MAIL_BUCKET || !env.INVOICE_RECEIPT_DOMAIN || env.INVOICE_INBOUND_SECRET?.length < 32
      || !env.INVOICE_INBOUND_SECRET || !env.EXPIRY_EMAIL
      || !validEmail(env.INVOICE_NOTIFY_FROM) || !validEmail(env.INVOICE_NOTIFY_TO)
      || env.INVOICE_NOTIFY_FROM.split('@')[1] !== env.INVOICE_RECEIPT_DOMAIN
      || env.INVOICE_NOTIFY_TO.split('@')[1] === env.INVOICE_RECEIPT_DOMAIN) {
    throw new Error('Invalid mail transport configuration');
  }
  return endpoint;
}

async function boundedBytes(stream) {
  const reader = stream.getReader();
  const chunks = []; let size = 0;
  while (true) {
    const {value, done} = await reader.read();
    if (done) break;
    size += value.byteLength;
    if (size > MAX_BYTES) {await reader.cancel(); throw new Error('size');}
    chunks.push(value);
  }
  const raw = new Uint8Array(size); let offset = 0;
  for (const chunk of chunks) {raw.set(chunk, offset); offset += chunk.byteLength;}
  return raw;
}

async function requireIPv6Origin(endpoint, env) {
  if (!['1','true','yes','on'].includes(String(env.INVOICE_IPV6_ONLY).toLowerCase())) return;
  const answers = await Promise.all(['A','AAAA'].map(async type => {
    const url = new URL('https://cloudflare-dns.com/dns-query');
    url.searchParams.set('name', endpoint.hostname); url.searchParams.set('type', type);
    const response = await fetch(url.href, {headers:{Accept:'application/dns-json'},
      redirect:'manual', signal:AbortSignal.timeout(10000)});
    if (response.status !== 200) throw new Error('Origin DNS check failed');
    const result = await response.json();
    if (result.Status !== 0) throw new Error('Origin DNS check failed');
    return result.Answer || [];
  }));
  if (answers[0].some(answer => answer.type === 1)
      || !answers[1].some(answer => answer.type === 28)) throw new Error('IPv6-only origin required');
}

async function signedDelivery(endpoint, env, recipient, raw, digest) {
  const stamp = String(Math.floor(Date.now()/1000));
  const key = await crypto.subtle.importKey('raw', encoder.encode(env.INVOICE_INBOUND_SECRET),
    {name:'HMAC', hash:'SHA-256'}, false, ['sign']);
  const signature = hex(await crypto.subtle.sign('HMAC', key,
    encoder.encode(`${stamp}\n${recipient}\n${digest}`)));
  const response = await fetch(endpoint.href, {method:'POST', redirect:'manual', body:raw,
    signal:AbortSignal.timeout(30000), headers:{'Content-Type':'message/rfc822',
      'X-Invoice-Timestamp':stamp, 'X-Invoice-Recipient':recipient, 'X-Invoice-Signature':signature}});
  if (![200,202].includes(response.status)) return response.status;
  const result = await response.json();
  if (!/^[0-9a-f]{32}$/.test(result.id || '')
      || !['queued','processing','completed','failed'].includes(result.status)) return 502;
  return 0;
}

const base64 = text => btoa(String.fromCharCode(...encoder.encode(text)));

async function notifyExpired(env, job) {
  // Only the configured, verified administrator receives notices, never arbitrary senders.
  const body = [
    '一封发票邮件暂存已满 7 天，仍未得到票栈的接收确认，原邮件已进入过期清理。',
    `收票地址：${job.recipient}`,
    `发件地址：${job.sender || '未知'}`,
    `云端接收时间：${new Date(job.received_at).toISOString()}`,
    `邮件编号：${job.id}`,
    `最后投递状态：${job.last_status || '未成功投递'}`,
    '', '请检查收信链路；如仍需要该发票，请联系发件人重新发送。',
    '此提醒不包含原邮件正文或发票附件。'
  ].join('\r\n');
  const mime = [
    `From: ${env.INVOICE_NOTIFY_FROM}`, `To: ${env.INVOICE_NOTIFY_TO}`,
    `Subject: =?UTF-8?B?${base64('票栈：收票过期提醒')}?=`,
    `Message-ID: <expiry-${job.id}@${env.INVOICE_RECEIPT_DOMAIN}>`,
    'Auto-Submitted: auto-generated', 'MIME-Version: 1.0',
    'Content-Type: text/plain; charset=utf-8', 'Content-Transfer-Encoding: base64', '',
    base64(body).match(/.{1,76}/g).join('\r\n')
  ].join('\r\n');
  await env.EXPIRY_EMAIL.send(new EmailMessage(env.INVOICE_NOTIFY_FROM, env.INVOICE_NOTIFY_TO, mime));
}

async function saveJob(bucket, key, job) {
  await bucket.put(key, JSON.stringify(job), {httpMetadata:{contentType:'application/json'}});
}

async function deferJob(bucket, key, job, status) {
  job.attempts = Math.min(1000000, Number(job.attempts || 0) + 1);
  job.next_attempt = Date.now() + Math.min(3600000, 60000 * 2 ** Math.min(job.attempts - 1, 6));
  job.last_status = String(status);
  await saveJob(bucket, key, job);
}

async function deliverBatch(env, endpoint) {
  const bucket = env.MAIL_BUCKET;
  const state = await bucket.get('state/cursor.json');
  const cursor = state ? (await state.json()).cursor : undefined;
  let page;
  try {
    page = await bucket.list({prefix:'queue/', limit:BATCH_SIZE, cursor:cursor || undefined});
  } catch {
    await bucket.put('state/cursor.json', JSON.stringify({cursor:null}));
    throw new Error('Mail listing failed');
  }
  const summary = {delivered:0, expired:0, notified:0, deferred:0, failed:0};
  let originReady;
  for (const item of page.objects) {
    const id = /^queue\/([0-9a-f]{64})\.json$/.exec(item.key)?.[1];
    if (!id) {summary.failed++; continue;}
    const rawKey = `raw/${id}.eml`;
    try {
      const object = await bucket.get(item.key);
      if (!object) continue;
      const job = await object.json();
      if (job.id !== id || !validEmail(job.recipient) || !Number.isFinite(job.received_at)
          || !/^[0-9a-f]{64}$/.test(job.raw_hash || '')
          || await sha256(encoder.encode(`${job.recipient}\n${job.raw_hash}`)) !== id) {
        summary.failed++; continue;
      }
      if (job.phase === 'delivered' || job.phase === 'notified') {
        await bucket.delete(rawKey); await bucket.delete(item.key); continue;
      }
      if (job.phase === 'pending' && Date.now() - job.received_at >= RETENTION_MS) {
        job.phase = 'expired'; job.attempts = 0; job.next_attempt = 0;
        await saveJob(bucket, item.key, job); // Durable notice task before deleting the original.
      }
      if (job.phase === 'expired') {
        // Cleanup and reminders work even when the IPv6 origin or the application is down.
        await bucket.delete(rawKey);
        summary.expired++;
        if (job.next_attempt > Date.now()) {summary.deferred++; continue;}
        try {
          await notifyExpired(env, job);
        } catch {
          await deferJob(bucket, item.key, job, 'notification_failed');
          summary.failed++; continue;
        }
        job.phase = 'notified';
        await saveJob(bucket, item.key, job);
        await bucket.delete(item.key);
        summary.notified++;
        continue;
      }
      if (job.phase !== 'pending') {summary.failed++; continue;}
      if (job.next_attempt > Date.now()) {summary.deferred++; continue;}
      if (originReady === undefined) {
        try {await requireIPv6Origin(endpoint, env); originReady = true;} catch {originReady = false;}
      }
      if (!originReady) {summary.failed++; continue;}
      const mail = await bucket.get(rawKey);
      if (!mail || mail.size > MAX_BYTES) {
        await deferJob(bucket, item.key, job, 'raw_mail_unavailable'); summary.failed++; continue;
      }
      const raw = await boundedBytes(mail.body);
      if (!raw.length || await sha256(raw) !== job.raw_hash) {
        await deferJob(bucket, item.key, job, 'integrity_failed'); summary.failed++; continue;
      }
      let status;
      try {status = await signedDelivery(endpoint, env, job.recipient, raw, job.raw_hash);} catch {status = 'network_or_ack';}
      if (status === 0) {
        job.phase = 'delivered'; await saveJob(bucket, item.key, job);
        await bucket.delete(rawKey); await bucket.delete(item.key);
        summary.delivered++;
      } else {
        await deferJob(bucket, item.key, job, status); summary.failed++;
      }
    } catch {
      summary.failed++;
    }
  }
  await bucket.put('state/cursor.json', JSON.stringify({cursor:page.truncated ? page.cursor : null}));
  return summary;
}

export default {
  async email(message, env) {
    try {config(env);} catch {
      message.setReject('Invoice receipt storage is unavailable; please resend later'); return;
    }
    const recipient = message.to.toLowerCase();
    if (!validEmail(recipient) || recipient.split('@')[1] !== env.INVOICE_RECEIPT_DOMAIN) {
      message.setReject('Invalid invoice recipient'); return;
    }
    if (message.rawSize > MAX_BYTES) {message.setReject('Invoice email exceeds 20 MB'); return;}
    try {
      const raw = await boundedBytes(message.raw);
      if (!raw.length) {message.setReject('Empty invoice email'); return;}
      const digest = await sha256(raw);
      const id = await sha256(encoder.encode(`${recipient}\n${digest}`));
      const jobKey = `queue/${id}.json`;
      if (await env.MAIL_BUCKET.get(jobKey)) return; // Do not reset existing retry/expiry state.
      await env.MAIL_BUCKET.put(`raw/${id}.eml`, raw, {httpMetadata:{contentType:'message/rfc822'}});
      await saveJob(env.MAIL_BUCKET, jobKey, {id, phase:'pending', recipient,
        sender:validEmail(message.from) ? message.from : '', raw_hash:digest,
        received_at:Date.now(), attempts:0, next_attempt:0, last_status:''});
      // SMTP success requires both the MIME and its durable queue/notification metadata.
    } catch {
      message.setReject('Invoice email was not saved; please resend later');
    }
  },

  async scheduled(_controller, env, _ctx) {
    try {console.log('Mail delivery batch', await deliverBatch(env, config(env)));} catch {
      console.error('Mail delivery deferred; check storage, endpoint, secret and notification configuration');
    }
  }
};
