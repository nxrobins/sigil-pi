// Same-origin transport and display-shape checks only. SIGIL owns all decisions.
const SESSION = /^[A-Za-z0-9][A-Za-z0-9_.-]{0,127}$/;
const OPERATION = /^[a-f0-9]{64}$/;
const REVISION = /^[1-9][0-9]{0,18}$/;
const TERMINAL = new Set(['done', 'failed', 'cancelled', 'uncertain']);
const MAX_RESPONSE = 2 * 1024 * 1024;
const object = value => value !== null && typeof value === 'object' && !Array.isArray(value);
const revision = value => typeof value === 'string' && REVISION.test(value) && BigInt(value) <= 9223372036854775807n;
const session = value => typeof value === 'string' && SESSION.test(value);
const operation = value => typeof value === 'string' && OPERATION.test(value);

export class ApiError extends Error {
  constructor(status, code, outcomeUnknown = false, retryAfterSeconds = null) {
    super(code);
    this.name = 'ApiError';
    this.status = status;
    this.code = code;
    this.outcomeUnknown = outcomeUnknown;
    // Display data only: never a timer, permission, expiry, or automatic retry.
    this.retryAfterSeconds = Number.isInteger(retryAfterSeconds) && retryAfterSeconds >= 1
      && retryAfterSeconds <= 60 ? retryAfterSeconds : null;
  }
}

export class StaleResult extends Error {
  constructor() { super('discarded_previous_connection'); this.name = 'StaleResult'; }
}

function requireShape(ok) {
  if (!ok) throw new ApiError(0, 'invalid_service_response');
}

async function boundedJson(response) {
  const type = response.headers.get('content-type')?.split(';')[0].trim().toLowerCase();
  requireShape(type === 'application/json' && response.body !== null);
  const reader = response.body.getReader();
  const decoder = new TextDecoder('utf-8', {fatal: true});
  let text = '';
  let total = 0;
  try {
    while (true) {
      const {done, value} = await reader.read();
      if (done) break;
      total += value.byteLength;
      if (total > MAX_RESPONSE) throw new ApiError(0, 'response_too_large');
      text += decoder.decode(value, {stream: true});
    }
    text += decoder.decode();
    const value = JSON.parse(text);
    requireShape(object(value));
    return value;
  } catch (error) {
    await reader.cancel().catch(() => {});
    throw error;
  } finally {
    reader.releaseLock();
  }
}

function messageShape(message, expected) {
  if (!object(message) || message.index !== expected || !['user', 'assistant', 'tool'].includes(message.role)
      || !Array.isArray(message.content) || message.content.length > 32) return false;
  return message.content.every(block => {
    if (!object(block)) return false;
    if (block.type === 'text') return typeof block.text === 'string';
    if (block.type === 'tool_call') return message.role === 'assistant' && block.origin === 'model_request'
      && typeof block.call_id === 'string' && typeof block.name === 'string' && object(block.arguments);
    if (block.type === 'tool_result') return message.role === 'tool' && typeof block.call_id === 'string'
      && typeof block.text === 'string' && typeof block.is_error === 'boolean';
    return false;
  });
}

export class PiApi {
  #token = '';
  #epoch = 0;
  #pending = new Set();
  #fetch;
  #crypto;
  #timeout;

  constructor({fetch = globalThis.fetch, crypto = globalThis.crypto, timeoutMs = 35000} = {}) {
    this.#fetch = fetch;
    this.#crypto = crypto;
    this.#timeout = timeoutMs;
  }

  get connected() { return this.#token.length > 0; }
  get epoch() { return this.#epoch; }

  connect(token) {
    this.disconnect();
    if (typeof token !== 'string' || token.length < 1 || token.length > 4096 || /[\s\x00-\x1f\x7f]/.test(token)) {
      throw new ApiError(0, 'invalid_credential_input');
    }
    this.#token = token;
  }

  disconnect() {
    this.#epoch += 1;
    this.#token = '';
    for (const controller of this.#pending) controller.abort();
    this.#pending.clear();
  }

  async #request(method, path, payload) {
    if (!this.connected) throw new ApiError(401, 'invalid_credential');
    // Paths are built only by the methods below, never from server status_url.
    const epoch = this.#epoch;
    const controller = new AbortController();
    this.#pending.add(controller);
    const timeout = setTimeout(() => controller.abort(), this.#timeout);
    const mutating = method !== 'GET';
    try {
      // Calling the stored browser function as a class method supplies this
      // PiApi as its receiver; native Window.fetch rejects that invocation.
      const fetch = this.#fetch;
      const response = await fetch(path, {
        method, headers: {'Authorization': `Bearer ${this.#token}`, 'Accept': 'application/json',
          ...(payload === undefined ? {} : {'Content-Type': 'application/json'})},
        ...(payload === undefined ? {} : {body: JSON.stringify(payload)}),
        signal: controller.signal, credentials: 'omit', cache: 'no-store', redirect: 'error',
        referrerPolicy: 'no-referrer', mode: 'same-origin',
      });
      if (epoch !== this.#epoch) throw new StaleResult();
      const value = await boundedJson(response);
      if (epoch !== this.#epoch) throw new StaleResult();
      if (!response.ok) {
        const code = typeof value.error?.code === 'string' && /^[a-z_]{1,80}$/.test(value.error.code)
          ? value.error.code : 'service_error';
        const retry = response.headers.get('retry-after') || '';
        const retryAfterSeconds = response.status === 429 && code === 'rate_limited'
          && /^[1-9][0-9]?$/.test(retry) && Number(retry) <= 60 ? Number(retry) : null;
        throw new ApiError(response.status, code, mutating && response.status >= 500, retryAfterSeconds);
      }
      return {status: response.status, value};
    } catch (error) {
      if (epoch !== this.#epoch) throw new StaleResult();
      if (error instanceof ApiError || error instanceof StaleResult) throw error;
      throw new ApiError(0, controller.signal.aborted ? 'connection_timeout' : 'connection_lost', mutating);
    } finally {
      clearTimeout(timeout);
      controller.abort();
      this.#pending.delete(controller);
    }
  }

  createSubmission(name, message) {
    requireShape(session(name) && typeof message === 'string' && message.length > 0
      && new TextEncoder().encode(message).length <= 262144);
    const bytes = this.#crypto.getRandomValues(new Uint8Array(16));
    const submission_key = Array.from(bytes, value => value.toString(16).padStart(2, '0')).join('');
    return Object.freeze({session: name, message, submission_key});
  }

  async submit(payload) {
    requireShape(object(payload) && Object.keys(payload).sort().join(',') === 'message,session,submission_key'
      && session(payload.session) && typeof payload.message === 'string' && payload.message.length > 0
      && new TextEncoder().encode(payload.message).length <= 262144
      && typeof payload.submission_key === 'string' && /^[a-f0-9]{32}$/.test(payload.submission_key));
    try {
      const {status, value} = await this.#request('POST', '/v1/operations', payload);
      requireShape(status === 202 && operation(value.operation) && value.status === 'accepted'
        && typeof value.replayed === 'boolean');
      return value;
    } catch (error) {
      if (error instanceof ApiError && error.status === 0) error.outcomeUnknown = true;
      throw error;
    }
  }

  async sessions({after = null, limit = 20} = {}) {
    requireShape(Number.isInteger(limit) && limit >= 1 && limit <= 50 && (after === null || session(after)));
    const query = `?limit=${limit}` + (after === null ? '' : `&after=${encodeURIComponent(after)}`);
    const {status, value} = await this.#request('GET', '/v1/sessions' + query);
    requireShape(status === 200 && value.order === 'session_name' && value.consistency === 'live_scan'
      && Array.isArray(value.sessions) && value.sessions.length <= limit
      && (value.next_after === null || session(value.next_after)));
    let previous = after;
    for (const row of value.sessions) {
      requireShape(object(row) && session(row.session) && revision(row.state_revision)
        && (previous === null || row.session > previous));
      previous = row.session;
    }
    requireShape(value.next_after === null || ((after === null || value.next_after > after)
      && (previous === null || value.next_after >= previous)));
    return value;
  }

  async history(name, {revision: expected = null, offset = 0, limit = 20} = {}) {
    requireShape(session(name) && (expected === null || revision(expected))
      && Number.isInteger(offset) && offset >= 0 && offset <= 10000 && (offset === 0 || expected !== null)
      && Number.isInteger(limit) && limit >= 1 && limit <= 50);
    const query = `?limit=${limit}` + (expected === null ? '' : `&revision=${expected}&offset=${offset}`);
    const {status, value} = await this.#request('GET', `/v1/sessions/${encodeURIComponent(name)}/messages${query}`);
    requireShape(status === 200 && value.session === name && operation(value.operation)
      && revision(value.state_revision) && (expected === null || value.state_revision === expected)
      && ['model', 'tool', ...TERMINAL].includes(value.state_phase) && value.history_kind === 'retained_context'
      && value.offset === offset && Array.isArray(value.messages) && value.messages.length <= limit
      && value.messages.every((row, i) => messageShape(row, offset + i))
      && (value.next_offset === null || (Number.isInteger(value.next_offset) && value.messages.length > 0
        && value.next_offset === offset + value.messages.length && value.next_offset <= 10000)));
    return value;
  }

  async operation(id) {
    requireShape(operation(id));
    const {status, value} = await this.#request('GET', `/v1/operations/${id}`);
    requireShape(status === 200 && value.operation === id && (value.status === 'accepted' || TERMINAL.has(value.status)));
    if (value.status === 'accepted') {
      requireShape((value.cancellation_requested === undefined && value.cancellation_status === undefined)
        || (value.cancellation_requested === true && value.cancellation_status === 'requested'));
    } else {
      requireShape(typeof value.reply === 'string' && typeof value.error === 'string' && object(value.usage)
        && typeof value.usage.known === 'boolean' && ['reported', 'unknown', 'overrun'].includes(value.accounting)
        && ['input_tokens', 'output_tokens'].every(key => Number.isSafeInteger(value.usage[key]) && value.usage[key] >= 0)
        && value.usage.known === (value.accounting !== 'unknown')
        && (value.status !== 'uncertain' || !value.usage.known));
    }
    return value;
  }

  async cancel(id) {
    requireShape(operation(id));
    try {
      const {status, value} = await this.#request('POST', `/v1/operations/${id}/cancel`, {});
      requireShape(value.operation === id && ((status === 202 && value.status === 'accepted'
        && value.cancellation_requested === true && value.cancellation_status === 'requested')
        || (status === 200 && TERMINAL.has(value.status) && value.cancellation_status === 'terminal')));
      return value;
    } catch (error) {
      if (error instanceof ApiError && error.status === 0) error.outcomeUnknown = true;
      throw error;
    }
  }
}

export function explainError(error) {
  const hints = {
    invalid_credential_input: 'Paste the access token only, without a Bearer prefix or spaces.',
    invalid_credential: 'The service did not accept this credential. Reconnect with an active access token.',
    authentication_required: 'The service requires an access token. Reconnect with an active credential.',
    permission_denied: 'This credential does not have permission for that action. Ask your operator for access.',
    quota_exceeded: 'The service quota is exhausted. Ask your operator about the allowance before trying again.',
    rate_limited: 'The request rate limit was reached. Wait before trying again. Service-side work is not cancelled; nothing is retried automatically.',
    clock_unavailable: 'The service could not validate its clock. Ask your operator to check service time; nothing is retried automatically.',
    session_busy: 'This conversation has active or unresolved work. Check its state before submitting again.',
    submission_conflict: 'This submission key already belongs to different content. Nothing was automatically retried.',
    submission_expired: 'The retained submission can no longer be replayed. Ask your operator to reconcile it.',
    deadline_exceeded: 'The service deadline was reached. Check the operation outcome before starting new work.',
    history_changed: 'The conversation changed between pages. Refresh its history; pages were not merged.',
    session_not_found: 'The conversation is missing or no longer available to this credential.',
    not_found: 'That item is missing or not visible to this credential.',
    storage_unavailable: 'State could not be read or committed. No empty success was substituted.',
    operation_version_mismatch: 'This operation belongs to another deployment. Ask your operator to reconcile it.',
    connection_timeout: 'The connection timed out. This does not cancel service-side work.',
    connection_lost: 'The connection was lost. This does not cancel service-side work.',
    invalid_service_response: 'The service response did not match the supported interface.',
    response_too_large: 'The service response exceeded the browser transport bound.',
    route_not_migrated: 'This deployment does not yet implement that route.',
  };
  let message = hints[error?.code] || 'The request failed. Check service status or ask your operator.';
  if (error?.status === 429 && error?.code === 'rate_limited' && Number.isInteger(error.retryAfterSeconds)
      && error.retryAfterSeconds >= 1 && error.retryAfterSeconds <= 60) {
    message += ` The service suggests waiting about ${error.retryAfterSeconds} second${error.retryAfterSeconds === 1 ? '' : 's'}, then retrying or checking again explicitly.`;
  }
  return message + (error?.outcomeUnknown ? ' The request may have been accepted; do not start a replacement. Use the explicit same-request retry if available.' : '');
}
