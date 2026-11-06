"""Thin HTTP client for the Ridgeline TMS.

Every call returns a plain dict the LLM can read:
  {"ok": True,  "status": 200, "data": {...}}
  {"ok": False, "status": 409, "error": {"code": "...", "message": "..."}}
Status 0 means the server could not be reached at all.
"""
import json
import os
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid

BASE_URL = os.environ.get('TMS_BASE_URL', 'http://127.0.0.1:8000')
TIMEOUT = 5
# Only transient failures are retried. 4xx errors are final.
RETRY_STATUSES = {0, 502, 503, 504}
MAX_ATTEMPTS = 3


def new_idempotency_key():
    """One key per confirmed action. Reuse it for every retry of that action."""
    return uuid.uuid4().hex


def _request(method, path, body=None, key=None):
    headers = {}
    data = None
    if body is not None:
        data = json.dumps(body).encode()
        headers['Content-Type'] = 'application/json'
    if key:
        headers['Idempotency-Key'] = key
    req = urllib.request.Request(BASE_URL + path, data=data, headers=headers, method=method)
    try:
        with urllib.request.urlopen(req, timeout=TIMEOUT) as resp:
            return {'ok': True, 'status': resp.status, 'data': json.load(resp)}
    except urllib.error.HTTPError as exc:
        try:
            error = json.load(exc).get('error', {})
        except (ValueError, AttributeError):
            error = {}
        return {'ok': False, 'status': exc.code,
                'error': {'code': error.get('code', 'http_error'),
                          'message': error.get('message', f'HTTP {exc.code}')}}
    except (urllib.error.URLError, TimeoutError, OSError) as exc:
        return {'ok': False, 'status': 0,
                'error': {'code': 'unreachable', 'message': f'Could not reach the TMS: {exc}'}}


def get(path):
    """GET with a short retry for transient failures (safe: reads have no side effects)."""
    result = _request('GET', path)
    for attempt in range(1, MAX_ATTEMPTS):
        if result['ok'] or result['status'] not in RETRY_STATUSES:
            break
        time.sleep(0.3 * attempt)
        result = _request('GET', path)
    return result


def post(path, body, key):
    """POST with retries. The same key is sent on every attempt, so a retry after a
    timeout can never create a duplicate record."""
    result = _request('POST', path, body, key)
    for attempt in range(1, MAX_ATTEMPTS):
        if result['ok'] or result['status'] not in RETRY_STATUSES:
            break
        time.sleep(0.5 * attempt)
        result = _request('POST', path, body, key)
    return result


# --- Convenience wrappers -------------------------------------------------

def find_driver_by_truck(truck):
    return get(f'/v1/drivers?truck={urllib.parse.quote(str(truck))}')


def driver_loads(driver_id, status=None):
    path = f'/v1/loads?driver={urllib.parse.quote(driver_id)}'
    if status:
        path += f'&status={urllib.parse.quote(status)}'
    return get(path)


def load(load_id):
    return get(f'/v1/loads/{urllib.parse.quote(load_id)}')


def facility(facility_id):
    return get(f'/v1/facilities/{urllib.parse.quote(facility_id)}')


def kb(topic=None):
    return get('/v1/kb' + (f'?topic={urllib.parse.quote(topic)}' if topic else ''))


def save_check_call(load_id, body, key):
    return post(f'/v1/loads/{urllib.parse.quote(load_id)}/check-calls', body, key)


def create_callback(body, key):
    return post('/v1/callbacks', body, key)
