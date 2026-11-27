import {PiApi, StaleResult, explainError} from './api.mjs';

const api = new PiApi();
const byId = id => document.getElementById(id);
let view = 0;
let selected = '';
let listAfter = null;
let historyAfter = null;
let historyRevision = null;
let heldSubmission = null;
let activeOperation = null;
let pollTimer = null;
let sending = false;
let polling = false;
let cancelling = false;
let listRequest = 0;
let historyRequest = 0;
let operationRequest = 0;
let lastTerminal = null;

function notice(message, error = false) {
  byId('notice').textContent = message;
  byId('notice').classList.toggle('error', error);
}

function stopPolling() { clearTimeout(pollTimer); pollTimer = null; }

function controls() {
  byId('connect-form').hidden = api.connected;
  for (const id of ['disconnect', 'session-controls']) byId(id).hidden = !api.connected;
  byId('message-form').hidden = !api.connected || !selected;
  byId('refresh-history').hidden = !api.connected || !selected;
  byId('send').disabled = sending || heldSubmission !== null;
  byId('message').disabled = sending || heldSubmission !== null;
  byId('retry-submission').hidden = heldSubmission === null || sending;
  byId('clear-submission').hidden = heldSubmission === null || sending;
  byId('cancel-operation').disabled = cancelling;
  byId('check-operation').disabled = polling;
}

function selectOperation(id) {
  stopPolling();
  operationRequest += 1;
  activeOperation = id;
  polling = false;
  cancelling = false;
  lastTerminal = null;
  byId('operation-panel').hidden = false;
  byId('operation-id').textContent = id;
  byId('operation-state').textContent = 'Checking this operation’s recorded state…';
  byId('operation-usage').textContent = '';
  byId('final-answer').textContent = '';
  byId('final-answer').hidden = true;
  byId('cancel-operation').hidden = true;
  controls();
}

function clearView(name = '') {
  view += 1;
  stopPolling();
  selected = name;
  historyAfter = null;
  historyRevision = null;
  activeOperation = null;
  historyRequest += 1;
  operationRequest += 1;
  lastTerminal = null;
  sending = false;
  polling = false;
  cancelling = false;
  heldSubmission = null;
  byId('message').value = '';
  byId('messages').replaceChildren();
  byId('final-answer').textContent = '';
  byId('operation-state').textContent = '';
  byId('operation-id').textContent = '';
  byId('operation-usage').textContent = '';
  byId('refresh-history').disabled = false;
  byId('more-messages').disabled = false;
  for (const id of ['more-messages', 'operation-panel', 'history-note', 'pending-note', 'final-answer', 'cancel-operation']) byId(id).hidden = true;
  byId('title').textContent = name || 'Ready when you are.';
  for (const button of byId('sessions').querySelectorAll('button')) button.setAttribute('aria-current', String(button.textContent === name));
  controls();
}

function disconnect(message = 'Disconnected. This page has cleared its token, messages and pending-request memory. Service-side work is not cancelled.') {
  api.disconnect();
  listRequest += 1;
  clearView();
  byId('sessions').replaceChildren();
  byId('session-name').value = '';
  byId('credential').value = '';
  listAfter = null;
  byId('more-sessions').hidden = true;
  byId('list-empty').hidden = false;
  byId('list-empty').textContent = 'No conversations loaded.';
  notice(message);
}

function report(error, expectedView = view) {
  if (error instanceof StaleResult || expectedView !== view) return;
  if (error.status === 401) disconnect(explainError(error));
  else notice(explainError(error), true);
}

async function loadSessions(append = false) {
  const epoch = api.epoch;
  const request = ++listRequest;
  const after = append ? listAfter : null;
  byId('refresh-list').disabled = true;
  byId('more-sessions').disabled = true;
  try {
    const page = await api.sessions({after});
    if (epoch !== api.epoch || request !== listRequest) return;
    if (!append) byId('sessions').replaceChildren();
    for (const row of page.sessions) {
      const li = document.createElement('li');
      const button = document.createElement('button');
      button.type = 'button';
      button.textContent = row.session;
      button.setAttribute('aria-current', String(row.session === selected));
      button.addEventListener('click', () => selectSession(row.session));
      li.append(button);
      byId('sessions').append(li);
    }
    listAfter = page.next_after;
    byId('more-sessions').hidden = listAfter === null;
    const empty = byId('sessions').children.length === 0;
    byId('list-empty').hidden = !empty;
    byId('list-empty').textContent = empty && listAfter !== null
      ? 'No visible conversations in this scanned page. More names may follow.' : 'No conversations yet. Start one above.';
    return true;
  } catch (error) { if (epoch === api.epoch && request === listRequest) report(error); }
  finally {
    if (epoch === api.epoch && request === listRequest) {
      byId('refresh-list').disabled = false;
      byId('more-sessions').disabled = false;
    }
  }
}

function renderMessage(message) {
  const li = document.createElement('li');
  li.className = `message ${message.role}`;
  const role = document.createElement('p');
  role.className = 'role';
  role.textContent = `${message.index + 1} · ${message.role}`;
  li.append(role);
  for (const block of message.content) {
    if (block.type === 'text') {
      const text = document.createElement('div');
      text.className = 'message-text';
      text.textContent = block.text;
      li.append(text);
    } else {
      const details = document.createElement('details');
      const summary = document.createElement('summary');
      const text = document.createElement('pre');
      summary.textContent = block.type === 'tool_call' ? `Model requested ${block.name} · ${block.call_id}`
        : `Tool result${block.is_error ? ' · error' : ''} · ${block.call_id}`;
      text.textContent = block.type === 'tool_call' ? JSON.stringify(block.arguments, null, 2) : block.text;
      details.append(summary, text);
      li.append(details);
    }
  }
  return li;
}

async function loadHistory(append = false) {
  const current = view;
  const name = selected;
  const offset = append ? historyAfter : 0;
  if (!name || (append && offset === null)) return;
  const request = ++historyRequest;
  byId('refresh-history').disabled = true;
  byId('more-messages').disabled = true;
  try {
    const page = await api.history(name, {revision: append ? historyRevision : null, offset});
    if (current !== view || request !== historyRequest) return;
    if (!append) byId('messages').replaceChildren();
    byId('messages').append(...page.messages.map(renderMessage));
    historyAfter = page.next_offset;
    historyRevision = page.state_revision;
    byId('more-messages').hidden = historyAfter === null;
    byId('history-note').hidden = false;
    byId('history-note').textContent = `Retained context · revision ${page.state_revision} · committed phase ${page.state_phase}. Model requests are not proof of approval or external delivery.`;
    if (activeOperation !== page.operation) {
      selectOperation(page.operation);
      void checkOperation();
    }
  } catch (error) {
    if (current !== view || request !== historyRequest || error instanceof StaleResult) return;
    if (error.code === 'history_changed' || error.code === 'session_not_found') {
      byId('messages').replaceChildren();
      byId('more-messages').hidden = true;
      byId('history-note').hidden = true;
      historyAfter = null;
      historyRevision = null;
    }
    report(error, current);
  } finally {
    if (current === view && request === historyRequest) {
      byId('refresh-history').disabled = false;
      byId('more-messages').disabled = false;
    }
  }
}

function selectSession(name, fresh = false) {
  if ((sending || heldSubmission) && !window.confirm('This submission may already be accepted. Leaving clears its same-request retry memory; it does not cancel work. Leave this view?')) return;
  clearView(name);
  notice(fresh ? 'Write your first message. The conversation is created only when the service accepts it.'
    : 'Loading retained conversation. No task is submitted by opening it.');
  byId('message').focus();
  if (!fresh) void loadHistory();
}

function showOperation(result) {
  byId('operation-panel').hidden = false;
  byId('operation-id').textContent = result.operation;
  const labels = {
    accepted: result.cancellation_requested ? 'Cancellation requested. The service has not yet published a terminal outcome.'
      : 'Accepted. Waiting for a committed outcome; the current worker phase is not exposed here.',
    done: 'Completed. The service has committed this response.',
    failed: 'The task failed. Check the recorded error before deciding what to do next.',
    cancelled: 'Cancelled at the service’s recorded boundary. This does not undo earlier external effects.',
    uncertain: 'Outcome uncertain. An external request may have been delivered. Do not automatically repeat the task; ask your operator to reconcile it.',
  };
  byId('operation-state').textContent = labels[result.status];
  byId('cancel-operation').hidden = result.status !== 'accepted';
  let usage = '';
  if (result.usage) {
    const {input_tokens: input, output_tokens: output, known} = result.usage;
    const counts = `${input} in / ${output} out`;
    const totals = known ? `Reported tokens: ${counts}.`
      : 'Token totals are unknown.' + (input || output ? ` Reported subtotal: ${counts} (not a final total).` : '');
    usage = `${totals} Usage ${known ? 'known' : 'unknown'}; accounting ${result.accounting}.${result.error ? ` Recorded error: ${result.error}.` : ''}`;
  }
  byId('operation-usage').textContent = usage;
  byId('final-answer').hidden = !result.reply;
  byId('final-answer').textContent = result.reply || '';
}

async function checkOperation() {
  if (!activeOperation || polling) return;
  const current = view;
  const id = activeOperation;
  const request = ++operationRequest;
  stopPolling();
  polling = true;
  controls();
  try {
    const result = await api.operation(id);
    if (current !== view || id !== activeOperation || request !== operationRequest) return;
    showOperation(result);
    if (result.status === 'accepted') pollTimer = setTimeout(() => void checkOperation(), 1500);
    else {
      notice(result.status === 'done' ? 'Response committed. You can ask a follow-up in this conversation.'
        : 'Review the recorded outcome. New work still requires the service’s authorization and recovery checks.', result.status === 'uncertain');
      if (lastTerminal !== id) {
        lastTerminal = id;
        void loadHistory();
      }
    }
  } catch (error) {
    if (current !== view || request !== operationRequest || error instanceof StaleResult) return;
    byId('cancel-operation').hidden = true;
    if (error.status === 404 || error.status === 403) {
      notice('Conversation history can be tenant-shared, but operation status and cancellation remain separately authorized. This credential cannot view this operation.');
    } else report(error, current);
    // No automatic retry after a transport/refusal error. The user can check again.
  } finally { if (current === view && request === operationRequest) { polling = false; controls(); } }
}

async function sendHeld() {
  if (!heldSubmission || sending) return;
  const current = view;
  sending = true;
  controls();
  notice('Sending one durable submission. A lost connection does not mean it was rejected.');
  try {
    const result = await api.submit(heldSubmission);
    if (current !== view) return;
    heldSubmission = null;
    byId('message').value = '';
    byId('pending-note').hidden = true;
    // A pre-submission read may still finish later. It cannot replace the
    // operation/history view selected by this actual acceptance acknowledgement.
    historyRequest += 1;
    selectOperation(result.operation);
    showOperation(result);
    notice(result.replayed ? 'The service recognized this submission and returned the original operation.' : 'Submission accepted. Waiting for the service’s recorded result.');
    void checkOperation();
    void loadHistory();
    void loadSessions();
  } catch (error) {
    if (current !== view || error instanceof StaleResult) return;
    report(error, current);
    byId('pending-note').hidden = heldSubmission === null;
    byId('pending-note').textContent = 'The exact message and submission key are held only in this page. Retry sends those same bytes and key, only when you press the retry button. Refreshing or leaving clears this recovery memory.';
  } finally { if (current === view) { sending = false; controls(); } }
}

byId('connect-form').addEventListener('submit', async event => {
  event.preventDefault();
  const token = byId('credential').value;
  disconnect('Connecting to the same-origin service…');
  try {
    api.connect(token);
    controls();
    if (await loadSessions()) notice('Connected. Start a conversation or reopen one from the list.');
  }
  catch (error) { report(error); }
});
byId('disconnect').addEventListener('click', () => disconnect());
byId('new-form').addEventListener('submit', event => { event.preventDefault(); selectSession(byId('session-name').value, true); });
byId('refresh-list').addEventListener('click', () => void loadSessions());
byId('more-sessions').addEventListener('click', () => void loadSessions(true));
byId('refresh-history').addEventListener('click', () => void loadHistory());
byId('more-messages').addEventListener('click', () => void loadHistory(true));
byId('check-operation').addEventListener('click', () => void checkOperation());
byId('message-form').addEventListener('submit', event => {
  event.preventDefault();
  if (sending || heldSubmission) return;
  try { heldSubmission = api.createSubmission(selected, byId('message').value); void sendHeld(); }
  catch (error) { report(error); }
});
byId('retry-submission').addEventListener('click', () => void sendHeld());
byId('clear-submission').addEventListener('click', () => {
  if (!heldSubmission || sending || !window.confirm('Clear only this page’s retry memory? The service may already have accepted the request. This does not cancel it. Check the conversation before sending a replacement.')) return;
  heldSubmission = null;
  byId('pending-note').hidden = true;
  notice('Local retry memory cleared. Service-side work is unchanged; inspect the conversation before starting replacement work.');
  controls();
});
byId('cancel-operation').addEventListener('click', async () => {
  if (!activeOperation || cancelling) return;
  const current = view;
  const id = activeOperation;
  cancelling = true;
  controls();
  try {
    const result = await api.cancel(id);
    if (current !== view || id !== activeOperation) return;
    notice(result.cancellation_status === 'terminal' ? 'The operation is already terminal. Checking its separately authorized result.'
      : 'Cancellation request retained. This is not proof of stopped execution or an undone external effect.');
    void checkOperation();
  } catch (error) { if (id === activeOperation) report(error, current); }
  finally { if (current === view && id === activeOperation) { cancelling = false; controls(); } }
});
window.addEventListener('beforeunload', event => {
  if (sending || heldSubmission) { event.preventDefault(); event.returnValue = ''; }
});
window.addEventListener('pagehide', () => disconnect());
controls();
