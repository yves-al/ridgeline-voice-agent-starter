"""LLM tool definitions and their executor.

Design rules enforced here (in code, not just in the prompt):
- The driver is identified once via identify_driver; write tools always use that
  driver id, never one supplied by the model.
- Each confirmed write gets one idempotency key. If the model repeats an identical
  write, the cached result is returned instead of POSTing again.
"""
import json

import tms_client as tms

CATEGORY_URGENCY = {
    'breakdown': 'now', 'safety_injury': 'now', 'gate_or_dock': 'now',
    'appointment_detention': 'today', 'paperwork': 'today', 'pay_general': 'routine',
}

TOOLS = [
    {'type': 'function', 'function': {
        'name': 'identify_driver',
        'description': 'Look up the caller by truck number. Call this first, before anything else.',
        'parameters': {'type': 'object', 'properties': {
            'truck': {'type': 'string', 'description': 'Truck number, digits only, e.g. "117"'}},
            'required': ['truck']}}},
    {'type': 'function', 'function': {
        'name': 'get_my_loads',
        'description': "The identified driver's loads. Optionally filter by status.",
        'parameters': {'type': 'object', 'properties': {
            'status': {'type': 'string', 'enum': ['assigned', 'in_transit', 'delivered']}}}}},
    {'type': 'function', 'function': {
        'name': 'get_load',
        'description': 'Details of one load, including pickup and delivery stops with facility ids.',
        'parameters': {'type': 'object', 'properties': {
            'load_id': {'type': 'string', 'description': 'e.g. RF-48213'}}, 'required': ['load_id']}}},
    {'type': 'function', 'function': {
        'name': 'get_facility',
        'description': 'Address, hours, gate code and dock notes for a facility id.',
        'parameters': {'type': 'object', 'properties': {
            'facility_id': {'type': 'string'}}, 'required': ['facility_id']}}},
    {'type': 'function', 'function': {
        'name': 'get_company_info',
        'description': 'Company facts and policies (fuel card, phone lines, detention, etc.). '
                       'Omit topic to list every topic.',
        'parameters': {'type': 'object', 'properties': {'topic': {'type': 'string'}}}}},
    {'type': 'function', 'function': {
        'name': 'save_status_update',
        'description': 'Save a driver status update (check call) to their assigned load. Only call '
                       'AFTER the driver has confirmed location, ETA and notes out loud.',
        'parameters': {'type': 'object', 'properties': {
            'load_id': {'type': 'string'},
            'location': {'type': 'string'},
            'eta': {'type': 'string', 'description': 'ISO 8601 with timezone offset, e.g. 2026-11-03T13:30:00-07:00'},
            'notes': {'type': 'string'},
            'confirmed': {'type': 'boolean', 'description': 'true ONLY if the driver said yes to your read-back of these exact details in their latest message'}},
            'required': ['load_id', 'location', 'eta', 'confirmed']}}},
    {'type': 'function', 'function': {
        'name': 'request_callback',
        'description': 'Ask a dispatcher to call the driver back. Only call after the driver has given you a '
                       'callback number. Pick the category that fits; urgency is set from it automatically.',
        'parameters': {'type': 'object', 'properties': {
            'callback_number': {'type': 'string', 'description': 'Exactly as the driver said it'},
            'reason': {'type': 'string'},
            'category': {'type': 'string', 'enum': list(CATEGORY_URGENCY),
                         'description': 'breakdown = truck broke down or cannot move; safety_injury = accident, '
                                        'injury or danger; gate_or_dock = stuck at a gate or dock right now; '
                                        'appointment_detention = appointment at risk, waiting past appointment, '
                                        'detention or layover approval; paperwork; pay_general = pay, '
                                        'scheduling preferences, hours-of-service and other questions'},
            'load_id': {'type': 'string', 'description': 'Optional; omit if unknown'}},
            'required': ['callback_number', 'reason', 'category']}}},
]


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
            self.must_say.append(('end', f'Dispatch: {contact}', contact))
