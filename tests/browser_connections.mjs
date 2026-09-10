// Hold actual server bytes at the browser transport boundary, never forge them.
import assert from 'node:assert/strict';
import {createRequire} from 'node:module';
import {mkdir} from 'node:fs/promises';
import path from 'node:path';
import {setTimeout as delay} from 'node:timers/promises';

let input = '';
for await (const chunk of process.stdin) input += chunk;
const config = JSON.parse(input);
input = '';
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
let directApiSubmissions = 0, heldActualResponses = 0;
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

async function createFixtureTurn(owner) {
  const headers = {Authorization: 'Bearer ' + config.tokens[owner]};
  const accepted = await context.request.post(new URL('/v1/operations', base).href, {headers,
    data: {session: 'same-name', message: 'Answer for this authorized tenant.', submission_key: 'connection-' + owner}, timeout: 35000});
  directApiSubmissions++;
  assert.equal(accepted.status(), 202);
  const {operation} = await accepted.json();
  assert.match(operation, /^[a-f0-9]{64}$/);
  const until = Date.now() + 115000;
  while (Date.now() < until) {
    const reply = await context.request.get(new URL('/v1/operations/' + operation, base).href, {headers, timeout: 35000});
    assert.equal(reply.status(), 200);
    const value = await reply.json();
    if (value.status !== 'accepted') {
      assert.equal(value.status, 'done');
      assert.equal(value.reply, config.canaries[owner]);
      return operation;
    }
    await delay(250);
  }
  throw new Error('fixture operation did not terminate in the observation window');
}

async function connect(owner) {
  await page.getByLabel('Access token', {exact: true}).fill(config.tokens[owner]);
  await page.getByRole('button', {name: 'Connect', exact: true}).click();
  await page.waitForFunction(() => document.getElementById('notice').textContent.startsWith('Connected.'), null, {timeout: 35000});
  assert.equal(await page.locator('#credential').inputValue(), '');
}

let releaseOld = () => {};
try {
  assert.equal((await page.goto(base.href, {waitUntil: 'networkidle', timeout: 35000})).status(), 200);
  await page.getByRole('button', {name: 'Connect', exact: true}).waitFor();
  assert.ok((await page.locator('body').innerText()).includes('Your workspace, in context.'));
  assert.equal(await page.locator('[data-nextjs-dialog], .vite-error-overlay, #webpack-dev-server-client-overlay').count(), 0);
  assert.deepEqual(errors, []);
  assert.deepEqual(pageErrors, []);
  await shot('01-connect');
  emit({event: 'initial_browser_verification_passed'});
  // These are ordinary authenticated API turns through the same SIGIL service.
  const operations = {a: await createFixtureTurn('a'), b: await createFixtureTurn('b')};
  let markCaptured, finishRoute;
  const captured = new Promise(resolve => {markCaptured = resolve;});
  const finished = new Promise(resolve => {finishRoute = resolve;});
  const release = new Promise(resolve => {releaseOld = resolve;});
  let hold = true;
  let routeFailure = null;
  await page.route('**/v1/sessions/same-name/messages?*', async route => {
    if (!hold || route.request().headers().authorization !== 'Bearer ' + config.tokens.a) return route.continue();
    hold = false;
    try {
      const response = await route.fetch();
      assert.equal(response.status(), 200);
      const actual = await response.json();
      assert.equal(actual.operation, operations.a);
      assert.ok(JSON.stringify(actual).includes(config.canaries.a));
      assert.equal(JSON.stringify(actual).includes(config.canaries.b), false);
      heldActualResponses++;
      markCaptured();
      await release;
      await route.fulfill({response});
    } catch (error) {
      // Disconnect legitimately aborts its outstanding read. No other route
      // failure is accepted as evidence that stale data was discarded.
      if (route.request().failure()?.errorText !== 'net::ERR_ABORTED') routeFailure = error;
    } finally {finishRoute();}
  });
  await connect('a');
  await page.locator('#sessions').getByRole('button', {name: 'same-name', exact: true}).click();
  await Promise.race([captured, finished.then(() => {throw routeFailure || new Error('old response was not captured');})]);
  assert.equal(heldActualResponses, 1);
  await page.getByRole('button', {name: 'Disconnect', exact: true}).click();
  assert.equal(await page.locator('#messages').textContent(), '');
  assert.equal(await page.locator('#final-answer').textContent(), '');
  await connect('b');
  await page.locator('#sessions').getByRole('button', {name: 'same-name', exact: true}).click();
  await page.waitForFunction(({operation, answer}) => document.getElementById('operation-id').textContent === operation
    && document.getElementById('final-answer').textContent === answer,
  {operation: operations.b, answer: config.canaries.b}, {timeout: 35000});
  await page.waitForFunction(() => !document.getElementById('refresh-history').disabled);
  const currentHistory = await page.locator('#messages').textContent();
  assert.ok(currentHistory.includes(config.canaries.b));
  assert.equal(currentHistory.includes(config.canaries.a), false);
  await shot('02-new-connection');
  releaseOld();
  await finished;
  if (routeFailure) throw routeFailure;
  // Wait for browser rendering without injecting any application state.
  await page.evaluate(() => new Promise(resolve => requestAnimationFrame(() => requestAnimationFrame(resolve))));
  assert.equal(await page.locator('#messages').textContent(), currentHistory);
  assert.equal(await page.locator('#operation-id').textContent(), operations.b);
  assert.equal(await page.locator('#final-answer').textContent(), config.canaries.b);
  assert.equal((await page.locator('body').innerText()).includes(config.canaries.a), false);
  await shot('03-old-response-released');
  assert.equal(posts.length, 0);
  assert.equal(cancellations.length, 0);
  assert.deepEqual(await page.evaluate(() => ({local: localStorage.length, session: sessionStorage.length})), {local: 0, session: 0});
  assert.deepEqual(await context.cookies(), []);
  assert.deepEqual(pageErrors, []);
  assert.deepEqual(foreign, []);
  assert.ok(errors.every(error => error.includes('Failed to load resource') && error.includes('net::ERR_ABORTED')), errors.join('\n'));
  emit({result: 'passed', operations, directApiSubmissions, heldActualResponses, submissions: posts.length,
    cancellations: cancellations.length, pageErrors: pageErrors.length, foreignRequests: foreign.length});
} catch (error) {
  await shot('failure').catch(() => {});
  console.error(JSON.stringify({event: 'browser_failure', errors, pageErrors, heldActualResponses,
    notice: await page.locator('#notice').textContent().catch(() => null)}));
  throw error;
} finally {
  releaseOld();
  await browser.close();
}
