// Transport/display-only request-admission checks. No runtime policy or server.
import test from 'node:test';
import assert from 'node:assert/strict';
import {webcrypto} from 'node:crypto';
import {PiApi, ApiError, explainError} from '../web/api.mjs';

const ID = 'a'.repeat(64);
const CANARY = 'undisplayed-response-detail-canary';
function fixture({status = 429, code = 'rate_limited', retry = null} = {}) {
  const requests = [];
  const api = new PiApi({crypto: webcrypto, fetch: async (url, options) => {
    requests.push({url, options});
    return new Response(JSON.stringify({error: {code, message: CANARY}}), {
      status, headers: {'Content-Type': 'application/json', ...(retry === null ? {} : {'Retry-After': retry})},
    });
  }});
  api.connect('inert-browser-rate-test-token');
  return {api, requests};
}

for (const method of ['read', 'submit', 'cancel']) {
  for (const retry of ['1', '29', '60']) {
    test(`${method}: bounded Retry-After is display data and cannot schedule a retry (${retry})`, async () => {
      const {api, requests} = fixture({retry});
      const payload = api.createSubmission('notes', 'Read the permitted file.');
      const invoke = () => method === 'read' ? api.sessions() : method === 'submit' ? api.submit(payload) : api.cancel(ID);
      await assert.rejects(invoke(), error => {
        assert.ok(error instanceof ApiError);
        assert.equal(error.status, 429);
        assert.equal(error.code, 'rate_limited');
        assert.equal(error.retryAfterSeconds, Number(retry));
        assert.equal(error.outcomeUnknown, false);
        const message = explainError(error);
        assert.ok(message.includes(`waiting about ${retry} second`));
        assert.ok(message.includes('Service-side work is not cancelled'));
        assert.ok(message.includes('nothing is retried automatically'));
        assert.ok(!message.includes(CANARY));
        return true;
      });
      assert.equal(requests.length, 1);
      await assert.rejects(invoke(), error => error.status === 429);
      assert.equal(requests.length, 2, 'only the explicit second invocation may retry');
      assert.equal(requests[1].url, requests[0].url);
      const {signal: firstSignal, ...firstOptions} = requests[0].options;
      const {signal: secondSignal, ...secondOptions} = requests[1].options;
      assert.notEqual(firstSignal, secondSignal, 'each invocation retains its own cancellation scope');
      assert.deepEqual(secondOptions, firstOptions, 'explicit retry must preserve the same payload and transport options');
      api.disconnect();
    });
  }
}

for (const retry of [null, '', '0', '00', '01', '61', '100', '-1', '1.5', '1e1', 'Infinity',
                     '1, 2', 'Wed, 21 Oct 2015 07:28:00 GMT', CANARY]) {
  test(`unsupported retry metadata is ignored, never echoed or coerced (${retry})`, async () => {
    const {api, requests} = fixture({retry});
    await assert.rejects(api.sessions(), error => {
      assert.equal(error.retryAfterSeconds, null);
      const message = explainError(error);
      assert.ok(message.includes('request rate limit'));
      assert.ok(!message.includes('waiting about'));
      assert.ok(!message.includes(CANARY));
      return true;
    });
    assert.equal(requests.length, 1);
    api.disconnect();
  });
}

for (const [status, code] of [[401, 'invalid_credential'], [403, 'permission_denied'],
                             [429, 'quota_exceeded'], [503, 'rate_limited'], [503, 'storage_unavailable']]) {
  test(`Retry-After cannot change another error's meaning or mutation uncertainty (${status}/${code})`, async () => {
    const {api, requests} = fixture({status, code, retry: '20'});
    await assert.rejects(api.submit(api.createSubmission('notes', 'read')), error => {
      assert.equal(error.retryAfterSeconds, null);
      assert.equal(error.status, status);
      assert.equal(error.code, code);
      assert.equal(error.outcomeUnknown, status >= 500);
      assert.ok(!explainError(error).includes('waiting about'));
      return true;
    });
    assert.equal(requests.length, 1);
    api.disconnect();
  });
}

test('display helpers do not echo a forged retry value or service error detail', () => {
  for (const retryAfterSeconds of [CANARY, 0, 61, NaN, Infinity, {}, true]) {
    assert.equal(new ApiError(429, 'rate_limited', false, retryAfterSeconds).retryAfterSeconds, null);
    const message = explainError({status: 429, code: 'rate_limited', retryAfterSeconds});
    assert.ok(!message.includes(CANARY) && !message.includes('waiting about'));
  }
  assert.ok(explainError(new ApiError(401, 'authentication_required')).includes('Reconnect'));
  assert.ok(explainError(new ApiError(503, 'clock_unavailable')).includes('nothing is retried automatically'));
});
