// Browser drives only public UI. Python checks actual provider/commit observations.
import assert from 'node:assert/strict';
import {createRequire} from 'node:module';
import {mkdir} from 'node:fs/promises';
import path from 'node:path';

let input = '';
for await (const chunk of process.stdin) input += chunk;
const config = JSON.parse(input);
input = '';
const base = new URL(config.url);
assert.equal(base.hostname, '127.0.0.1');
assert.equal(base.protocol, 'http:');
assert.ok(['usage_unknown', 'quota_exhausted'].includes(config.error));
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
  if (request.method() === 'POST' && url.pathname.endsWith('/cancel')) cancellations.push(url.pathname);
});
await context.route('**/*', route => new URL(route.request().url()).origin === base.origin
  ? route.continue() : route.abort());
await mkdir(config.screenshots, {recursive: true});
const shot = name => page.screenshot({path: path.join(config.screenshots, name + '.png'), fullPage: true});
const emit = value => console.log(JSON.stringify(value));

async function connect() {
  await page.getByLabel('Access token', {exact: true}).fill(config.token);
  await page.getByRole('button', {name: 'Connect', exact: true}).click();
  await page.waitForFunction(() => document.getElementById('notice').textContent.startsWith('Connected.'), null, {timeout: 35000});
  assert.equal(await page.locator('#credential').inputValue(), '');
}

async function failed(operation = null) {
  await page.waitForFunction(expected => {
    const id = document.getElementById('operation-id').textContent;
    return /^[a-f0-9]{64}$/.test(id) && (expected === null || id === expected)
      && document.getElementById('operation-state').textContent.startsWith('The task failed.');
  }, operation, {timeout: 115000});
  const usage = await page.locator('#operation-usage').textContent();
  assert.ok(usage.includes(`Recorded error: ${config.error}.`), usage);
  assert.ok(usage.includes(`Usage ${config.known ? 'known' : 'unknown'}; accounting ${config.accounting}.`), usage);
  if (config.known) {
    assert.ok(usage.startsWith('Reported tokens:'), usage);
  } else {
    assert.ok(usage.startsWith('Token totals are unknown.'), usage);
    assert.ok(!usage.includes('Reported tokens:'), usage);
    if (config.reportedSubtotal) {
      const [input, output] = config.reportedSubtotal;
      assert.ok(usage.includes(`Reported subtotal: ${input} in / ${output} out (not a final total).`), usage);
    } else {
      assert.ok(!usage.includes('0 in') && !usage.includes('0 out') && !usage.includes('subtotal'), usage);
    }
  }
  assert.equal(await page.locator('#final-answer').textContent(), '');
  assert.equal(await page.getByRole('button', {name: 'Request cancellation', exact: true}).isVisible(), false);
  assert.equal(await page.getByRole('button', {name: 'Retry the same submission', exact: true}).isVisible(), false);
  return page.locator('#operation-id').textContent();
}

try {
  // Before interactions, verify actual page, controls, errors and screenshot.
  assert.equal((await page.goto(base.href, {waitUntil: 'networkidle', timeout: 35000})).status(), 200);
  await page.getByRole('button', {name: 'Connect', exact: true}).waitFor();
  assert.ok((await page.locator('body').innerText()).includes('Your workspace, in context.'));
  assert.equal(await page.locator('[data-nextjs-dialog], .vite-error-overlay, #webpack-dev-server-client-overlay').count(), 0);
  assert.deepEqual(errors, []);
  assert.deepEqual(pageErrors, []);
  await shot('01-connect');
  emit({event: 'initial_browser_verification_passed'});
  await connect();
  await page.getByLabel('New conversation name', {exact: true}).fill('accounting-task');
  await page.getByRole('button', {name: 'Start conversation', exact: true}).click();
  assert.equal(posts.length, 0);
  await page.getByLabel('Message', {exact: true}).fill('Read README.md and answer.');
  await page.getByRole('button', {name: 'Send message', exact: true}).click();
  const operation = await failed();
  assert.equal(posts.length, 1);
  await shot('02-accounting-stop');
  await page.reload({waitUntil: 'networkidle'});
  assert.equal(await page.locator('#credential').inputValue(), '');
  assert.equal(await page.locator('#messages').textContent(), '');
  await connect();
  await page.locator('#sessions').getByRole('button', {name: 'accounting-task', exact: true}).click();
  assert.equal(await failed(operation), operation);
  await shot('03-reopened');
  assert.equal(posts.length, 1, 'refresh/reconnect silently resubmitted the stopped turn');
  assert.equal(cancellations.length, 0);
  assert.deepEqual(await page.evaluate(() => ({local: localStorage.length, session: sessionStorage.length})), {local: 0, session: 0});
  assert.deepEqual(await context.cookies(), []);
  assert.deepEqual(errors, []);
  assert.deepEqual(pageErrors, []);
  assert.deepEqual(foreign, []);
  emit({result: 'passed', operation, submissions: posts.length, cancellations: cancellations.length,
    pageErrors: pageErrors.length, foreignRequests: foreign.length});
} catch (error) {
  await shot('failure').catch(() => {});
  console.error(JSON.stringify({event: 'browser_failure', errors, pageErrors, submissions: posts.length,
    notice: await page.locator('#notice').textContent().catch(() => null),
    operationState: await page.locator('#operation-state').textContent().catch(() => null)}));
  throw error;
} finally {
  await browser.close();
}
