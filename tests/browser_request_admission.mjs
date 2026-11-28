// Real same-origin v8 browser checks. Credentials arrive only on test stdin.
import assert from 'node:assert/strict';
import {createRequire} from 'node:module';
import {mkdir} from 'node:fs/promises';
import {createInterface} from 'node:readline';
import path from 'node:path';

const input = createInterface({input: process.stdin, crlfDelay: Infinity});
const lines = input[Symbol.asyncIterator]();
async function receive() {
  const {value, done} = await lines.next();
  assert.equal(done, false, 'test coordination ended early');
  return JSON.parse(value);
}
const config = await receive();
assert.ok(['quota_read', 'quota_submit', 'quota_poll', 'expiry_after_connect'].includes(config.mode));
const base = new URL(config.url);
assert.equal(base.hostname, '127.0.0.1');
assert.equal(base.protocol, 'http:');
const require = createRequire(import.meta.url);
const {chromium} = require(process.env.PI_PLAYWRIGHT_DIR || 'playwright');
const browser = await chromium.launch({headless: true});
process.once('SIGTERM', async () => {await browser.close(); process.exit(143);});
const context = await browser.newContext({viewport: {width: 1280, height: 900}});
const page = await context.newPage();
const errors = [], pageErrors = [], foreign = [], calls = [], posts = [], responses = [];
page.on('console', message => {if (message.type() === 'error') errors.push(message.text());});
page.on('pageerror', error => pageErrors.push(error.message));
page.on('request', request => {
  const url = new URL(request.url());
  if (url.origin !== base.origin && url.protocol !== 'data:') foreign.push(url.pathname);
  if (url.pathname.startsWith('/v1/')) calls.push({method: request.method(), path: url.pathname});
  if (url.pathname === '/v1/operations' && request.method() === 'POST') posts.push(request.postData());
});
page.on('response', response => {
  if (new URL(response.url()).pathname.startsWith('/v1/')) responses.push(response.status());
});
await context.route('**/*', route => new URL(route.request().url()).origin === base.origin ? route.continue() : route.abort());
await mkdir(config.screenshots, {recursive: true});
const shot = name => page.screenshot({path: path.join(config.screenshots, name + '.png'), fullPage: true});
const emit = value => console.log(JSON.stringify(value));
const button = name => page.getByRole('button', {name, exact: true});

async function connect(token) {
  await page.getByLabel('Access token', {exact: true}).fill(token);
  await button('Connect').click();
  await page.waitForFunction(() => document.getElementById('notice').textContent.startsWith('Connected.'), null, {timeout: 35000});
  assert.equal(await page.locator('#credential').inputValue(), '');
}
async function waitForQuota(operationVisible = false) {
  await page.waitForFunction(() => document.getElementById('notice').textContent.includes('request rate limit'), null, {timeout: 35000});
  const notice = await page.locator('#notice').textContent();
  const match = notice.match(/waiting about ([1-9][0-9]?) second/);
  assert.ok(match && Number(match[1]) <= 60, notice);
  assert.ok(notice.includes('Service-side work is not cancelled'));
  assert.ok(notice.includes('nothing is retried automatically'));
  assert.equal(await page.locator('#session-controls').isVisible(), true);
  assert.equal(await page.locator('#operation-panel').isVisible(), operationVisible);
}
async function noSavedCredential() {
  assert.deepEqual(await page.evaluate(() => ({local: localStorage.length, session: sessionStorage.length})), {local: 0, session: 0});
  assert.deepEqual(await context.cookies(), []);
}
async function readsSettled() {
  // The navigation's networkidle event may already have fired while later API
  // reads are still pending. Wait for the real controller's read completions,
  // including response-body handling, before checking the exact request trace.
  // This issues no request and preserves the existing 35-second UI bound.
  await page.waitForFunction(() => ['refresh-list', 'refresh-history', 'check-operation']
    .every(id => !document.getElementById(id).disabled), null, {timeout: 35000});
}

try {
  // Skill-guided first checks: rendering, actual error listeners and controls,
  // before entering credentials or proceeding with the behavioral scenario.
  assert.equal((await page.goto(base.href, {waitUntil: 'networkidle'})).status(), 200);
  await button('Connect').waitFor();
  assert.ok((await page.locator('body').innerText()).includes('Your workspace, in context.'));
  assert.equal(await page.locator('[data-nextjs-dialog], .vite-error-overlay, #webpack-dev-server-client-overlay').count(), 0);
  assert.deepEqual(errors, []);
  assert.deepEqual(pageErrors, []);
  await shot('01-connect');
  emit({event: 'initial_browser_verification_passed'});
  await connect(config.token);
  assert.equal(calls.length, 1);
  if (config.mode === 'expiry_after_connect') {
    await button(config.session).click();
    await page.waitForFunction(reply => document.getElementById('final-answer').textContent === reply,
      config.reply, {timeout: 35000});
    await readsSettled();
    assert.ok((await page.locator('#messages').textContent()).includes(config.reply));
    assert.equal(await page.locator('#operation-id').textContent(), config.operation);
    assert.equal(await page.locator('#operation-panel').isVisible(), true);
    assert.deepEqual(responses, [200, 200, 200, 200]);
    await shot('02-retained-conversation');
    emit({event: 'credential_connected'});
    assert.equal((await receive()).event, 'credential_expired');
    await button('Refresh').click();
    await page.waitForFunction(() => document.getElementById('notice').textContent.includes('did not accept this credential'), null, {timeout: 35000});
    assert.equal(await page.locator('#connect-form').isVisible(), true);
    assert.equal(await page.locator('#session-controls').isVisible(), false);
    assert.equal(await page.locator('#credential').inputValue(), '');
    assert.equal(await page.locator('#messages').textContent(), '');
    assert.equal(await page.locator('#sessions').textContent(), '');
    assert.equal(await page.locator('#final-answer').textContent(), '');
    assert.equal(await page.locator('#operation-id').textContent(), '');
    assert.equal(await page.locator('#operation-panel').isVisible(), false);
    await noSavedCredential();
    await shot('03-expired');
    await connect(config.other_token);
    assert.equal(await page.locator('#sessions').textContent(), '');
    assert.ok(!(await page.locator('body').innerText()).includes(config.reply));
    await shot('04-other-tenant');
    assert.deepEqual(responses, [200, 200, 200, 200, 401, 200]);
    assert.equal(posts.length, 0);
  } else if (config.mode === 'quota_poll') {
    await page.getByLabel('New conversation name', {exact: true}).fill('rate-task');
    await button('Start conversation').click();
    await page.getByLabel('Message', {exact: true}).fill('Read README.md and answer.');
    await button('Send message').click();
    await waitForQuota(true);
    await readsSettled();
    const operation = await page.locator('#operation-id').textContent();
    assert.match(operation, /^[a-f0-9]{64}$/);
    assert.ok((await page.locator('#operation-state').textContent()).startsWith('Accepted.'));
    assert.equal(await page.locator('#final-answer').textContent(), '');
    assert.equal(await button('Request cancellation').isVisible(), false);
    assert.equal(await button('Retry the same submission').isVisible(), false);
    assert.equal(posts.length, 1);
    assert.deepEqual(responses, [200, 202, 429, 429, 429]);
    const count = calls.length;
    await page.waitForTimeout(1800);
    assert.equal(calls.length, count, 'quota refusal must stop operation polling');
    const response = page.waitForResponse(r => new URL(r.url()).pathname === '/v1/operations/' + operation);
    await button('Check status').click();
    assert.equal((await response).status(), 429);
    await waitForQuota(true);
    await readsSettled();
    assert.deepEqual(calls.at(-1), {method: 'GET', path: '/v1/operations/' + operation});
    assert.equal(calls.length, count + 1);
    assert.equal(posts.length, 1, 'explicit status check cannot resubmit the accepted task');
    assert.equal(await page.locator('#operation-id').textContent(), operation);
    assert.deepEqual(responses, [200, 202, 429, 429, 429, 429]);
    await shot('02-rate-limited-accepted');
  } else {
    if (config.mode === 'quota_read') {
      await button('Refresh').click();
    } else {
      await page.getByLabel('New conversation name', {exact: true}).fill('rate-task');
      await button('Start conversation').click();
      await page.getByLabel('Message', {exact: true}).fill('Keep this exact browser request.');
      await button('Send message').click();
    }
    await waitForQuota();
    await readsSettled();
    const count = calls.length;
    // Observe beyond the current automatic operation-poll interval. No request
    // here is sent to the service to obtain the evidence, so quota is unchanged.
    await page.waitForTimeout(1800);
    assert.equal(calls.length, count, 'refusal triggered an automatic request');
    if (config.mode === 'quota_submit') {
      assert.equal(posts.length, 1);
      assert.equal(await button('Retry the same submission').isVisible(), true);
      assert.equal(await button('Send message').isEnabled(), false);
      assert.equal(await page.locator('#message').inputValue(), 'Keep this exact browser request.');
      const response = page.waitForResponse(r => r.request().method() === 'POST' && new URL(r.url()).pathname === '/v1/operations');
      await button('Retry the same submission').click();
      assert.equal((await response).status(), 429);
      await waitForQuota();
      assert.equal(posts.length, 2);
      assert.equal(posts[1], posts[0]);
      assert.match(JSON.parse(posts[0]).submission_key, /^[a-f0-9]{32}$/);
      assert.deepEqual(responses, [200, 429, 429]);
    } else {
      assert.equal(posts.length, 0);
      assert.deepEqual(responses, [200, 429]);
    }
    await shot('02-rate-limited');
  }
  await noSavedCredential();
  assert.deepEqual(pageErrors, []);
  assert.deepEqual(foreign, []);
  const expected = config.mode === 'expiry_after_connect' ? '401' : '429';
  assert.ok(errors.every(error => error.includes('Failed to load resource') && error.includes(expected)), errors.join('\n'));
  emit({result: 'passed', mode: config.mode, submissions: posts.length,
    operation: await page.locator('#operation-id').textContent(),
    calls: calls.length, statuses: responses, pageErrors: pageErrors.length, foreignRequests: foreign.length});
} catch (error) {
  await shot('failure').catch(() => {});
  console.error(JSON.stringify({event: 'browser_failure', errors, pageErrors, calls, statuses: responses,
    notice: await page.locator('#notice').textContent().catch(() => null)}));
  throw error;
} finally {
  await browser.close();
  input.close();
  process.stdin.destroy();
}
