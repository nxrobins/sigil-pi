// Real process outage, not a fabricated HTTP response or client state mutation.
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
const errors = [], pageErrors = [], foreign = [], posts = [], cancellations = [], failedReads = [];
page.on('console', message => {if (message.type() === 'error') errors.push(message.text());});
page.on('pageerror', error => pageErrors.push(error.message));
page.on('request', request => {
  const url = new URL(request.url());
  if (url.origin !== base.origin && url.protocol !== 'data:') foreign.push(url.pathname);
  if (request.method() === 'POST' && url.pathname === '/v1/operations') posts.push(request.postData());
  if (request.method() === 'POST' && url.pathname.endsWith('/cancel')) cancellations.push(url.pathname);
});
page.on('requestfailed', request => {
  if (request.method() === 'GET' && new URL(request.url()).pathname.startsWith('/v1/')) {
    failedReads.push({path: new URL(request.url()).pathname, error: request.failure()?.errorText});
  }
});
await context.route('**/*', route => new URL(route.request().url()).origin === base.origin
  ? route.continue() : route.abort());
await mkdir(config.screenshots, {recursive: true});
const shot = name => page.screenshot({path: path.join(config.screenshots, name + '.png'), fullPage: true});
const emit = value => console.log(JSON.stringify(value));

async function completed(expected = null) {
  await page.waitForFunction(operation => {
    const id = document.getElementById('operation-id').textContent;
    return /^[a-f0-9]{64}$/.test(id) && (operation === null || id === operation)
      && document.getElementById('operation-state').textContent.startsWith('Completed.');
  }, expected, {timeout: 115000});
  assert.equal(await page.locator('#final-answer').textContent(), config.answer);
  return page.locator('#operation-id').textContent();
}

async function verifyRenderedPage() {
  assert.ok((await page.locator('body').innerText()).includes('Your workspace, in context.'));
  assert.equal(await page.locator('[data-nextjs-dialog], .vite-error-overlay, #webpack-dev-server-client-overlay').count(), 0);
  assert.deepEqual(pageErrors, []);
}

try {
  assert.equal((await page.goto(base.href, {waitUntil: 'networkidle', timeout: 35000})).status(), 200);
  await page.getByRole('button', {name: 'Connect', exact: true}).waitFor();
  await verifyRenderedPage();
  assert.deepEqual(errors, []);
  await shot('01-connect');
  emit({event: 'initial_browser_verification_passed'});
  await page.getByLabel('Access token', {exact: true}).fill(config.token);
  await page.getByRole('button', {name: 'Connect', exact: true}).click();
  await page.waitForFunction(() => document.getElementById('notice').textContent.startsWith('Connected.'), null, {timeout: 35000});
  await page.getByLabel('New conversation name', {exact: true}).fill('outage-task');
  await page.getByRole('button', {name: 'Start conversation', exact: true}).click();
  assert.equal(posts.length, 0);
  await page.getByLabel('Message', {exact: true}).fill('Answer once.');
  await page.getByRole('button', {name: 'Send message', exact: true}).click();
  const operation = await completed();
  // Ensure terminal-triggered history loading has finished before the outage.
  await page.waitForFunction(() => !document.getElementById('refresh-history').disabled);
  const history = await page.locator('#messages').textContent();
  assert.ok(history.includes(config.answer));
  assert.deepEqual(errors, []);
  emit({event: 'stop_service', operation});
  assert.equal((await receive()).event, 'service_stopped');
  await page.getByRole('button', {name: 'Check status', exact: true}).click();
  await page.waitForFunction(() => document.getElementById('notice').textContent.includes('The connection was lost.'), null, {timeout: 35000});
  assert.ok((await page.locator('#notice').textContent()).includes('does not cancel service-side work'));
  assert.equal(await page.locator('#messages').textContent(), history);
  assert.equal(await page.locator('#final-answer').textContent(), config.answer);
  assert.equal(posts.length, 1);
  assert.ok(failedReads.length >= 1);
  assert.ok(failedReads.every(row => row.error === 'net::ERR_CONNECTION_REFUSED'), JSON.stringify(failedReads));
  await shot('02-outage');
  emit({event: 'reopen_service'});
  assert.equal((await receive()).event, 'service_reopened');
  // First interaction after restart checks the real response and rendered page.
  const response = page.waitForResponse(response => response.request().method() === 'GET'
    && new URL(response.url()).pathname === '/v1/operations/' + operation);
  await page.getByRole('button', {name: 'Check status', exact: true}).click();
  const returned = await response;
  assert.equal(returned.status(), 200);
  assert.equal((await returned.json()).operation, operation);
  await completed(operation);
  await verifyRenderedPage();
  await page.getByRole('button', {name: 'Refresh history', exact: true}).click();
  await page.waitForFunction(() => !document.getElementById('refresh-history').disabled);
  assert.equal(await page.locator('#messages').textContent(), history);
  await shot('03-restored');
  assert.equal(posts.length, 1);
  assert.equal(cancellations.length, 0);
  assert.deepEqual(await page.evaluate(() => ({local: localStorage.length, session: sessionStorage.length})), {local: 0, session: 0});
  assert.deepEqual(await context.cookies(), []);
  assert.deepEqual(pageErrors, []);
  assert.deepEqual(foreign, []);
  assert.ok(errors.every(error => error.includes('Failed to load resource') && error.includes('net::ERR_CONNECTION_REFUSED')), errors.join('\n'));
  emit({result: 'passed', operation, submissions: posts.length, cancellations: cancellations.length,
    pageErrors: pageErrors.length, foreignRequests: foreign.length, failedReads: failedReads.length});
} catch (error) {
  await shot('failure').catch(() => {});
  console.error(JSON.stringify({event: 'browser_failure', errors, pageErrors, failedReads,
    notice: await page.locator('#notice').textContent().catch(() => null)}));
  throw error;
} finally {
  await browser.close();
  input.close();
  process.stdin.destroy();
}
