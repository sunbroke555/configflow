"""Generate PyJWT 2.8.0 reference cases for the Go HS256 port.

Run from the repository root on the test server:
  ~/cf-test/.venv/bin/python server/internal/auth/testdata/gen_pyjwt.py
"""
import base64
import json
import time
from datetime import datetime, timedelta

import jwt

SECRET = 'go-port-test-secret'
NOW = int(time.time())
FAR = NOW + 10 * 365 * 86400


def b64(data):
    return base64.urlsafe_b64encode(data).rstrip(b'=').decode()


def raw(header, payload, secret=SECRET):
    import hmac, hashlib
    signing = b64(header) + '.' + b64(payload)
    sig = hmac.new(secret.encode(), signing.encode(), hashlib.sha256).digest()
    return signing + '.' + b64(sig)


tokens = {
    'valid': jwt.encode({'username': 'admin', 'exp': FAR, 'iat': NOW - 10}, SECRET, algorithm='HS256'),
    'unicode_user': jwt.encode({'username': '管理员', 'exp': FAR, 'iat': NOW - 10}, SECRET, algorithm='HS256'),
    'expired': jwt.encode({'username': 'admin', 'exp': NOW - 100, 'iat': NOW - 200}, SECRET, algorithm='HS256'),
    'future_iat': jwt.encode({'username': 'admin', 'exp': FAR, 'iat': FAR - 10}, SECRET, algorithm='HS256'),
    'nbf_future': jwt.encode({'username': 'admin', 'exp': FAR, 'nbf': FAR - 10}, SECRET, algorithm='HS256'),
    'wrong_secret': jwt.encode({'username': 'admin', 'exp': FAR}, 'other-secret', algorithm='HS256'),
    'hs512': jwt.encode({'username': 'admin', 'exp': FAR}, SECRET, algorithm='HS512'),
    'none_alg': raw(b'{"alg":"none","typ":"JWT"}', b'{"username":"admin"}'),
    'no_alg': raw(b'{"typ":"JWT"}', b'{"username":"admin"}'),
    'aud': jwt.encode({'username': 'admin', 'exp': FAR, 'aud': 'x'}, SECRET, algorithm='HS256'),
    'empty_aud': jwt.encode({'username': 'admin', 'exp': FAR, 'aud': ''}, SECRET, algorithm='HS256'),
    'exp_string': raw(b'{"alg":"HS256","typ":"JWT"}', json.dumps({'exp': str(FAR)}).encode()),
    'exp_bad_string': raw(b'{"alg":"HS256","typ":"JWT"}', b'{"exp":"soon"}'),
    'exp_float': raw(b'{"alg":"HS256","typ":"JWT"}', json.dumps({'exp': FAR + 0.5}).encode()),
    'exp_bool': raw(b'{"alg":"HS256","typ":"JWT"}', b'{"exp":true}'),
    'iat_bad': raw(b'{"alg":"HS256","typ":"JWT"}', b'{"iat":"x","exp":%d}' % FAR),
    'payload_list': raw(b'{"alg":"HS256","typ":"JWT"}', b'[1,2]'),
    'payload_bad_json': raw(b'{"alg":"HS256","typ":"JWT"}', b'{"a":'),
    'header_list': raw(b'[1]', b'{}'),
    'header_bad_json': 'eyJhbGciOi.' + b64(b'{}') + '.sig',
    'one_segment': 'abc',
    'two_segments': 'abc.def',
    'bad_padding': 'a.b.c',
    'empty_payload_object': raw(b'{"alg":"HS256","typ":"JWT"}', b'{}'),
    'error_claim': raw(b'{"alg":"HS256","typ":"JWT"}', b'{"error":"expired","exp":%d}' % FAR),
    'junk_chars': jwt.encode({'username': 'admin', 'exp': FAR}, SECRET, algorithm='HS256').replace('.', '.!', 1),
}

cases = []
for name, token in tokens.items():
    try:
        claims = jwt.decode(token, SECRET, algorithms=['HS256'])
        result = {'ok': True, 'claims': claims}
    except jwt.ExpiredSignatureError as e:
        result = {'ok': False, 'expired': True, 'message': str(e)}
    except jwt.InvalidTokenError as e:
        result = {'ok': False, 'expired': False, 'message': str(e)}
    except Exception as e:  # escapes verify_token
        result = {'ok': False, 'crash': type(e).__name__, 'message': str(e)}
    cases.append({'name': name, 'token': token, 'result': result})

# The exact encoding of generate_token at a fixed time.
encoded = jwt.encode({'username': 'admin', 'exp': datetime(2030, 1, 2, 3, 4, 5) + timedelta(hours=24),
                      'iat': datetime(2030, 1, 2, 3, 4, 5)}, SECRET, algorithm='HS256')
unicode_encoded = jwt.encode({'username': '管理员', 'exp': 1900000000, 'iat': 1800000000}, SECRET, algorithm='HS256')
with open('server/internal/auth/testdata/pyjwt_cases.json', 'w') as handle:
    json.dump({'secret': SECRET, 'generated_at': NOW, 'cases': cases,
               'encode': {'token': encoded, 'unicode': unicode_encoded}}, handle, ensure_ascii=False, indent=1)
print(len(cases), 'cases; PyJWT', jwt.__version__)
