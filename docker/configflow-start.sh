#!/bin/sh
# Container entrypoint: optionally switch to the latest source, then start services.
# The update step is opt-in (CONFIGFLOW_AUTO_UPDATE=1) and never blocks startup.
python3 /usr/local/bin/configflow-auto-update.py || echo "[auto-update] skipped after an unexpected error"
python3 /usr/local/bin/configflow-web-listener.py prepare || exit 1
exec /usr/bin/supervisord -c /etc/supervisor/conf.d/supervisord.conf
