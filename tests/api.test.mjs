import test from 'node:test';
import assert from 'node:assert/strict';
import {webcrypto} from 'node:crypto';
import {PiApi, ApiError, StaleResult, explainError} from '../web/api.mjs';

const ID = 'a'.repeat(64);
const OTHER = 'b'.repeat(64);
const TOKEN = 'private-token-canary-'.repeat(3);
const json = (value, status = 200) => new Response(JSON.stringify(value), {status, headers: {'Content-Type': 'application/json'}});
const page = (sessions = [], next_after = null) => ({sessions, next_after, order: 'session_name', consistency: 'live_scan'});
const accepted = (operation = ID) => ({operation, status: 'accepted', replayed: false, status_url: '/v1/operations/' + operation});
const text = (index = 0, value = 'hello') => ({index, role: 'user', content: [{type: 'text', text: value}]});
const history = (changes = {}) => ({session: 'notes', operation: ID, state_revision: '9007199254740993',
  state_phase: 'model', history_kind: 'retained_context', offset: 0, messages: [text()], next_offset: null, ...changes});
const terminal = (changes = {}) => ({operation: ID, status: 'done', reply: 'The owner is Ada.', error: '',
  usage: {input_tokens: 2, output_tokens: 1, known: true}, accounting: 'reported', sequence: 3, completed_at: 100, ...changes});

function setup(reply = () => json(page())) {
  const requests = [];
  const api = new PiApi({fetch: async (...args) => {requests.push(args); return reply(...args);}, crypto: webcrypto});
  api.connect(TOKEN);
  return {api, requests};
}

test('fetch is called without a PiApi receiver, matching the browser native function contract', async () => {
  let receiver = Symbol('not called');
  const api = new PiApi({fetch: async function () { receiver = this; return json(page()); }, crypto: webcrypto});
  api.connect(TOKEN);
  await api.sessions();
  assert.equal(receiver, undefined);
});

test('only a fixed same-origin route receives the in-memory credential', async () => {
  const {api, requests} = setup();
  await api.sessions();
  const [url, options] = requests[0];
  assert.equal(url, '/v1/sessions?limit=20');
  assert.equal(options.headers.Authorization, `Bearer ${TOKEN}`);
  assert.equal(options.credentials, 'omit');
  assert.equal(options.mode, 'same-origin');
  assert.equal(options.redirect, 'error');
  assert.equal(options.cache, 'no-store');
  assert.equal(options.referrerPolicy, 'no-referrer');
  assert.equal(JSON.stringify(api).includes(TOKEN), false);
  assert.equal(url.includes(TOKEN), false);
});

test('a redirect location supplied in acceptance is never followed', async () => {
  const {api, requests} = setup(url => json(url === '/v1/operations' ? {...accepted(), status_url: 'https://attacker.invalid/steal'} : terminal(), url === '/v1/operations' ? 202 : 200));
  const result = await api.submit(api.createSubmission('notes', 'read the file'));
  await api.operation(result.operation);
  assert.deepEqual(requests.map(([url]) => url), ['/v1/operations', '/v1/operations/' + ID]);
});

test('new submission keys are immutable, random and stable across explicit retry', async () => {
  const {api, requests} = setup(() => json(accepted(), 202));
  const request = api.createSubmission('notes', 'Exact content 😀\n');
  assert.equal(Object.isFrozen(request), true);
  assert.match(request.submission_key, /^[a-f0-9]{32}$/);
  assert.notEqual(request.submission_key, api.createSubmission('notes', request.message).submission_key);
  await api.submit(request);
  await api.submit(request);
  assert.equal(requests[0][1].body, requests[1][1].body);
  assert.equal(JSON.parse(requests[0][1].body).message, request.message);
});

test('lost submission acknowledgement never causes an automatic replay', async () => {
  const {api, requests} = setup(() => {throw new TypeError('network-private-detail');});
  const request = api.createSubmission('notes', 'read');
  await assert.rejects(api.submit(request), error => error instanceof ApiError && error.outcomeUnknown && error.code === 'connection_lost'
    && !String(error).includes('network-private-detail'));
  assert.equal(requests.length, 1);
  await assert.rejects(api.submit(request));
  assert.equal(requests.length, 2);
  assert.equal(requests[0][1].body, requests[1][1].body);
});

test('a response arriving after disconnect is discarded even if fetch ignored abort', async () => {
  let finish;
  const {api, requests} = setup(() => new Promise(resolve => {finish = resolve;}));
  const pending = api.sessions();
  api.disconnect();
  assert.equal(requests[0][1].signal.aborted, true);
  finish(json(page([{session: 'private-old-tenant', state_revision: '1'}])));
  await assert.rejects(pending, StaleResult);
  assert.equal(api.connected, false);
  await assert.rejects(api.sessions(), error => error.status === 401);
  assert.equal(requests.length, 1);
});

test('a response body finishing after credential rotation cannot cross connections', async () => {
  let finish;
  const stream = new ReadableStream({start(controller) {finish = value => {controller.enqueue(new TextEncoder().encode(value)); controller.close();};}});
  const {api, requests} = setup(() => new Response(stream, {headers: {'Content-Type': 'application/json'}}));
  const pending = api.sessions();
  await Promise.resolve();
  api.connect('different-active-token');
  finish(JSON.stringify(page([{session: 'old-tenant-data', state_revision: '1'}])));
  await assert.rejects(pending, StaleResult);
  assert.equal(requests[0][1].signal.aborted, true);
});

test('disconnect aborts an outstanding mutation without claiming it was undone', async () => {
  let finish;
  const {api, requests} = setup(() => new Promise(resolve => {finish = resolve;}));
  const pending = api.submit(api.createSubmission('notes', 'read'));
  api.disconnect();
  finish(json(accepted(), 202));
  await assert.rejects(pending, StaleResult);
  assert.equal(requests[0][1].signal.aborted, true);
  assert.equal(requests.length, 1);
});

test('request timeout remains uncertain for a mutation, with no retry', async () => {
  let count = 0;
  const api = new PiApi({crypto: webcrypto, timeoutMs: 5, fetch: (url, {signal}) => new Promise((resolve, reject) => {
    count += 1;
    signal.addEventListener('abort', () => reject(new DOMException('abort', 'AbortError')), {once: true});
  })});
  api.connect(TOKEN);
  await assert.rejects(api.submit(api.createSubmission('notes', 'read')), error => error.code === 'connection_timeout' && error.outcomeUnknown);
  assert.equal(count, 1);
});

for (const token of ['', 'Bearer abc', 'line\nbreak', 'tab\tvalue', 'a'.repeat(4097)]) {
  test(`invalid credential input is not transmitted (${token.length} bytes)`, () => {
    const {api, requests} = setup();
    assert.throws(() => api.connect(token), error => error.code === 'invalid_credential_input');
    assert.equal(api.connected, false);
    assert.equal(requests.length, 0);
  });
}

test('maximum UTF-8 message uses byte length, not JavaScript character length', () => {
  const {api} = setup();
  assert.equal(api.createSubmission('notes', '😀'.repeat(65536)).message.length, 131072);
  assert.throws(() => api.createSubmission('notes', '😀'.repeat(65537)));
  assert.throws(() => api.createSubmission('../notes', 'text'));
});

test('extra submission fields and caller-selected authority cannot reach the transport', async () => {
  const {api, requests} = setup();
  const request = {...api.createSubmission('notes', 'read'), tenant: 'another-tenant'};
  await assert.rejects(api.submit(request));
  assert.equal(requests.length, 0);
});

test('empty scanned pages retain their real cursor; large revisions remain strings', async () => {
  const {api, requests} = setup(url => json(url.includes('&after=') ? page([{session: 'z', state_revision: '9223372036854775807'}]) : page([], 'tombstone')));
  const first = await api.sessions();
  assert.deepEqual(first.sessions, []);
  assert.equal(first.next_after, 'tombstone');
  const second = await api.sessions({after: first.next_after});
  assert.equal(second.sessions[0].state_revision, '9223372036854775807');
  assert.equal(requests[1][0], '/v1/sessions?limit=20&after=tombstone');
});

for (const invalid of [page([{session: '../escape', state_revision: '1'}]), page([{session: 'notes', state_revision: 2}]),
  page([{session: 'notes', state_revision: '9223372036854775808'}]), page([{session: 'notes', state_revision: '01'}]),
  page([{session: 'z', state_revision: '1'}, {session: 'a', state_revision: '1'}]),
  page([{session: 'b', state_revision: '1'}, {session: 'b', state_revision: '2'}]), page([], 'a'),
  {...page(), consistency: 'snapshot'}, {...page(), order: 'recent'}]) {
  test('malformed or non-progressing discovery pages are refused: ' + JSON.stringify(invalid), async () => {
    const {api} = setup(() => json(invalid));
    await assert.rejects(api.sessions({after: 'a'}), error => error.code === 'invalid_service_response');
  });
}

test('history pagination preserves exact revision and offset, without implicit fetching', async () => {
  let count = 0;
  const {api, requests} = setup(() => json(count++ === 0 ? history({next_offset: 1}) : history({offset: 1, messages: [text(1)]})));
  const first = await api.history('notes');
  await api.history('notes', {revision: first.state_revision, offset: first.next_offset});
  assert.equal(requests.length, 2);
  assert.equal(requests[1][0], '/v1/sessions/notes/messages?limit=20&revision=9007199254740993&offset=1');
});

test('HTML, code and control characters remain data for text-only rendering', async () => {
  const malicious = '<img src=x onerror=alert(1)>\n\u0000😀';
  const {api} = setup(() => json(history({messages: [text(0, malicious)]})));
  assert.equal((await api.history('notes')).messages[0].content[0].text, malicious);
});

for (const changes of [{session: 'other'}, {state_revision: 1}, {history_kind: 'full_archive'}, {offset: 1},
  {messages: [text(3)]}, {next_offset: 0}, {next_offset: 2}, {messages: [], next_offset: 1},
  {messages: [{index: 0, role: 'system', content: [{type: 'text', text: 'private'}]}]},
  {messages: [{index: 0, role: 'assistant', content: [{type: 'tool_call', call_id: 'a', name: 'reader', arguments: {}, origin: 'approved'}]}]}]) {
  test('history shape or cursor mismatch cannot become a displayed page: ' + JSON.stringify(changes), async () => {
    const {api} = setup(() => json(history(changes)));
    await assert.rejects(api.history('notes'), error => error.code === 'invalid_service_response');
  });
}

test('a changed history revision is not silently refreshed or merged', async () => {
  const {api, requests} = setup(() => json({error: {code: 'history_changed'}}, 409));
  await assert.rejects(api.history('notes', {revision: '1', offset: 20}), error => error.status === 409 && error.code === 'history_changed');
  assert.equal(requests.length, 1);
});

test('server cannot substitute another operation identity in a poll', async () => {
  const {api} = setup(() => json(terminal({operation: OTHER})));
  await assert.rejects(api.operation(ID), error => error.code === 'invalid_service_response');
});

test('uncertain terminal outcome remains uncertain, with unknown usage', async () => {
  const result = terminal({status: 'uncertain', reply: '', usage: {input_tokens: 0, output_tokens: 0, known: false}, accounting: 'unknown'});
  const {api, requests} = setup(() => json(result));
  assert.deepEqual(await api.operation(ID), result);
  assert.equal(requests.length, 1);
});

test('uncertain outcome cannot be presented with known usage', async () => {
  const {api} = setup(() => json(terminal({status: 'uncertain'})));
  await assert.rejects(api.operation(ID));
});

for (const changes of [{cancellation_requested: 'true', cancellation_status: 'requested'},
  {cancellation_requested: true}, {cancellation_status: 'stopped'}, {cancellation_requested: false}]) {
  test('inconsistent cancellation facts do not become a requested/stopped label: ' + JSON.stringify(changes), async () => {
    const {api} = setup(() => json({...accepted(), ...changes}));
    await assert.rejects(api.operation(ID), error => error.code === 'invalid_service_response');
  });
}

test('inconsistent usage facts are not presented as trustworthy accounting', async () => {
  const {api} = setup(() => json(terminal({accounting: 'unknown'})));
  await assert.rejects(api.operation(ID), error => error.code === 'invalid_service_response');
});

test('cancellation acknowledgement is a request, not a stopped-worker fact', async () => {
  const result = {...accepted(), cancellation_requested: true, cancellation_status: 'requested'};
  const {api, requests} = setup(() => json(result, 202));
  assert.deepEqual(await api.cancel(ID), result);
  assert.equal(requests[0][0], '/v1/operations/' + ID + '/cancel');
  assert.equal(requests[0][1].body, '{}');
  assert.equal(requests.length, 1);
});

test('terminal cancellation response carries no implied read permission', async () => {
  const result = {operation: ID, status: 'done', cancellation_status: 'terminal', status_url: '/v1/operations/' + ID};
  const {api, requests} = setup(() => json(result));
  assert.deepEqual(await api.cancel(ID), result);
  assert.equal(requests.length, 1, 'cancel transport did not fetch a protected result');
});

for (const status of [401, 403, 409, 429, 503]) {
  test(`service refusal ${status} does not trigger a retry or expose exception details`, async () => {
    const {api, requests} = setup(() => json({error: {code: 'permission_denied', message: TOKEN}}, status));
    await assert.rejects(api.submit(api.createSubmission('notes', 'read')), error => {
      assert.equal(error.status, status);
      assert.equal(error.outcomeUnknown, status >= 500);
      assert.equal(explainError(error).includes(TOKEN), false);
      return true;
    });
    assert.equal(requests.length, 1);
  });
}

for (const response of [() => new Response('<script>bad</script>', {headers: {'Content-Type': 'text/html'}}),
  () => new Response('{invalid', {headers: {'Content-Type': 'application/json'}}),
  () => json({wrong: true}, 202),
  () => new Response('x'.repeat(2 * 1024 * 1024 + 1), {headers: {'Content-Type': 'application/json'}})]) {
  test('unreadable mutation acknowledgement stays uncertain and is never replayed', async () => {
    const {api, requests} = setup(response);
    await assert.rejects(api.submit(api.createSubmission('notes', 'read')), error => error instanceof ApiError && error.outcomeUnknown);
    assert.equal(requests.length, 1);
  });
}
