// Actual same-origin browser boundary checks. Test credentials arrive via stdin.
import assert from 'node:assert/strict';
import {createRequire} from 'node:module';
import {mkdir} from 'node:fs/promises';
import {createInterface} from 'node:readline';
import path from 'node:path';

const input = createInterface({input: process.stdin, crlfDelay: Infinity});
const lines = input[Symbol.asyncIterator]();
async function receive() {
  const {value, done} = await lines.next();
  assert.equal(done, false, 'test coordination stream ended early');
  return JSON.parse(value);
}
const config = await receive();
const base = new URL(config.url);
assert.equal(base.hostname, '127.0.0.1');
assert.equal(base.protocol, 'http:');
const require = createRequire(import.meta.url);
const {chromium} = require(process.env.PI_PLAYWRIGHT_DIR || 'playwright');
const browser = await chromium.launch({headless: true});
process.once('SIGTERM', async () => {await browser.close(); process.exit(143);});
const context = await browser.newContext({viewport: {width: 1280, height: 900}});
const page = await context.newPage();
const errors = [], pageErrors = [], foreign = [], posts = [], cancellations = [];
page.on('console', message => {if (message.type() === 'error') errors.push(message.text());});
page.on('pageerror', error => pageErrors.push(error.message));
page.on('request', request => {
  const url = new URL(request.url());
  if (url.origin !== base.origin && url.protocol !== 'data:') foreign.push(url.pathname);
  if (request.method() === 'POST' && url.pathname === '/v1/operations') posts.push(request.postData());
  if (request.method() === 'POST' && /^\/v1\/operations\/[a-f0-9]{64}\/cancel$/.test(url.pathname)) cancellations.push(url.pathname);
});
await context.route('**/*', route => new URL(route.request().url()).origin === base.origin ? route.continue() : route.abort());
await mkdir(config.screenshots, {recursive: true});
const shot = name => page.screenshot({path: path.join(config.screenshots, name + '.png'), fullPage: true});
const emit = value => console.log(JSON.stringify(value));

async function connect(success = true) {
  await page.getByLabel('Access token', {exact: true}).fill(config.token);
  await page.getByRole('button', {name: 'Connect', exact: true}).click();
  await page.waitForFunction(ok => document.getElementById('notice').textContent.includes(
    ok ? 'Connected.' : 'did not accept this credential'), success, {timeout: 35000});
  assert.equal(await page.locator('#credential').inputValue(), '');
}
async function start() {
  await page.getByLabel('New conversation name', {exact: true}).fill('boundary-task');
  await page.getByRole('button', {name: 'Start conversation', exact: true}).click();
  await page.getByLabel('Message', {exact: true}).fill('Read the permitted workspace.');
  await page.getByRole('button', {name: 'Send message', exact: true}).click();
}
async function noSavedCredential() {
  assert.deepEqual(await page.evaluate(() => ({local: localStorage.length, session: sessionStorage.length})), {local: 0, session: 0});
  assert.deepEqual(await context.cookies(), []);
}

let operation = null;
try {
  // First actions after service startup: actual rendering, controls and errors.
  assert.equal((await page.goto(base.href, {waitUntil: 'networkidle'})).status(), 200);
  await page.getByRole('button', {name: 'Connect', exact: true}).waitFor();
  assert.ok((await page.locator('body').innerText()).includes('Your workspace, in context.'));
  assert.equal(await page.locator('[data-nextjs-dialog], .vite-error-overlay, #webpack-dev-server-client-overlay').count(), 0);
  assert.deepEqual(errors, []);
  assert.deepEqual(pageErrors, []);
  await shot('01-connect');
  emit({event: 'initial_browser_verification_passed'});

  if (config.mode === 'unknown_credential' || config.mode === 'expired_credential') {
    await connect(false);
    assert.equal(await page.locator('#connect-form').isVisible(), true);
    assert.equal(await page.locator('#session-controls').isVisible(), false);
    assert.equal(await page.locator('#messages').textContent(), '');
    assert.equal(await page.locator('#sessions').textContent(), '');
    await shot('02-refusal');
    assert.equal(posts.length, 0);
  } else if (config.mode === 'denied_submission') {
    await connect();
    await start();
    await page.waitForFunction(() => document.getElementById('notice').textContent.includes('does not have permission'), null, {timeout: 35000});
    assert.equal(posts.length, 1);
    assert.equal(await page.getByRole('button', {name: 'Retry the same submission', exact: true}).isVisible(), true);
    assert.equal(await page.getByRole('button', {name: 'Send message', exact: true}).isEnabled(), false);
    await shot('02-refusal');
    const dialog = page.waitForEvent('dialog');
    const click = page.getByRole('button', {name: 'Clear local retry', exact: true}).click();
    const confirmation = await dialog;
    assert.equal(confirmation.type(), 'confirm');
    assert.ok(confirmation.message().startsWith('Clear only this page'));
    await confirmation.accept();
    await click;
    assert.equal(await page.getByRole('button', {name: 'Send message', exact: true}).isEnabled(), true);
    assert.equal(posts.length, 1);
  } else {
    assert.equal(config.mode, 'sent_refresh_cancel');
    await connect();
    await start();
    await page.waitForFunction(() => document.getElementById('operation-state').textContent.startsWith('Accepted.'), null, {timeout: 35000});
    operation = await page.locator('#operation-id').textContent();
    emit({event: 'submission_accepted', operation});
    assert.equal((await receive()).event, 'effect_observed');
    await page.reload({waitUntil: 'networkidle'});
    assert.equal(await page.locator('#credential').inputValue(), '');
    assert.equal(await page.locator('#messages').textContent(), '');
    assert.equal(posts.length, 1, 'refresh resubmitted work');
    await noSavedCredential();
    await connect();
    await page.locator('#sessions').getByRole('button', {name: 'boundary-task', exact: true}).click();
    await page.getByRole('button', {name: 'Request cancellation', exact: true}).waitFor();
    assert.equal(await page.locator('#operation-id').textContent(), operation);
    const response = page.waitForResponse(response => response.request().method() === 'POST'
      && new URL(response.url()).pathname === '/v1/operations/' + operation + '/cancel');
    await page.getByRole('button', {name: 'Request cancellation', exact: true}).click();
    const acknowledged = await response;
    assert.equal(acknowledged.status(), 202, 'cancellation must arrive while the operation is active');
    const control = await acknowledged.json();
    assert.equal(control.cancellation_status, 'requested');
    assert.equal(control.cancellation_requested, true);
    await page.waitForFunction(() => document.getElementById('operation-state').textContent.startsWith('Outcome uncertain.'), null, {timeout: 115000});
    assert.ok((await page.locator('#operation-state').textContent()).includes('may have been delivered'));
    assert.equal(await page.getByRole('button', {name: 'Request cancellation', exact: true}).isVisible(), false);
    assert.equal(posts.length, 1);
    assert.equal(cancellations.length, 1);
    const usage = await page.locator('#operation-usage').textContent();
    assert.ok(usage.startsWith('Token totals are unknown.'), usage);
    assert.ok(usage.includes('Usage unknown; accounting unknown.'), usage);
    assert.ok(!usage.includes('Reported tokens:'), usage);
    await shot('03-uncertain');
  }
  await noSavedCredential();
  assert.deepEqual(pageErrors, []);
  assert.deepEqual(foreign, []);
  const expectedStatus = config.mode === 'denied_submission' ? '403' : '401';
  if (config.mode === 'sent_refresh_cancel') assert.deepEqual(errors, []);
  else assert.ok(errors.every(error => error.includes('Failed to load resource') && error.includes(expectedStatus)), errors.join('\n'));
  emit({result: 'passed', mode: config.mode, operation, submissions: posts.length,
    cancellations: cancellations.length, pageErrors: pageErrors.length, foreignRequests: foreign.length});
} catch (error) {
  await shot('failure').catch(() => {});
  console.error(JSON.stringify({event: 'browser_failure', errors, pageErrors, submissions: posts.length,
    notice: await page.locator('#notice').textContent().catch(() => null),
    operationState: await page.locator('#operation-state').textContent().catch(() => null)}));
  throw error;
} finally {
  await browser.close();
  input.close();
  process.stdin.destroy();
}
