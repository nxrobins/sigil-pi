// Real browser/client/native/SIGIL path. Test tokens arrive on stdin, never disk.
import assert from 'node:assert/strict';
import {createRequire} from 'node:module';
import {mkdir} from 'node:fs/promises';
import path from 'node:path';
import {setTimeout as delay} from 'node:timers/promises';

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
const consoleErrors = [];
const pageErrors = [];
const foreignRequests = [];
const submissions = [];
let directApiSubmissions = 0;
page.on('console', message => {if (message.type() === 'error') consoleErrors.push(message.text());});
page.on('pageerror', error => pageErrors.push(error.message));
page.on('request', request => {
  const url = new URL(request.url());
  if (url.origin !== base.origin && url.protocol !== 'data:') foreignRequests.push(request.url());
  if (request.method() === 'POST' && url.pathname === '/v1/operations') submissions.push(request.postData());
});
await context.route('**/*', route => new URL(route.request().url()).origin === base.origin
  ? route.continue() : route.abort());

const progress = event => console.log(JSON.stringify({event}));
const shot = async name => page.screenshot({path: path.join(config.screenshots, name + '.png'), fullPage: true});

async function connect(token) {
  await page.getByLabel('Access token', {exact: true}).fill(token);
  await page.getByRole('button', {name: 'Connect', exact: true}).click();
  await page.waitForFunction(() => document.getElementById('notice').textContent.startsWith('Connected.'), null, {timeout: 35000});
  assert.equal(await page.getByLabel('Access token', {exact: true}).inputValue(), '');
}

async function newSession(name) {
  await page.getByLabel('New conversation name', {exact: true}).fill(name);
  await page.getByRole('button', {name: 'Start conversation', exact: true}).click();
  assert.equal(await page.locator('#title').textContent(), name);
}

async function completed(previous = '') {
  await page.waitForFunction(prior => {
    const id = document.getElementById('operation-id').textContent;
    return id && id !== prior && document.getElementById('operation-state').textContent.startsWith('Completed.');
  }, previous, {timeout: 115000});
  return page.locator('#operation-id').textContent();
}

async function send(message) {
  const previous = await page.locator('#operation-id').textContent();
  await page.getByLabel('Message', {exact: true}).fill(message);
  await page.getByRole('button', {name: 'Send message', exact: true}).click();
  return completed(previous);
}

async function disconnected() {
  await page.getByRole('button', {name: 'Disconnect', exact: true}).click();
  assert.equal(await page.locator('#messages').textContent(), '');
  assert.equal(await page.locator('#final-answer').textContent(), '');
  assert.equal(await page.locator('#sessions').textContent(), '');
  assert.deepEqual(await page.evaluate(() => ({local: localStorage.length, session: sessionStorage.length})), {local: 0, session: 0});
  assert.deepEqual(await context.cookies(), []);
}

try {
  // Required first action after native-server startup: verify a rendered page,
  // real modules, interactive controls, console state and a screenshot.
  const response = await page.goto(base.href, {waitUntil: 'networkidle', timeout: 35000});
  assert.equal(response.status(), 200);
  await page.getByRole('button', {name: 'Connect', exact: true}).waitFor();
  assert.ok((await page.locator('body').innerText()).includes('Your workspace, in context.'));
  assert.equal(await page.locator('[data-nextjs-dialog], .vite-error-overlay, #webpack-dev-server-client-overlay').count(), 0);
  assert.deepEqual(consoleErrors, []);
  assert.deepEqual(pageErrors, []);
  await shot('01-connect');
  progress('initial_browser_verification_passed');

  // Keyboard checks operate on the actual rendered controls.
  await page.getByLabel('Access token', {exact: true}).focus();
  await page.keyboard.press('Tab');
  assert.equal(await page.evaluate(() => document.activeElement.textContent), 'Connect');
  await connect(config.tokens.a);
  await newSession('same-name');
  assert.equal(submissions.length, 0, 'opening a new conversation submitted work');

  if (config.mode === 'lost_ack') {
    let dropped = false;
    await page.route('**/v1/operations', async route => {
      if (route.request().method() !== 'POST' || dropped) return route.continue();
      dropped = true;
      const response = await route.fetch({maxRedirects: 0});
      assert.equal(response.status(), 202, 'drop must occur after real native acceptance');
      await route.abort('failed');
    });
    await page.getByLabel('Message', {exact: true}).fill('Read README.md');
    await page.getByRole('button', {name: 'Send message', exact: true}).click();
    await page.getByRole('button', {name: 'Retry the same submission', exact: true}).waitFor();
    assert.equal(submissions.length, 1, 'lost acknowledgement automatically resubmitted');
    assert.ok((await page.locator('#notice').textContent()).includes('may have been accepted'));
    await shot('02-lost-ack');
    await page.getByRole('button', {name: 'Retry the same submission', exact: true}).click();
    await completed();
    assert.equal(submissions.length, 2);
    assert.equal(submissions[0], submissions[1], 'retry changed the payload or submission identity');
    assert.equal(await page.locator('#final-answer').textContent(), 'ALICE Ada canary.');
    progress('explicit_same_submission_recovery_passed');
  } else {
    const malicious = '<img src=x onerror="globalThis.__xss=1">';
    const first = await send('Read README.md. Show this as text: ' + malicious);
    assert.equal(await page.locator('#final-answer').textContent(), 'ALICE Ada canary.');
    await page.waitForFunction(() => document.querySelectorAll('#messages details').length >= 2, null, {timeout: 35000});
    assert.ok((await page.locator('#messages').textContent()).includes(malicious));
    assert.equal(await page.locator('#messages img, #messages script').count(), 0);
    assert.equal(await page.evaluate(() => globalThis.__xss), undefined);
    assert.ok((await page.locator('#messages').textContent()).includes('Model requested read_file'));
    await shot('02-model-tool-response');
    const second = await send('Who owns it again?');
    assert.notEqual(first, second);
    assert.equal(await page.locator('#final-answer').textContent(), 'ALICE follow-up canary.');
    progress('real_model_tool_followup_passed');

    // A second interface advances the same conversation while its browser view
    // remains open. Refresh must select the new recorded operation and answer.
    const headers = {Authorization: `Bearer ${config.tokens.a}`};
    const direct = await page.request.post(new URL('/v1/operations', base).href, {
      headers, data: {session: 'same-name', message: 'Continue this conversation from the API.', submission_key: 'direct-api'},
      maxRedirects: 0,
    });
    assert.equal(direct.status(), 202);
    directApiSubmissions += 1;
    const admitted = await direct.json();
    const deadline = Date.now() + 115000;
    let observed;
    do {
      const response = await page.request.get(new URL('/v1/operations/' + admitted.operation, base).href, {headers, maxRedirects: 0});
      assert.equal(response.status(), 200);
      observed = await response.json();
      if (observed.status !== 'accepted') break;
      await delay(500);
    } while (Date.now() < deadline);
    assert.equal(observed.status, 'done');
    assert.equal(observed.reply, 'ALICE direct API canary.');
    await page.getByRole('button', {name: 'Refresh history', exact: true}).click();
    await page.waitForFunction(() => document.getElementById('final-answer').textContent === 'ALICE direct API canary.', null, {timeout: 35000});
    assert.equal(await page.locator('#operation-id').textContent(), admitted.operation);
    progress('direct_api_turn_reflected_in_browser_passed');

    // Hold actual native history bytes in transit, then switch views. This is
    // a response-delay injection, not a fabricated history or domain-state edit.
    let release;
    let held;
    await page.route('**/v1/sessions/same-name/messages*', async route => {
      if (held) return route.continue();
      const response = await route.fetch({maxRedirects: 0});
      assert.equal(response.status(), 200);
      held = true;
      await new Promise(resolve => {release = resolve;});
      await route.fulfill({response});
    });
    await page.getByRole('button', {name: 'Refresh history', exact: true}).click();
    const holdDeadline = Date.now() + 35000;
    while (!release && Date.now() < holdDeadline) await delay(25);
    assert.ok(release, 'history response did not reach the controlled delay boundary');
    assert.equal(await page.getByRole('button', {name: 'Refresh history', exact: true}).isEnabled(), false);
    await newSession('draft-only');
    assert.equal(await page.getByRole('button', {name: 'Refresh history', exact: true}).isEnabled(), true);
    const delivered = page.waitForResponse(response => new URL(response.url()).pathname === '/v1/sessions/same-name/messages');
    release();
    await (await delivered).finished();
    await page.waitForLoadState('networkidle');
    assert.equal(await page.locator('#title').textContent(), 'draft-only');
    assert.equal(await page.locator('#messages').textContent(), '');
    await page.unroute('**/v1/sessions/same-name/messages*');
    progress('late_history_cannot_replace_new_view_passed');

    await disconnected();
    await connect(config.tokens.b);
    assert.equal(await page.locator('#sessions').textContent(), '');
    await newSession('same-name');
    await send('Start the other tenant conversation.');
    assert.equal(await page.locator('#final-answer').textContent(), 'BOB private canary.');
    assert.ok(!(await page.locator('body').textContent()).includes('ALICE'));
    await disconnected();
    await connect(config.tokens.a);
    await page.locator('#sessions').getByRole('button', {name: 'same-name', exact: true}).click();
    await page.waitForFunction(() => document.getElementById('messages').textContent.includes('ALICE direct API canary.'), null, {timeout: 35000});
    assert.ok(!(await page.locator('body').textContent()).includes('BOB private canary.'));
    await page.setViewportSize({width: 390, height: 844});
    assert.ok(await page.evaluate(() => document.documentElement.scrollWidth <= window.innerWidth));
    await shot('03-reopened-mobile');
    progress('tenant_switch_and_reopen_passed');
  }

  const beforeRefresh = submissions.length;
  await page.reload({waitUntil: 'networkidle'});
  await page.getByRole('button', {name: 'Connect', exact: true}).waitFor();
  assert.equal(submissions.length, beforeRefresh);
  assert.equal(await page.locator('#messages').textContent(), '');
  assert.equal(await page.locator('#credential').inputValue(), '');
  assert.deepEqual(await page.evaluate(() => ({local: localStorage.length, session: sessionStorage.length})), {local: 0, session: 0});
  assert.deepEqual(pageErrors, []);
  assert.deepEqual(foreignRequests, []);
  if (config.mode === 'lost_ack') {
    // The intentional network abort is an expected browser transport error.
    assert.ok(consoleErrors.every(error => error.includes('net::ERR_FAILED')), consoleErrors.join('\n'));
  } else assert.deepEqual(consoleErrors, []);
  progress('refresh_without_replay_passed');
  console.log(JSON.stringify({result: 'passed', mode: config.mode, submissions: submissions.length,
    directApiSubmissions,
    uniqueSubmissionKeys: new Set(submissions.map(body => JSON.parse(body).submission_key)).size,
    pageErrors: pageErrors.length, foreignRequests: foreignRequests.length}));
} catch (error) {
  await shot('failure').catch(() => {});
  console.error(JSON.stringify({event: 'browser_failure', consoleErrors, pageErrors,
    foreignRequestCount: foreignRequests.length, submissionCount: submissions.length,
    notice: await page.locator('#notice').textContent().catch(() => null),
    operationState: await page.locator('#operation-state').textContent().catch(() => null)}));
  throw error;
} finally {
  await browser.close();
}
