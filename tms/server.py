#!/usr/bin/env python3
"""Local Ridgeline training API. State is reset on restart; no dependencies."""
import argparse
import copy
import json
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlsplit


class APIError(Exception):
    def __init__(self, status, code, message):
        self.status, self.code, self.message = status, code, message


def required(body, fields):
    for field in fields:
        if not isinstance(body.get(field), str) or not body[field].strip():
            raise APIError(422, 'invalid_field', f'{field} must be a nonempty string')


def timestamp(value):
    try:
        result = datetime.fromisoformat(value.replace('Z', '+00:00'))
        if result.utcoffset() is None:
            raise ValueError()
        return result
    except (ValueError, TypeError, AttributeError):
        raise APIError(422, 'invalid_eta', 'eta must be an ISO 8601 timestamp with timezone')


def make_server(port=8000, fail_check_calls=False):
    seed = json.loads(Path(__file__).with_name('seed.json').read_text())
    state = copy.deepcopy(seed)
    state.update(check_calls=[], callbacks=[])
    saved = {}

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *_):
            pass

        def reply(self, status, payload):
            raw = json.dumps(payload).encode()
            self.send_response(status)
            self.send_header('Content-Type', 'application/json')
            self.send_header('Content-Length', str(len(raw)))
            self.end_headers()
            self.wfile.write(raw)

        def lookup(self, collection, ident):
            for item in state[collection]:
                if item['id'] == ident:
                    return item
            raise APIError(404, 'not_found', f'{collection}: {ident} not found')

        def do_GET(self):
            try:
                url = urlsplit(self.path)
                parts = url.path.strip('/').split('/')
                query = parse_qs(url.query)
                if url.path == '/health':
                    return self.reply(200, {'status': 'ok'})
                if parts == ['v1', 'kb']:
                    topic = query.get('topic', [''])[0]
                    if topic:
                        if topic not in state['kb']:
                            raise APIError(404, 'not_found', 'Unknown topic')
                        return self.reply(200, {'item': {'topic': topic, 'answer': state['kb'][topic]}})
                    return self.reply(200, {'item': state['kb']})
                if len(parts) == 4 and parts[:2] == ['v1', 'loads'] and parts[3] == 'check-calls':
                    self.lookup('loads', parts[2])
                    return self.reply(200, {'items': [c for c in state['check_calls'] if c['load'] == parts[2]]})
                collections = {'drivers': 'drivers', 'loads': 'loads', 'facilities': 'facilities',
                               'check-calls': 'check_calls', 'callbacks': 'callbacks'}
                if len(parts) in (2, 3) and parts[0] == 'v1' and parts[1] in collections:
                    collection = collections[parts[1]]
                    if len(parts) == 3:
                        return self.reply(200, {'item': self.lookup(collection, parts[2])})
                    items = state[collection]
                    for field in ('truck', 'driver', 'status'):
                        if field in query:
                            items = [i for i in items if i.get(field) == query[field][0]]
                    return self.reply(200, {'items': items})
                raise APIError(404, 'not_found', 'Unknown route')
            except APIError as e:
                self.reply(e.status, {'error': {'code': e.code, 'message': e.message}})

        def do_POST(self):
            try:
                path = urlsplit(self.path).path
                parts = path.strip('/').split('/')
                check = len(parts) == 4 and parts[:2] == ['v1', 'loads'] and parts[3] == 'check-calls'
                if not check and path != '/v1/callbacks':
                    raise APIError(404, 'not_found', 'Unknown route')
                try:
                    length = int(self.headers.get('Content-Length', '0'))
                    if not 0 < length <= 65536:
                        raise ValueError()
                    body = json.loads(self.rfile.read(length))
                    if not isinstance(body, dict):
                        raise ValueError()
                except (ValueError, UnicodeDecodeError):
                    raise APIError(400, 'invalid_json', 'Send a JSON object up to 64 KiB')
                key = self.headers.get('Idempotency-Key', '').strip()
                if not key:
                    raise APIError(422, 'missing_key', 'Idempotency-Key is required')
                cache_key = (path, key)
                if cache_key in saved:
                    old_body, item = saved[cache_key]
                    if old_body != body:
                        raise APIError(409, 'key_reused', 'Key already used with different data')
                    return self.reply(200, {'item': item})
                required(body, ['driver'])
                driver = self.lookup('drivers', body['driver'])
                if check:
                    required(body, ['location', 'eta'])
                    load = self.lookup('loads', parts[2])
                    if load['driver'] != driver['id']:
                        raise APIError(409, 'driver_mismatch', 'Load is assigned to another driver')
                    if load['status'] == 'delivered':
                        raise APIError(409, 'load_delivered', 'Cannot update a delivered load')
                    timestamp(body['eta'])
                    if 'notes' in body and not isinstance(body['notes'], str):
                        raise APIError(422, 'invalid_field', 'notes must be a string')
                    if fail_check_calls:
                        raise APIError(503, 'unavailable', 'Check-call service is unavailable; nothing saved')
                    item = {k: body[k] for k in ('driver', 'location', 'eta')}
                    item.update(load=load['id'], notes=body.get('notes', ''))
                    collection = 'check_calls'
                else:
                    required(body, ['callback_number', 'reason', 'urgency'])
                    if body['urgency'] not in ('now', 'today', 'routine'):
                        raise APIError(422, 'invalid_urgency', 'Use now, today or routine')
                    if 'load' in body:
                        required(body, ['load'])
                        load = self.lookup('loads', body['load'])
                        if load['driver'] != driver['id']:
                            raise APIError(409, 'driver_mismatch', 'Load is assigned to another driver')
                    item = {k: body[k] for k in ('driver', 'callback_number', 'reason', 'urgency')}
                    item.update(load=body.get('load'), status='open')
                    collection = 'callbacks'
                item.update(id=f'{collection}-{len(state[collection])+1}', truck=driver['truck'],
                            timestamp=datetime.now(timezone.utc).isoformat())
                state[collection].append(item)
                saved[cache_key] = (body, copy.deepcopy(item))
                self.reply(201, {'item': item})
            except APIError as e:
                self.reply(e.status, {'error': {'code': e.code, 'message': e.message}})

    return HTTPServer(('127.0.0.1', port), Handler)


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--port', type=int, default=8000)
    parser.add_argument('--fail-check-calls', action='store_true')
    args = parser.parse_args()
    server = make_server(args.port, args.fail_check_calls)
    print(f'Ridgeline TMS: http://127.0.0.1:{server.server_port} (restart resets state)', flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
