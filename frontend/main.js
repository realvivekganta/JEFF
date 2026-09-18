// The dashboard displays Python state; booking decisions stay on the server.
const $ = (id) => document.getElementById(id);
let pending = false;
let lastTranscript = '';
let lastOptions = '';
let lastEvents = '';
let lastReservations = '';

function readableDeadline(value) {
  if (!value) return 'none';
  const [date, time] = value.split('T');
  const [hours, minutes] = time.slice(0, 5).split(':');
  const hour = Number(hours);
  return `${date}, ${hour % 12 || 12}:${minutes} ${hour >= 12 ? 'PM' : 'AM'} (destination local)`;
}

function node(tag, text, className) {
  const element = document.createElement(tag);
  element.textContent = text;
  if (className) element.className = className;
  return element;
}

function renderTrip(state) {
  const customer = state.customer || {};
  const reservation = state.reservation || {};
  const trip = state.trip || {};
  const fields = {
    passenger: customer.name || 'Awaiting lookup',
    confirmation: customer.confirmation || '—',
    origin: reservation.origin || '—',
    destination: reservation.destination || '—',
    'origin-name': trip.origin_name || 'Origin',
    'destination-name': trip.destination_name || 'Destination',
    'travel-date': trip.travel_date || '—',
    flight: reservation.flight || '—',
    departure: reservation.departure || '—',
    seat: reservation.seat || '—',
    status: reservation.status || 'LOOKUP',
  };
  for (const [id, value] of Object.entries(fields)) $(id).textContent = value;
  $('status').classList.toggle('confirmed', state.stage === 'resolved');
  $('trip-note').textContent = 'Times are local to each airport. Fictional inventory and reservations.';
  $('message').placeholder = state.reservation
    ? 'Find alternatives, ask a question, or choose a flight…'
    : 'Enter a demo confirmation code…';
  $('find-alternatives').dataset.text = 'Find alternative flights';
  const result = {
    resolved: 'Your updated trip is confirmed. You’re on your way.',
    escalated: state.handoff?.reason + '. Human review recorded; no live transfer.',
    identify: 'Enter a confirmation code to find the affected reservation.',
  };
  $('result').textContent = result[state.stage]
    || `${state.disruption?.reason || 'Reservation found'}. Let’s review your recovery options.`;
  $('result').classList.toggle('success', state.stage === 'resolved');
  const preferences = state.preferences;
  $('preferences').textContent = state.reservation
    ? `Seat preference: ${preferences.seat_type}. Arrival deadline: ${readableDeadline(preferences.arrival_before)}.`
    : 'Choose a demo case below, or provide its code in the conversation.';

  const signature = JSON.stringify(state.demoReservations);
  if (signature !== lastReservations) {
    lastReservations = signature;
    const selected = $('demo-reservation').value;
    $('demo-reservation').replaceChildren(...state.demoReservations.map((reservation) => {
      const option = node('option', reservation.label);
      option.value = reservation.confirmation;
      return option;
    }));
    if (state.demoReservations.some((reservation) => reservation.confirmation === selected)) {
      $('demo-reservation').value = selected;
    }
  }
}

function renderTranscript(state) {
  const signature = JSON.stringify([state.transcript, state.trip?.destination_name]);
  if (signature === lastTranscript) return;
  lastTranscript = signature;

  $('messages').replaceChildren();
  for (const message of state.transcript) {
    const bubble = node('div', '', `message ${message.role}`);
    bubble.append(
      node('b', message.role === 'user' ? 'Passenger' : 'Recovery assistant'),
      node('span', message.text),
    );
    $('messages').append(bubble);
  }
  if (!state.transcript.length) {
    $('messages').append(node(
      'p',
      'Start with a confirmation code. The agent will look up its reservation and disruption before offering alternatives.',
      'empty',
    ));
  }
  $('messages').scrollTop = $('messages').scrollHeight;
}

function renderEvents(events) {
  const signature = JSON.stringify(events);
  if (signature === lastEvents) return;
  lastEvents = signature;

  const items = events.map((event) => {
    const item = node('li', '');
    item.append(node('b', event.name.replaceAll('_', ' ')), node('span', event.detail));
    return item;
  });
  $('events').replaceChildren(...items);
  if (!events.length) $('events').append(node('li', 'No actions yet'));
}

function renderOptions(state) {
  // Avoid replacing unchanged buttons during polling so keyboard focus stays put.
  const signature = JSON.stringify([state.stage, state.options]);
  if (signature === lastOptions) return;
  lastOptions = signature;

  $('options').replaceChildren();
  if (state.stage === 'options') {
    for (const flight of state.options) {
      const button = node('button', `${flight.flight} · ${flight.departure} → ${flight.arrival}`);
      button.onclick = () => send(`I choose ${flight.flight}`);
      $('options').append(button);
    }
  }
  if (state.stage === 'confirm') {
    const button = node('button', 'Confirm rebooking', 'primary');
    button.onclick = () => send('confirm rebooking');
    $('options').append(button);
  }
}

function disableButtons(disabled) {
  document.querySelectorAll('button').forEach((button) => {
    button.disabled = disabled;
  });
}

function render(state) {
  const stages = {
    resolved: 'Resolved',
    escalated: 'Human review',
    confirm: 'Awaiting confirmation',
    identify: 'Find reservation',
    options: 'Choose an alternative',
  };
  $('mode').textContent = state.mode;
  $('stage').textContent = state.activeCall
    ? 'Phone call connected'
    : stages[state.stage] || 'Ready';
  renderTrip(state);
  renderTranscript(state);
  renderEvents(state.events);
  renderOptions(state);
  disableButtons(pending || state.busy || state.activeCall);
  $('find-alternatives').disabled ||= !state.reservation;
  $('demo-reservation').disabled = pending || state.busy || state.activeCall;
}

async function api(path, data) {
  const options = data ? {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(data),
  } : {};
  const response = await fetch(`/api/${path}`, options);
  const result = await response.json();
  if (!response.ok) {
    throw new Error(result.error || (
      typeof result.detail === 'string' ? result.detail : 'Check the submitted message'
    ));
  }
  return result;
}

async function send(text) {
  if (pending) return;
  pending = true;
  $('error').textContent = '';
  disableButtons(true);
  try {
    const state = await api('turn', { text });
    pending = false;
    render(state);
    $('message').value = '';
  } catch (error) {
    $('error').textContent = error.message;
  } finally {
    pending = false;
    await refresh();
  }
}

async function refresh() {
  try {
    render(await api('state'));
    $('connection').textContent = 'Connected';
  } catch {
    $('connection').textContent = 'Disconnected';
  }
}

$('chat').onsubmit = (event) => {
  event.preventDefault();
  send($('message').value);
};
$('find-alternatives').onclick = () => send($('find-alternatives').dataset.text);
document.querySelectorAll('[data-text]').forEach((button) => {
  button.onclick = () => send(button.dataset.text);
});
$('load-reservation').onclick = async () => {
  if (pending) return;
  const confirmation = $('demo-reservation').value;
  pending = true;
  disableButtons(true);
  try {
    render(await api('reset', { scenario: $('scenario').value }));
    pending = false;
    await send(`My confirmation is ${confirmation}`);
  } catch (error) {
    $('error').textContent = error.message;
  } finally {
    pending = false;
    await refresh();
  }
};
$('reset').onclick = async () => {
  try {
    render(await api('reset', { scenario: $('scenario').value }));
    $('error').textContent = '';
  } catch (error) {
    $('error').textContent = error.message;
  }
};

await refresh();
setInterval(refresh, 1000);
