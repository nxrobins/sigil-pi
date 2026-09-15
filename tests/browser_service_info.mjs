// Actual browser fetches use the admitted SIGIL entry, never a request mock.
import assert from 'node:assert/strict';
import {mkdir} from 'node:fs/promises';
import {createRequire} from 'node:module';
import path from 'node:path';

const require = createRequire(import.meta.url);
const {chromium} = require(process.env.PI_PLAYWRIGHT_DIR || 'playwright');
let input = '';
for await (const chunk of process.stdin) input += chunk;
const config = JSON.parse(input);
input = '';
const base = new URL(config.url);
assert.equal(base.hostname, '127.0.0.1');
assert.equal(base.protocol, 'http:');
await mkdir(config.screenshots, {recursive: true});
const browser = await chromium.launch({headless: true});
const context = await browser.newContext({viewport: {width: 1280, height: 900}});
const page = await context.newPage();
const consoleErrors = [], pageErrors = [], foreignRequests = [], failedRequests = [], writes = [];
page.on('console', message => {if (message.type() === 'error') consoleErrors.push(message.text());});
page.on('pageerror', error => pageErrors.push(error.message));
page.on('requestfailed', request => failedRequests.push(request.url()));
page.on('request', request => {
  const url = new URL(request.url());
  if (url.origin !== base.origin && url.protocol !== 'data:') foreignRequests.push(request.url());
  if (request.method() !== 'GET') writes.push(request.url());
});
await context.route('**/*', route => new URL(route.request().url()).origin === base.origin
  ? route.continue() : route.abort());
const shot = name => page.screenshot({path: path.join(config.screenshots, name + '.png'), fullPage: true});

try {
  const loaded = await page.goto(base.href, {waitUntil: 'networkidle', timeout: 35000});
  assert.equal(loaded.status(), 200);
  await page.getByRole('button', {name: 'Connect', exact: true}).waitFor();
  assert.ok((await page.locator('body').innerText()).includes('Your workspace, in context.'));
  assert.equal(await page.locator('[data-nextjs-dialog], .vite-error-overlay, #webpack-dev-server-client-overlay').count(), 0);
  assert.deepEqual(consoleErrors, []);
  assert.deepEqual(pageErrors, []);
  await shot('01-connect');
  console.log(JSON.stringify({event: 'initial_browser_verification_passed'}));

  await page.getByLabel('Access token', {exact: true}).fill(config.tokens.a);
  await page.getByRole('button', {name: 'Connect', exact: true}).click();
  await page.waitForFunction(() => document.getElementById('notice').textContent.startsWith('Connected.'), null, {timeout: 35000});
  assert.equal(await page.getByLabel('Access token', {exact: true}).inputValue(), '');
  const observed = [];
  for (const [name, token] of Object.entries(config.tokens)) {
    for (const pathname of ['/v1/health', '/v1/version']) {
      const result = await page.evaluate(async ({pathname, token, requestId}) => {
        const response = await fetch(pathname, {headers: {
          Authorization: 'Bearer ' + token, 'X-Request-ID': requestId,
        }, cache: 'no-store', redirect: 'error'});
        return {status: response.status, body: await response.json(), headers: {
          requestId: response.headers.get('x-request-id'), cache: response.headers.get('cache-control'),
          challenge: response.headers.get('www-authenticate'),
        }};
      }, {pathname, token, requestId: config.requestId});
      assert.equal(result.headers.requestId, config.requestId);
      assert.equal(result.headers.cache, 'no-store');
      assert.equal(result.body.request_id, config.requestId);
      observed.push({name, pathname, ...result});
    }
  }
  assert.equal(observed.length, 6);
  await shot('02-connected-info');
  await page.getByRole('button', {name: 'Disconnect', exact: true}).click();
  assert.equal(await page.locator('#messages').textContent(), '');
  assert.equal(await page.locator('#sessions').textContent(), '');
  assert.deepEqual(await page.evaluate(() => ({local: localStorage.length, session: sessionStorage.length})), {local: 0, session: 0});
  assert.deepEqual(await context.cookies(), []);
  assert.deepEqual(writes, []);
  assert.deepEqual(foreignRequests, []);
  assert.deepEqual(failedRequests, []);
  assert.deepEqual(pageErrors, []);
  // Only the deliberate four auth/scope HTTP errors are expected; no generic
  // browser error suppression or mocked success responses are introduced.
  assert.equal(consoleErrors.length, 4);
  assert.ok(consoleErrors.every(error => /^Failed to load resource: the server responded with a status of (401|403) \(/.test(error)), consoleErrors.join('\n'));
  console.log(JSON.stringify({result: 'passed', observed, pageErrors: pageErrors.length,
    foreignRequests: foreignRequests.length, failedRequests: failedRequests.length, writes: writes.length}));
} catch (error) {
  await shot('failure').catch(() => {});
  console.error(JSON.stringify({event: 'browser_failure', consoleErrors, pageErrors,
    foreignRequests, failedRequests, writes}));
  throw error;
} finally {
  await browser.close();
}
