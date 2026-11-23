"""Tool executor (business rules) and the LangChain tools that wrap it.

Design rules enforced here (in code, not just in the prompt):
- The driver is identified once via identify_driver; write tools always use that
  driver id, never one supplied by the model.
- Each confirmed write gets one idempotency key. If the model repeats an identical
  write, the cached result is returned instead of POSTing again.
"""
import json
from typing import Literal, Optional

from langchain_core.tools import tool

import tms_client as tms

CATEGORY_URGENCY = {
    'breakdown': 'now', 'safety_injury': 'now', 'gate_or_dock': 'now',
    'appointment_detention': 'today', 'paperwork': 'today', 'pay_general': 'routine',
}


class ToolExecutor:
    def __init__(self):
        self.driver = None          # driver record once identified
        self.must_say = []          # (position, text, marker) lines the reply must contain
        self.last_agent_text = ''   # what the agent last said aloud
        self.heard = ''             # digits from everything the driver has said
        self._writes = {}           # (kind, canonical body) -> (key, result)

    def note_driver_text(self, text):
        self.heard += ' ' + ''.join(ch for ch in text if ch.isdigit()) + ' '

    def run(self, name, args):
        """Run a tool and return a JSON string for the model."""
        handler = getattr(self, f'_{name}', None)
        if handler is None:
            result = {'ok': False, 'error': {'code': 'unknown_tool', 'message': name}}
        else:
            try:
                result = handler(**args)
            except TypeError as exc:
                result = {'ok': False, 'error': {'code': 'bad_arguments', 'message': str(exc)}}
        return json.dumps(result)

    # --- helpers ---
    def _need_driver(self):
        if self.driver is None:
            return {'ok': False, 'error': {'code': 'not_identified',
                                           'message': 'Identify the driver by truck number first.'}}

    def _kb_lines(self, *topics):
        out = {}
        result = tms.kb()
        if result['ok']:
            for topic in topics:
                out[topic] = result['data']['item'].get(topic)
        return out

    def _write(self, kind, body, send):
        """Idempotent write: one key per distinct confirmed action."""
        cache_id = (kind, json.dumps(body, sort_keys=True))
        if cache_id in self._writes:
            key, previous = self._writes[cache_id]
            if previous['ok']:
                return previous  # model repeated a write that already succeeded
        else:
            key = tms.new_idempotency_key()
        result = send(body, key)
        self._writes[cache_id] = (key, result)  # failed writes keep their key for a retry
        return result

    # --- tools ---
    def _identify_driver(self, truck):
        result = tms.find_driver_by_truck(truck)
        if not result['ok']:
            return result
        items = result['data']['items']
        if len(items) != 1:
            return {'ok': False, 'error': {'code': 'no_match',
                                           'message': f'No unique driver found for truck {truck}.'}}
        self.driver = items[0]
        loads = tms.driver_loads(self.driver['id'])
        active = [l for l in loads['data']['items'] if l['status'] == 'in_transit'] if loads['ok'] else []
        return {'ok': True, 'driver': self.driver,
                'current_load': active[0]['id'] if active else None,
                'current_load_delivery_window': active[0]['delivery'] if active else None,
                'instruction': 'Driver identified. Greet them by first name and ask how you can help. '
                               'Do not ask for the truck number again.'}

    def _get_my_loads(self, status=None):
        return self._need_driver() or tms.driver_loads(self.driver['id'], status)

    def _get_load(self, load_id):
        result = self._need_driver() or tms.load(load_id)
        # Never expose another driver's load.
        if result['ok'] and result['data']['item']['driver'] != self.driver['id']:
            return {'ok': False, 'status': 403,
                    'error': {'code': 'not_your_load', 'message': 'That load is not assigned to this driver.'}}
        return result

    def _get_facility(self, facility_id):
        return self._need_driver() or tms.facility(facility_id)

    def _get_company_info(self, topic=None):
        result = tms.kb(topic)
        if not result['ok'] and result['status'] == 404:
            result['hint'] = 'Call again with no topic to get everything.'
        return result

    def _save_status_update(self, load_id, location, eta, confirmed=False, notes=''):
        if (err := self._need_driver()):
            return err
        spoken = self.last_agent_text.lower()
        if not confirmed or '?' not in spoken or location.split(',')[0].strip().lower() not in spoken:
            return {'ok': False, 'error': {'code': 'not_confirmed',
                    'message': 'Read the details back and get a yes from the driver first. Nothing saved.'}}
        body = {'driver': self.driver['id'], 'location': location, 'eta': eta, 'notes': notes}
        result = self._write('check_call:' + load_id, body,
                             lambda b, k: tms.save_check_call(load_id, b, k))
        if not result['ok']:
            self._require_dispatch_contact()
            result = {**result, 'instruction': 'NOT saved. Tell the driver it was not saved.'}
        return result

    def _request_callback(self, callback_number, reason, category, load_id=None):
        if (err := self._need_driver()):
            return err
        if category not in CATEGORY_URGENCY:
            return {'ok': False, 'error': {'code': 'bad_category', 'message': f'Use one of {list(CATEGORY_URGENCY)}'}}
        urgency = CATEGORY_URGENCY[category]
        digits = ''.join(ch for ch in callback_number if ch.isdigit())
        if len(digits) < 7 or digits not in self.heard.replace(' ', ''):
            return {'ok': False, 'error': {'code': 'number_not_given',
                    'message': 'The driver has not told you this callback number. Ask them for it and '
                               'wait for their answer. Nothing saved.'}}
        body = {'driver': self.driver['id'], 'callback_number': callback_number,
                'reason': reason, 'urgency': urgency}
        # Attach the load only if it really is this driver's; never let a guessed id fail the call.
        mine = tms.driver_loads(self.driver['id'])
        if load_id and mine['ok'] and any(l['id'] == load_id for l in mine['data']['items']):
            body['load'] = load_id
        result = self._write('callback', body, tms.create_callback)
        if not result['ok']:
            self._require_dispatch_contact()
            return {**result, 'instruction': 'NOT saved. Say so and give the dispatch contact.'}
        lines = self._kb_lines('breakdown_line', 'safety_line')
        if category == 'breakdown' and lines.get('breakdown_line'):
            self.must_say.append(('end', f"Breakdown line: {lines['breakdown_line']}", lines['breakdown_line']))
        if category == 'safety_injury' and lines.get('safety_line'):
            self.must_say.append(('start', 'If anyone is hurt, contact emergency services first.', 'emergency'))
            self.must_say.append(('end', f"Then call the safety line: {lines['safety_line']}", lines['safety_line']))
        return {**result, 'instruction': 'Say only that the callback request was recorded. Do not say when '
                                         'dispatch will call. Required phone lines are added automatically.'}

    def _require_dispatch_contact(self):
        contact = self._kb_lines('dispatch_contact').get('dispatch_contact')
        if contact:
            self.must_say.append(('replace', f"I wasn't able to save that. Please call dispatch: {contact}", contact))


def make_tools(ex):
    """LangChain tools bound to one ToolExecutor (one call = one session).

    Every tool returns a JSON string; the guards live in ToolExecutor, not here."""

    @tool(parse_docstring=True)
    def identify_driver(truck: str) -> str:
        """Look up the caller by truck number. Call this first, before anything else.

        Args:
            truck: Truck number, digits only, e.g. "117".
        """
        return ex.run('identify_driver', {'truck': truck})

    @tool(parse_docstring=True)
    def get_my_loads(status: Optional[Literal['assigned', 'in_transit', 'delivered']] = None) -> str:
        """The identified driver's loads. Optionally filter by status.

        Args:
            status: Only return loads with this status.
        """
        return ex.run('get_my_loads', {'status': status} if status else {})

    @tool(parse_docstring=True)
    def get_load(load_id: str) -> str:
        """Details of one load, including pickup and delivery stops with facility ids.

        Args:
            load_id: Load number, e.g. RF-48213.
        """
        return ex.run('get_load', {'load_id': load_id})

    @tool(parse_docstring=True)
    def get_facility(facility_id: str) -> str:
        """Address, hours, gate code and dock notes for a facility id.

        Args:
            facility_id: Facility id from a load stop, e.g. receiver-1.
        """
        return ex.run('get_facility', {'facility_id': facility_id})

    @tool(parse_docstring=True)
    def get_company_info(topic: Optional[str] = None) -> str:
        """Company facts and policies (fuel card, phone lines, detention, etc.). Omit topic to get everything.

        Args:
            topic: A single topic key. Leave empty to read all topics.
        """
        return ex.run('get_company_info', {'topic': topic} if topic else {})

    @tool(parse_docstring=True)
    def save_status_update(load_id: str, location: str, eta: str, confirmed: bool,
                           notes: str = '') -> str:
        """Save a driver status update (check call) to their load. Only call AFTER the driver said yes to your read-back.

        Args:
            load_id: The driver's in-transit load number.
            location: Where the driver is.
            eta: ISO 8601 with timezone offset, e.g. 2026-11-03T13:30:00-07:00.
            confirmed: True only if the driver said yes to your read-back of these exact details in their latest message.
            notes: Optional notes from the driver.
        """
        return ex.run('save_status_update', {'load_id': load_id, 'location': location, 'eta': eta,
                                             'confirmed': confirmed, 'notes': notes})

    @tool(parse_docstring=True)
    def request_callback(callback_number: str, reason: str,
                         category: Literal['breakdown', 'safety_injury', 'gate_or_dock',
                                           'appointment_detention', 'paperwork', 'pay_general'],
                         load_id: Optional[str] = None) -> str:
        """Ask a dispatcher to call the driver back. Only call after the driver gave you a callback number.

        Args:
            callback_number: Exactly as the driver said it.
            reason: Short reason for the callback.
            category: breakdown = truck broke down or cannot move; safety_injury = accident, injury or danger; gate_or_dock = stuck at a gate or dock right now; appointment_detention = appointment at risk, waiting past appointment, detention or layover approval; paperwork; pay_general = pay, scheduling preferences, hours-of-service and other questions.
            load_id: Optional; omit if unknown.
        """
        return ex.run('request_callback', {'callback_number': callback_number, 'reason': reason,
                                           'category': category, **({'load_id': load_id} if load_id else {})})

    return [identify_driver, get_my_loads, get_load, get_facility, get_company_info,
            save_status_update, request_callback]
