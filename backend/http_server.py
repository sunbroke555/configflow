"""Serve production requests on a reusable, bounded pool of worker threads."""
import os


DEFAULT_HTTP_THREADS = 8


def server_options():
    """Keep transport behavior compatible with the existing nginx frontend."""
    raw_threads = os.environ.get('CONFIGFLOW_HTTP_THREADS', str(DEFAULT_HTTP_THREADS))
    try:
        threads = int(raw_threads)
    except ValueError:
        raise ValueError('CONFIGFLOW_HTTP_THREADS must be an integer between 1 and 64') from None
    if not 1 <= threads <= 64:
        raise ValueError('CONFIGFLOW_HTTP_THREADS must be an integer between 1 and 64')
    return {
        'host': '0.0.0.0',
        'port': 5001,
        'threads': threads,
        'channel_timeout': 300,
        # Routes already interpret these headers (including deployments behind
        # an external proxy). Do not silently remove or reinterpret them here.
        'clear_untrusted_proxy_headers': False,
    }


def run_http_server(app, *, debug=False):
    from backend.utils.local_http import register_application
    register_application(app)
    if debug:
        # Keep the local debugger/reloader and its existing background-loop
        # guard in app.py. Production never uses Werkzeug's per-request threads.
        app.run(host='0.0.0.0', port=5001, debug=True)
        return

    from waitress import serve
    from backend.http_queue import QueueWaitDispatcher

    options = server_options()
    dispatcher = QueueWaitDispatcher()
    try:
        dispatcher.set_thread_count(options['threads'])
        serve(app, _dispatcher=dispatcher, **options)
    finally:
        dispatcher.shutdown()
