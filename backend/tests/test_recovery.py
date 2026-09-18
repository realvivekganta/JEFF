"""Recovery tools, inventory checks, and approval independent of model phrasing."""

import asyncio
from copy import deepcopy
import pytest
from main import Settings
from core.loop import handle_turn, process_turn
from core.session import create_session, create_runtime, get_record, session_view, interrupt_session, reset_session, BusyError
from core.guardrails import BookingError, require_recoverable
from core import tools
from test_context import call, say, fake_model, decision


def identify(session, code):
    session['transcript'].append({'role': 'user', 'text': code})
    return tools.lookup_reservation(session, code)


def ready(code='XK72LP', scenario='normal'):
    s = create_session(scenario)
    identify(s, code)
    tools.search_flights(s)
    return s


@pytest.mark.parametrize('code,flight_id,number,seat,other_id', [
    ('XK72LP', 'F2', 'DL2219', '14A', 'R2'),
    ('BOS842', 'F5', 'DL512', '12C', 'R1'),
])
async def test_two_routes_commit_only_after_approval_without_changing_other_reservations(code, flight_id, number, seat, other_id):
    s = ready(code)
    other = deepcopy(get_record(s['data'], 'reservations', other_id))
    request, requests = fake_model([call('propose_rebooking', flight_id=flight_id, seat=seat)], [decision()])
    reply = await handle_turn(s, 'Whatever you think is best', Settings(), request)
    assert 'Shall I confirm' in reply
    assert session_view(s)['reservation']['status'] == 'CANCELLED'
    await handle_turn(s, 'Yes, please.', Settings(), request)
    assert session_view(s)['reservation']['flight'] == number
    assert session_view(s)['reservation']['seat'] == seat
    assert session_view(s)['reservation']['status'] == 'CONFIRMED'
    followup, _ = fake_model([say('You leave this evening. Have a good trip!')])
    await handle_turn(s, 'Thanks!', Settings(), followup)
    assert len([e for e in s['events'] if e['name'] == 'rebook_flight']) == 1
    assert len(requests) == 2
    assert get_record(s['data'], 'reservations', other_id) == other


def test_unknown_code_clears_previous_passenger():
    s = create_session()
    identify(s, 'ABC123')
    assert s['reservation_id'] is None
    identify(s, 'XK72LP')
    identify(s, 'ABC123')
    assert session_view(s)['customer'] is None
    assert s['stage'] == 'identify'


def test_search_excludes_wrong_route_date_cancelled_early_and_full():
    assert [o['flight_id'] for o in ready()['options']] == ['F2', 'F3']


def test_structured_preferences_filter_inventory_and_preserve_unspecified_fields():
    s = ready()
    tools.update_preferences(s, seat_type='window', arrival_before='21:00')
    assert [o['flight_id'] for o in tools.search_flights(s)] == ['F2']
    tools.update_preferences(s, seat_type='unchanged', arrival_before='none')
    assert s['preferences'] == {'seat_type': 'window', 'arrival_before': None}


@pytest.mark.parametrize('seat,time', [('middle', 'unchanged'), ('any', '25:00'), ('any', 'tomorrow')])
def test_invalid_structured_preferences_do_not_change_state(seat, time):
    s = ready()
    before = deepcopy(s['preferences'])
    with pytest.raises(BookingError):
        tools.update_preferences(s, seat_type=seat, arrival_before=time)
    assert s['preferences'] == before


async def test_no_matching_inventory_escalates_with_saved_preferences():
    s = ready()
    request, _ = fake_model(
        [call('update_preferences', seat_type='aisle', arrival_before='21:00')],
        [call('search_flights')],
    )
    await handle_turn(s, 'An aisle seat and arrive before nine tonight', Settings(), request)
    assert s['stage'] == 'escalated'
    assert s['handoff']['preferences']['seat_type'] == 'aisle'
    assert session_view(s)['reservation']['status'] == 'CANCELLED'


@pytest.mark.parametrize('scenario', ['no-seats', 'api-error', 'sold-out'])
async def test_service_and_inventory_failures_never_commit(scenario):
    s = create_session(scenario)
    identify(s, 'XK72LP')
    request, _ = fake_model([call('search_flights')])
    if scenario == 'sold-out':
        tools.search_flights(s)
        tools.propose_rebooking(s, 'F2', '14A')
        text = 'yes'
        request, _ = fake_model([decision()])
    else:
        text = 'Find alternatives'
    await handle_turn(s, text, Settings(), request)
    assert s['stage'] == 'escalated'
    assert session_view(s)['reservation']['status'] == 'CANCELLED'


def test_delayed_reservation_cannot_use_cancellation_tools():
    s = create_session()
    identify(s, 'ORD631')
    with pytest.raises(BookingError):
        require_recoverable(s)
    assert session_view(s)['reservation']['status'] == 'DELAYED'


@pytest.mark.parametrize('text', ['yes if available', 'yes but the other one', 'yes?', 'do not confirm', 'maybe', 'you choose'])
async def test_nonapproval_interpretation_cannot_commit(text):
    s = ready()
    tools.propose_rebooking(s, 'F2', '14A')
    request, _ = fake_model([decision(False)], [say('Let’s clarify what you would like.')])
    await handle_turn(s, text, Settings(), request)
    assert session_view(s)['reservation']['status'] == 'CANCELLED'
    assert s['approval_id'] is None


async def test_interruption_and_questions_require_fresh_readback_and_approval():
    s = ready()
    tools.propose_rebooking(s, 'F2', '14A')
    interrupt_session(s, 'Flight DL2219')
    request, _ = fake_model([call('propose_rebooking', flight_id='F2', seat='14A')])
    await handle_turn(s, 'yes', Settings(), request)
    assert s['stage'] == 'confirm'
    question, _ = fake_model([decision(False)], [say('That is a window seat.')])
    await handle_turn(s, 'Is that a window seat?', Settings(), question)
    assert s['approval_id'] is None
    assert s['preferences']['seat_type'] == 'any'
    with pytest.raises(BookingError):
        tools.rebook_flight(s, s['pending']['proposal_id'])
    tools.propose_rebooking(s, 'F2', '14A')
    approval, _ = fake_model([decision()])
    await handle_turn(s, 'yes', Settings(), approval)
    assert s['stage'] == 'resolved'


async def test_denial_and_reservation_switch_invalidate_old_approval():
    s = ready()
    tools.propose_rebooking(s, 'F2', '14A')
    stale = s['approval_id']
    request, _ = fake_model([decision(False)], [say('No change made. Would you like another option?')])
    await handle_turn(s, 'No thanks', Settings(), request)
    with pytest.raises(BookingError):
        tools.rebook_flight(s, stale)
    identify(s, 'BOS842')
    assert s['pending'] is None
    assert get_record(s['data'], 'reservations', 'R1')['status'] == 'DISRUPTED'


@pytest.mark.parametrize('mutation', ['seat', 'time', 'status', 'revision', 'fare'])
async def test_commit_rechecks_inventory_and_proposal(mutation):
    s = ready()
    tools.propose_rebooking(s, 'F2', '14A')
    f = get_record(s['data'], 'flights', 'F2')
    if mutation == 'seat': f['seats'] = []
    if mutation == 'time': f['departure_at'] = '2026-09-18T19:20:00-04:00'
    if mutation == 'status': f['status'] = 'CANCELLED'
    if mutation == 'revision': s['revision'] += 1
    if mutation == 'fare': s['data']['policy']['additional_fare'] = 25
    approval, _ = fake_model([decision()])
    await handle_turn(s, 'yes', Settings(), approval)
    assert s['stage'] == 'escalated'
    assert session_view(s)['reservation']['status'] == 'CANCELLED'


def test_unoffered_flight_seat_and_fake_approval_are_rejected():
    s = ready()
    for flight, seat in [('F5', '12C'), ('F2', '1A')]:
        with pytest.raises(BookingError): tools.propose_rebooking(s, flight, seat)
    tools.propose_rebooking(s, 'F2', '14A')
    with pytest.raises(BookingError): tools.rebook_flight(s, 'invented')


def test_new_reservation_uses_existing_recovery_tools():
    s = create_session()
    s['data']['passengers'].append({'id': 'P9', 'name': 'Taylor Example'})
    s['data']['reservations'].append({'id': 'R9', 'confirmation': 'NEW999', 'passenger_id': 'P9', 'flight_id': 'F4', 'seat': '7C', 'status': 'DISRUPTED'})
    identify(s, 'NEW999')
    tools.search_flights(s)
    tools.propose_rebooking(s, 'F6', '9A')
    tools.rebook_flight(s, s['approval_id'])
    assert session_view(s)['customer']['name'] == 'Taylor Example'
    assert session_view(s)['reservation']['flight'] == 'DL618'


async def test_cancelled_model_turn_releases_busy_and_cannot_create_stale_proposal():
    entered = asyncio.Event()
    async def slow(payload):
        entered.set()
        await asyncio.sleep(30)
        return {'output': []}
    runtime = create_runtime(Settings(), slow)
    task = asyncio.create_task(process_turn(runtime, 'Tell me my options'))
    await entered.wait()
    with pytest.raises(BusyError): reset_session(runtime)
    with pytest.raises(BusyError): await process_turn(runtime, 'yes')
    interrupt_session(runtime['session'])
    task.cancel()
    with pytest.raises(asyncio.CancelledError): await task
    assert not runtime['busy'] and runtime['session']['approval_id'] is None


@pytest.mark.parametrize('response', [
    {'status': 'incomplete', 'output': [decision()]},
    {'status': 'completed', 'output': [say('{"decision":"maybe"}')]},
    {'status': 'completed', 'output': [{'type': 'message', 'content': [{'type': 'refusal', 'refusal': 'No'}]}]},
])
async def test_invalid_approval_responses_never_commit(response):
    s = ready()
    tools.propose_rebooking(s, 'F2', '14A')
    async def invalid(payload):
        return response
    await handle_turn(s, "Yeah, let's do it", Settings(), invalid)
    assert session_view(s)['reservation']['status'] == 'CANCELLED'
    assert s['approval_id'] is None


async def test_interruption_during_approval_interpretation_prevents_commit():
    s = ready()
    tools.propose_rebooking(s, 'F2', '14A')
    async def interrupted(payload):
        if 'text' in payload:
            interrupt_session(s)
            return {'status': 'completed', 'output': [decision()]}
        return {'status': 'completed', 'output': [say('Let me repeat the flight details.') ]}
    await handle_turn(s, "Yeah, let's do it", Settings(), interrupted)
    assert session_view(s)['reservation']['status'] == 'CANCELLED'
    assert s['approval_id'] is None
