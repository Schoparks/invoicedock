import assert from 'node:assert/strict';
import {createHash, createHmac} from 'node:crypto';
import {readFile} from 'node:fs/promises';
import test from 'node:test';

// Node has no cloudflare:email builtin. Replace only that runtime value object;
// execute the unchanged Worker handlers, storage state machine and signing logic.
const source=await readFile(new URL('../deploy/cloudflare-email-worker.mjs',import.meta.url),'utf8');
const runtime=source.replace("import {EmailMessage} from 'cloudflare:email';",
  'class EmailMessage {constructor(from,to,raw){this.from=from;this.to=to;this.raw=raw;}}');
const {default:worker}=await import('data:text/javascript;base64,'+Buffer.from(runtime).toString('base64'));
const raw=new TextEncoder().encode('From: fixture@example.test\r\nTo: forged@example.test\r\n\r\nSynthetic only');
const recipient='invoice@example.test';
const secret='test-only-receipt-key-at-least-32-characters';
const retention=7*24*60*60*1000;
const hash=value=>createHash('sha256').update(value).digest('hex');
const id=body=>hash(recipient+'\n'+hash(body));
const rawKey=body=>'raw/'+id(body)+'.eml';
const jobKey=body=>'queue/'+id(body)+'.json';
const ack=()=>Response.json({id:'a'.repeat(32),status:'queued',duplicate:false},{status:202});
function setup(t) {
  const objects=new Map(), notices=[];
  const original=globalThis.fetch; t.after(()=>{globalThis.fetch=original;});
  const bucket={
    async put(key,value) {objects.set(key,value);return {key};},
    async get(key) {
      const value=objects.get(key);
      return value===undefined ? null : {body:new Blob([value]).stream(),size:new Blob([value]).size,
        json:async()=>JSON.parse(value)};
    },
    async delete(key) {objects.delete(key);},
    async list({prefix,limit,cursor}) {
      const keys=[...objects.keys()].filter(key=>key.startsWith(prefix)).sort();
      const remaining=keys.filter(key=>!cursor || key>cursor), page=remaining.slice(0,limit);
      return {objects:page.map(key=>({key})),truncated:remaining.length>limit,cursor:page.at(-1)};
    }
  };
  return {objects,notices,env:{MAIL_BUCKET:bucket,INVOICE_RECEIPT_DOMAIN:'example.test',
    INVOICE_INGRESS_URL:'https://invoices.example.test/api/inbound/email',INVOICE_INBOUND_SECRET:secret,
    INVOICE_NOTIFY_FROM:'notices@example.test',INVOICE_NOTIFY_TO:'owner@verified.test',
    EXPIRY_EMAIL:{async send(message){notices.push(message);}}}};
}
function message(body=raw) {
  return {from:'fixture@example.test',to:'Invoice@example.test',rawSize:body.length,raw:new Blob([body]).stream(),
    rejected:'',setReject(reason){this.rejected=reason;}};
}
function alterJob(objects,body,values) {
  const job={...JSON.parse(objects.get(jobKey(body))),...values};
  objects.set(jobKey(body),JSON.stringify(job));return job;
}

test('SMTP saves MIME and queue without contacting the app; duplicates preserve the original deadline',async t=>{
  const {objects,env}=setup(t);
  globalThis.fetch=()=>{throw new Error('offline');};
  const mail=message();await worker.email(mail,env);assert.equal(mail.rejected,'');
  const job=alterJob(objects,raw,{received_at:Date.now()-10000,attempts:2});
  await worker.email(message(),env);
  assert.equal(objects.size,2);assert.deepEqual(objects.get(rawKey(raw)),raw);
  assert.deepEqual(JSON.parse(objects.get(jobKey(raw))),job);
  assert.equal(worker.fetch,undefined);
});

test('SMTP acceptance requires both raw storage and durable job; oversize and invalid mail reject safely',async t=>{
  const {objects,env}=setup(t);
  const put=env.MAIL_BUCKET.put;let release;
  env.MAIL_BUCKET.put=(...args)=>new Promise(resolve=>{release=async()=>{await put(...args);resolve();};});
  let complete=false;const pending=worker.email(message(),env).then(()=>{complete=true;});
  while(!release) await new Promise(resolve=>setImmediate(resolve));
  assert.equal(complete,false);await release();
  while(!objects.has(jobKey(raw)) && !complete) {
    await new Promise(resolve=>setImmediate(resolve));
    if(release) await release();
  }
  await pending;
  objects.clear();
  env.MAIL_BUCKET.put=async(key,...args)=>{
    if(key.startsWith('queue/')) throw new Error('private fixture');return put(key,...args);
  };
  const failed=message();await worker.email(failed,env);
  assert.match(failed.rejected,/not saved/);assert.doesNotMatch(failed.rejected,/private/);
  const huge=message();huge.rawSize=20*1024*1024+1;
  await worker.email(huge,env);assert.match(huge.rejected,/20 MB/);
  const empty=message(new Uint8Array());await worker.email(empty,env);assert.match(empty.rejected,/Empty/);
  const other=message();other.to='invoice@other.test';await worker.email(other,env);assert.match(other.rejected,/recipient/);
});

test('scheduled delivery persists backoff and signs the unchanged ingress contract',async t=>{
  const {objects,env}=setup(t);let now=Date.now();t.mock.method(Date,'now',()=>now);
  await worker.email(message(),env);let calls=0;
  globalThis.fetch=async()=>{calls++;throw new Error('offline');};
  await worker.scheduled({},env,{});assert.equal(calls,1);
  const job=JSON.parse(objects.get(jobKey(raw)));assert.equal(job.attempts,1);assert.equal(job.next_attempt,now+60000);
  await worker.scheduled({},env,{});assert.equal(calls,1);now+=61000;
  globalThis.fetch=async(url,options)=>{
    assert.equal(url,env.INVOICE_INGRESS_URL);assert.equal(options.redirect,'manual');
    assert.equal(options.headers['X-Invoice-Recipient'],recipient);assert.deepEqual(options.body,raw);
    const input=options.headers['X-Invoice-Timestamp']+'\n'+recipient+'\n'+hash(raw);
    assert.equal(options.headers['X-Invoice-Signature'],createHmac('sha256',secret).update(input).digest('hex'));
    return ack();
  };
  await worker.scheduled({},env,{});assert.equal(objects.has(rawKey(raw)),false);assert.equal(objects.has(jobKey(raw)),false);
});

test('redirects, application errors and false HTTP 200 never acknowledge durable receipt',async t=>{
  const {objects,env}=setup(t);
  for(const status of [302,404,413,500,200]) {
    objects.clear();await worker.email(message(),env);
    globalThis.fetch=async()=>new Response('<html>not a receipt</html>',{status});
    await worker.scheduled({},env,{});assert.ok(objects.has(rawKey(raw)),String(status));
    assert.equal(JSON.parse(objects.get(jobKey(raw))).attempts,1);
  }
});

test('cleanup failure after app acknowledgement resumes cleanup without forwarding or expiration notices',async t=>{
  const {objects,notices,env}=setup(t);await worker.email(message(),env);
  const remove=env.MAIL_BUCKET.delete;let interrupt=true,calls=0;
  env.MAIL_BUCKET.delete=async key=>{if(interrupt) throw new Error('delete interrupted');return remove(key);};
  globalThis.fetch=async()=>{calls++;return ack();};
  await worker.scheduled({},env,{});assert.equal(JSON.parse(objects.get(jobKey(raw))).phase,'delivered');
  interrupt=false;await worker.scheduled({},env,{});
  assert.equal(calls,1);assert.equal(notices.length,0);assert.equal(objects.has(rawKey(raw)),false);
});

test('pagination advances past deferred jobs',async t=>{
  const {objects,env}=setup(t);const bodies=[];
  for(let i=0;i<4;i++){const body=new TextEncoder().encode('synthetic-'+i);bodies.push(body);await worker.email(message(body),env);}
  const jobs=[...objects.keys()].filter(key=>key.startsWith('queue/')).sort();
  for(const key of jobs.slice(0,3)) objects.set(key,JSON.stringify({...JSON.parse(objects.get(key)),next_attempt:Date.now()+3600000}));
  let calls=0;globalThis.fetch=async()=>{calls++;return ack();};
  await worker.scheduled({},env,{});assert.equal(calls,0);
  await worker.scheduled({},env,{});assert.equal(calls,1);assert.equal(objects.has(jobs[3]),false);
});

test('IPv6 policy rejects A records but allows AAAA-only delivery',async t=>{
  const {objects,env}=setup(t);env.INVOICE_IPV6_ONLY='true';await worker.email(message(),env);
  let ipv4=true,calls=0;
  globalThis.fetch=async url=>{
    if(url.startsWith('https://cloudflare-dns.com/dns-query')) {
      const type=new URL(url).searchParams.get('type');
      return Response.json({Status:0,Answer:type==='AAAA'?[{type:28,data:'2001:db8::10'}]:ipv4?[{type:1,data:'192.0.2.10'}]:[]});
    }
    calls++;return ack();
  };
  await worker.scheduled({},env,{});assert.equal(calls,0);assert.ok(objects.has(rawKey(raw)));
  ipv4=false;await worker.scheduled({},env,{});assert.equal(calls,1);
});

test('corrupt MIME is retained until its expiry and cannot block later jobs',async t=>{
  const {objects,env}=setup(t);await worker.email(message(),env);objects.set(rawKey(raw),new TextEncoder().encode('tampered'));
  const second=new TextEncoder().encode('second synthetic mail');await worker.email(message(second),env);
  let calls=0;globalThis.fetch=async()=>{calls++;return ack();};
  await worker.scheduled({},env,{});assert.equal(calls,1);assert.ok(objects.has(rawKey(raw)));
  assert.equal(objects.has(rawKey(second)),false);
});

test('seven-day expiry persists a notice then cleans original even when origin DNS is unavailable',async t=>{
  const {objects,notices,env}=setup(t);await worker.email(message(),env);env.INVOICE_IPV6_ONLY='true';
  alterJob(objects,raw,{received_at:Date.now()-retention-1,last_status:'503'});
  globalThis.fetch=()=>{throw new Error('expiry must work without origin access');};
  await worker.scheduled({},env,{});
  assert.equal(objects.has(rawKey(raw)),false);assert.equal(objects.has(jobKey(raw)),false);
  assert.equal(notices.length,1);assert.equal(notices[0].to,env.INVOICE_NOTIFY_TO);
  assert.notEqual(notices[0].to,'fixture@example.test');assert.match(notices[0].raw,/Auto-Submitted: auto-generated/);
  const text=Buffer.from(notices[0].raw.split('\r\n\r\n')[1],'base64').toString();
  assert.match(text,/503/);assert.match(text,/7 天/);assert.doesNotMatch(text,/Synthetic only/);
});

test('notification failure retries only small metadata after cleanup; lifecycle-deleted originals still notify',async t=>{
  const {objects,notices,env}=setup(t);let now=Date.now();t.mock.method(Date,'now',()=>now);
  await worker.email(message(),env);alterJob(objects,raw,{received_at:now-retention-1});
  objects.delete(rawKey(raw)); // R2 raw/ lifecycle may expire before the next Cron scan.
  env.EXPIRY_EMAIL.send=async()=>{throw new Error('notification unavailable');};
  await worker.scheduled({},env,{});
  const job=JSON.parse(objects.get(jobKey(raw)));assert.equal(job.phase,'expired');assert.equal(job.next_attempt,now+60000);
  assert.equal(objects.has(rawKey(raw)),false);now+=61000;
  env.EXPIRY_EMAIL.send=async mail=>notices.push(mail);
  await worker.scheduled({},env,{});assert.equal(notices.length,1);assert.equal(objects.has(jobKey(raw)),false);
});

test('expiry never deletes original unless its notification task is durably saved',async t=>{
  const {objects,notices,env}=setup(t);await worker.email(message(),env);
  alterJob(objects,raw,{received_at:Date.now()-retention-1});
  env.MAIL_BUCKET.put=async()=>{throw new Error('metadata write failed');};
  await worker.scheduled({},env,{});assert.ok(objects.has(rawKey(raw)));assert.equal(notices.length,0);
});
