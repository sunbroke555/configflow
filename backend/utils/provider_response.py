"""Answer provider downloads within the client's timeout.

Mihomo gives up on a proxy-provider download after 20 seconds and keeps its
previous copy. Rendering one provider can re-render the whole provider bundle
(dialer validation) and wait on remote subscriptions: a 30 s direct fetch, then
Sub-Store, which may first have to start on demand. Refreshes could therefore
fail every time, so node changes never reached Mihomo.

Rendering runs in a shared single flight per provider and configuration. After
RESPONSE_SECONDS the last content successfully served for that provider is
returned instead, and the flight finishes in the background, so the next
refresh is fresh. That is never worse than the timeout it replaces: Mihomo
would keep an even older copy. Only the Mihomo format falls back; Surge/Loon
requests wait as before.
"""
from concurrent.futures import ThreadPoolExecutor, TimeoutError as FutureTimeout
import hashlib
import json
import threading

from flask import Response, current_app, request

from backend.utils.logger import get_logger

logger = get_logger(__name__)

RESPONSE_SECONDS = 12
# Mihomo refreshes every provider at once; none should queue behind another.
_pool = ThreadPoolExecutor(max_workers=8, thread_name_prefix='provider-render')
_flights = {}
_flights_lock = threading.Lock()


def _last_good_path(kind, item_id):
    return f'providers/served/{kind}-{item_id}.yaml'


def _read_last_good(profile_id, kind, item_id):
    from backend.common.config import get_repository
    # Before anything was served (new install, upgrade), an aggregation still
    # has the validated artifact its last bundle commit wrote.
    paths = [_last_good_path(kind, item_id)] + ([f'providers/{item_id}.yaml'] if kind == 'aggregation' else [])
    for relative in paths:
        try:
            return get_repository().profile_path(profile_id, relative).read_text(encoding='utf-8')
        except OSError:
            continue
    return None


def _copy(response):
    # One rendered response may answer several waiting requests.
    return Response(response.get_data(), status=response.status_code, headers=list(response.headers))


def serve_provider(kind, item_id, profile_id, config_data, render):
    """Return ``render()``'s response, or the last good content once the client would time out.

    Call after authentication and existence checks: the fallback must never
    answer a request that would otherwise be rejected.
    """
    fallback = not request.args.get('format')
    digest = hashlib.sha256(json.dumps(config_data, sort_keys=True, default=str).encode()).hexdigest()
    key = (kind, profile_id, item_id, request.args.get('format') or '', digest)
    app = current_app._get_current_object()
    environ = dict(request.environ)

    def run():
        # The render may outlive this request; give it a context of its own.
        with app.request_context(environ):
            response = render()
            if fallback and response.status_code == 200 and response.mimetype == 'text/yaml':
                from backend.common.config import get_repository
                get_repository().write_profile_text(profile_id, _last_good_path(kind, item_id),
                                                    response.get_data(as_text=True))
            return response

    created = False
    with _flights_lock:
        future = _flights.get(key)
        if future is None:
            future = _pool.submit(run)
            _flights[key] = future
            created = True
    # Completed futures run callbacks immediately, so _forget must be able to
    # acquire the lock even when rendering finished before registration.
    if created:
        future.add_done_callback(lambda done, key=key: _forget(key, done))
    try:
        return _copy(future.result(timeout=RESPONSE_SECONDS if fallback else None))
    except FutureTimeout:
        cached = _read_last_good(profile_id, kind, item_id)
        if cached is None:
            return _copy(future.result())
        logger.warning('Provider %s/%s 生成超过 %d 秒，先返回上次成功内容，后台继续生成',
                       kind, item_id, RESPONSE_SECONDS)
        return Response(cached, mimetype='text/yaml', headers={'X-ConfigFlow-Provider': 'last-good'})


def _forget(key, future):
    with _flights_lock:
        if _flights.get(key) is future:
            del _flights[key]
