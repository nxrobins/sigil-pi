// Focused controller/race tests with a minimal DOM double. Real browser tests
// remain mandatory for rendering, keyboard behavior, CSP and end-to-end behavior.
import test from 'node:test';
import assert from 'node:assert/strict';
import {readFile} from 'node:fs/promises';
import {setImmediate as nextTurn} from 'node:timers/promises';
import {setTimeout as wait} from 'node:timers/promises';

const ID = 'a'.repeat(64);
const NEXT = 'b'.repeat(64);
const json = (value, status = 200) => new Response(JSON.stringify(value), {status, headers: {'Content-Type': 'application/json'}});
const list = () => json({sessions: [{session: 'alpha', state_revision: '1'}], next_after: null, order: 'session_name', consistency: 'live_scan'});
const history = (id = ID, revision = '1', content = 'old transcript') => json({session: 'alpha', operation: id,
  state_revision: revision, state_phase: 'done', history_kind: 'retained_context', offset: 0,
  messages: [{index: 0, role: 'user', content: [{type: 'text', text: content}]}], next_offset: null});
const outcome = (id = ID, reply = 'old answer') => json({operation: id, status: 'done', reply, error: '',
  usage: {input_tokens: 2, output_tokens: 1, known: true}, accounting: 'reported', sequence: 1, completed_at: 100});

const rateRefusal = () => new Response(JSON.stringify({error: {code: 'rate_limited'}}), {
  status: 429, headers: {'Content-Type': 'application/json', 'Retry-After': '1'},
});

class Element {
  children = [];
  events = new Map();
  hidden = false;
  disabled = false;
  value = '';
  attributes = {};
  ownText = '';
  classList = {toggle() {}};
  constructor(tag = 'div') { this.tag = tag; }
  get textContent() { return this.ownText + this.children.map(child => child.textContent).join(''); }
  set textContent(value) { this.ownText = String(value); this.children = []; }
  replaceChildren(...children) { this.ownText = ''; this.children = children; }
  append(...children) { this.children.push(...children); }
  setAttribute(name, value) { this.attributes[name] = value; }
  addEventListener(name, callback) { this.events.set(name, callback); }
  trigger(name) { return this.events.get(name)?.({preventDefault() {}}); }
  focus() {}
  querySelectorAll(tag) {
    return this.children.flatMap(child => [...(child.tag === tag ? [child] : []), ...child.querySelectorAll(tag)]);
  }
}

async function until(condition) {
  for (let i = 0; i < 500; i += 1) {
    if (condition()) return;
    await nextTurn();
  }
  assert.fail('controller did not reach expected state');
}

async function setup(fetch) {
  const markup = await readFile(new URL('../web/index.html', import.meta.url), 'utf8');
  const elements = new Map(Array.from(markup.matchAll(/id="([^"]+)"/g), match => [match[1], new Element()]));
  const window = {events: new Map(), confirm: () => true, addEventListener(name, callback) {this.events.set(name, callback);}};
  const prior = {document: globalThis.document, window: globalThis.window, fetch: globalThis.fetch};
  globalThis.document = {getElementById: id => {assert.ok(elements.has(id), id); return elements.get(id);}, createElement: tag => new Element(tag)};
  globalThis.window = window;
  globalThis.fetch = fetch;
  await import(`../web/app.mjs?test=${crypto.randomUUID()}`);
  const get = id => elements.get(id);
  return {get,
    connect: async () => {get('credential').value = 'test-controller-token'; await get('connect-form').trigger('submit');},
    select: () => get('sessions').querySelectorAll('button')[0].trigger('click'),
    close: async () => {
      window.events.get('pagehide')?.();
      await nextTurn();
      Object.assign(globalThis, prior);
    }};
}

test('switching away from a pending history read restores fresh-view controls and ignores old content', async () => {
  let finish;
  const app = await setup(url => url.startsWith('/v1/sessions?') ? list()
    : new Promise(resolve => {finish = resolve;}));
  try {
    await app.connect();
    app.select();
    await until(() => finish !== undefined);
    assert.equal(app.get('refresh-history').disabled, true);
    app.get('session-name').value = 'new-conversation';
    app.get('new-form').trigger('submit');
    finish(history());
    await nextTurn();
    assert.equal(app.get('title').textContent, 'new-conversation');
    assert.equal(app.get('messages').textContent, '');
    assert.equal(app.get('refresh-history').disabled, false);
  } finally { await app.close(); }
});

test('refreshing history after another API turn selects its operation instead of leaving the old answer', async () => {
  let current = ID;
  const app = await setup(url => {
    if (url.startsWith('/v1/sessions?')) return list();
    if (url.includes('/messages')) return history(current, current === ID ? '1' : '2');
    return outcome(url.endsWith(NEXT) ? NEXT : ID, url.endsWith(NEXT) ? 'new API answer' : 'old answer');
  });
  try {
    await app.connect();
    app.select();
    await until(() => app.get('final-answer').textContent === 'old answer' && !app.get('refresh-history').disabled);
    current = NEXT;
    app.get('refresh-history').trigger('click');
    await until(() => app.get('history-note').textContent.includes('revision 2') && !app.get('refresh-history').disabled);
    await nextTurn();
    assert.equal(app.get('operation-id').textContent, NEXT);
    await until(() => app.get('final-answer').textContent === 'new API answer');
  } finally { await app.close(); }
});

test('an old history snapshot cannot replace a newly accepted local operation', async () => {
  let finishOld;
  let latest = false;
  const app = await setup((url, options) => {
    if (url.startsWith('/v1/sessions?')) return list();
    if (url.includes('/messages')) {
      if (!latest) return new Promise(resolve => {finishOld = resolve;});
      return history(NEXT, '2', 'new transcript');
    }
    if (options.method === 'POST') {
      latest = true;
      return json({operation: NEXT, status: 'accepted', replayed: false}, 202);
    }
    return json({operation: NEXT, status: 'accepted'});
  });
  try {
    await app.connect();
    app.select();
    await until(() => finishOld !== undefined);
    app.get('message').value = 'new request';
    app.get('message-form').trigger('submit');
    await until(() => app.get('operation-id').textContent === NEXT);
    finishOld(history());
    await until(() => !app.get('refresh-history').disabled);
    assert.equal(app.get('operation-id').textContent, NEXT);
    assert.equal(app.get('final-answer').textContent, '');
    assert.ok(!app.get('messages').textContent.includes('old transcript'));
  } finally { if (finishOld) finishOld(history()); await app.close(); }
});

test('late accepted submission after disconnect cannot restore another connection view', async () => {
  let finish;
  const app = await setup((url, options) => options.method === 'POST' ? new Promise(resolve => {finish = resolve;}) : list());
  try {
    await app.connect();
    app.get('session-name').value = 'alpha';
    app.get('new-form').trigger('submit');
    app.get('message').value = 'request';
    app.get('message-form').trigger('submit');
    await until(() => finish !== undefined);
    app.get('disconnect').trigger('click');
    finish(json({operation: ID, status: 'accepted', replayed: false}, 202));
    await nextTurn();
    assert.equal(app.get('operation-panel').hidden, true);
    assert.equal(app.get('operation-id').textContent, '');
    assert.equal(app.get('messages').textContent, '');
    assert.equal(app.get('credential').value, '');
    assert.equal(app.get('connect-form').hidden, false);
  } finally { if (finish) finish(json({operation: ID, status: 'accepted', replayed: false}, 202)); await app.close(); }
});

test('a late cancellation acknowledgement for the previous operation cannot relabel the current one', async () => {
  let current = ID;
  let finishCancel;
  const app = await setup(url => {
    if (url.startsWith('/v1/sessions?')) return list();
    if (url.includes('/messages')) return history(current, current === ID ? '1' : '2');
    if (url.endsWith('/cancel')) return new Promise(resolve => {finishCancel = resolve;});
    return url.endsWith(ID) ? json({operation: ID, status: 'accepted'}) : outcome(NEXT, 'new completed answer');
  });
  try {
    await app.connect();
    app.select();
    await until(() => !app.get('cancel-operation').hidden && !app.get('refresh-history').disabled);
    app.get('cancel-operation').trigger('click');
    await until(() => finishCancel !== undefined);
    current = NEXT;
    app.get('refresh-history').trigger('click');
    await until(() => app.get('final-answer').textContent === 'new completed answer');
    finishCancel(json({operation: ID, status: 'accepted', cancellation_requested: true, cancellation_status: 'requested'}, 202));
    await nextTurn();
    assert.equal(app.get('operation-id').textContent, NEXT);
    assert.ok(app.get('operation-state').textContent.startsWith('Completed.'));
    assert.ok(!app.get('notice').textContent.includes('Cancellation request retained'));
  } finally { if (finishCancel) finishCancel(json({operation: ID, status: 'accepted', cancellation_requested: true, cancellation_status: 'requested'}, 202)); await app.close(); }
});

test('rate-refused submission keeps its exact retry bytes without retrying after the advisory delay', async () => {
  const posts = [];
  const app = await setup((url, options) => {
    if (options.method === 'POST') { posts.push(options.body); return rateRefusal(); }
    return list();
  });
  try {
    await app.connect();
    app.get('session-name').value = 'alpha';
    app.get('new-form').trigger('submit');
    app.get('message').value = 'Keep this exact request.';
    app.get('message-form').trigger('submit');
    await until(() => app.get('notice').textContent.includes('waiting about 1 second'));
    assert.equal(posts.length, 1);
    assert.equal(app.get('retry-submission').hidden, false);
    assert.equal(app.get('send').disabled, true);
    assert.equal(app.get('message').value, 'Keep this exact request.');
    // Exceeds both the displayed 1s advisory and the existing 1.5s poll interval.
    await wait(1800);
    assert.equal(posts.length, 1, 'the hint cannot schedule a mutation retry');
    app.get('retry-submission').trigger('click');
    await until(() => posts.length === 2 && !app.get('retry-submission').hidden);
    assert.equal(posts[1], posts[0]);
    const request = JSON.parse(posts[0]);
    assert.equal(request.message, 'Keep this exact request.');
    assert.match(request.submission_key, /^[a-f0-9]{32}$/);
  } finally { await app.close(); }
});

test('rate refusal stops operation polling and retains the last accepted state for explicit checking', async () => {
  const posts = [], polls = [];
  const app = await setup(async (url, options) => {
    if (options.method === 'POST') {
      posts.push(options.body);
      return json({operation: ID, status: 'accepted', replayed: false}, 202);
    }
    if (url.startsWith('/v1/sessions?')) return list();
    if (url.includes('/messages')) return json({...await history().json(), state_phase: 'model'});
    polls.push(url);
    return rateRefusal();
  });
  try {
    await app.connect();
    app.get('session-name').value = 'alpha';
    app.get('new-form').trigger('submit');
    app.get('message').value = 'An accepted task.';
    app.get('message-form').trigger('submit');
    await until(() => app.get('notice').textContent.includes('waiting about 1 second')
      && !app.get('refresh-history').disabled && !app.get('check-operation').disabled);
    assert.equal(app.get('operation-id').textContent, ID);
    assert.ok(app.get('operation-state').textContent.startsWith('Accepted.'));
    assert.equal(app.get('final-answer').textContent, '');
    assert.equal(app.get('cancel-operation').hidden, true);
    assert.equal(app.get('retry-submission').hidden, true);
    assert.equal(polls.length, 1);
    await wait(1800);
    assert.equal(polls.length, 1, 'rate refusal cannot continue the polling timer');
    assert.equal(posts.length, 1, 'an accepted task is not resubmitted to obtain a result');
    app.get('check-operation').trigger('click');
    await until(() => polls.length === 2 && !app.get('check-operation').disabled);
    assert.equal(posts.length, 1);
    assert.equal(polls[1], polls[0]);
  } finally { await app.close(); }
});

test('credential refusal during an operation read clears the connection and retained view', async () => {
  const app = await setup(url => url.startsWith('/v1/sessions?') ? list()
    : url.includes('/messages') ? history()
      : json({error: {code: 'invalid_credential'}}, 401));
  try {
    await app.connect();
    app.select();
    await until(() => app.get('notice').textContent.includes('did not accept this credential'));
    assert.equal(app.get('connect-form').hidden, false);
    assert.equal(app.get('session-controls').hidden, true);
    assert.equal(app.get('credential').value, '');
    assert.equal(app.get('messages').textContent, '');
    assert.equal(app.get('sessions').textContent, '');
    assert.equal(app.get('operation-id').textContent, '');
    assert.equal(app.get('retry-submission').hidden, true);
  } finally { await app.close(); }
});

for (const scenario of [
  {name: 'known zero totals', input: 0, output: 0, known: true, status: 'done'},
  {name: 'known positive totals', input: 2, output: 1, known: true, status: 'done'},
  {name: 'unknown totals with zero placeholders', input: 0, output: 0, known: false, status: 'failed'},
  {name: 'unknown totals with a reported subtotal', input: 2, output: 1, known: false, status: 'failed'},
  {name: 'uncertain delivery with a reported subtotal', input: 2, output: 1, known: false, status: 'uncertain'},
]) {
  test(`usage display distinguishes ${scenario.name}`, async () => {
    const requests = [];
    const result = {operation: ID, status: scenario.status, reply: scenario.known ? 'A completed answer.' : '',
      error: scenario.known ? '' : scenario.status === 'uncertain' ? 'possibly_delivered' : 'usage_unknown',
      usage: {input_tokens: scenario.input, output_tokens: scenario.output, known: scenario.known},
      accounting: scenario.known ? 'reported' : 'unknown', sequence: 1, completed_at: 100};
    const original = JSON.stringify(result);
    const app = await setup((url, options) => {
      requests.push(options.method);
      if (url.startsWith('/v1/sessions?')) return list();
      if (url.includes('/messages')) return history();
      return json(result);
    });
    try {
      await app.connect();
      app.select();
      await until(() => app.get('operation-usage').textContent !== '');
      const text = app.get('operation-usage').textContent;
      assert.ok(text.includes(`Usage ${scenario.known ? 'known' : 'unknown'}; accounting ${result.accounting}.`));
      if (scenario.known) {
        assert.ok(text.startsWith(`Reported tokens: ${scenario.input} in / ${scenario.output} out.`), text);
        assert.ok(!text.includes('unknown') && !text.includes('subtotal'), text);
      } else {
        assert.ok(text.startsWith('Token totals are unknown.'), text);
        assert.ok(!text.includes('Reported tokens:'), text);
        if (scenario.input || scenario.output) {
          assert.ok(text.includes(`Reported subtotal: ${scenario.input} in / ${scenario.output} out (not a final total).`), text);
        } else {
          assert.ok(!text.includes('0 in') && !text.includes('0 out') && !text.includes('subtotal'), text);
        }
        assert.ok(text.includes(`Recorded error: ${result.error}.`), text);
        assert.equal(app.get('final-answer').textContent, '');
      }
      assert.equal(app.get('operation-id').textContent, ID);
      assert.equal(JSON.stringify(result), original, 'presentation cannot change the recorded result');
      assert.ok(requests.every(method => method === 'GET'), 'presentation cannot submit new work');
    } finally { await app.close(); }
  });
}
