# The floor of requires-python, deliberately not the newest: an image built on the oldest
# supported interpreter runs everywhere the classifiers claim. Moves in lockstep with
# requires-python, the classifiers, and the CI matrix.
FROM python:3.12-slim

# Bind all interfaces by default: inside a container, loopback would be reachable only from
# the container itself. The daemon's own guard then REFUSES to start without an access key,
# which is the intended outcome -- run it with MCP_GATEWAY_WS_KEY set, or put WS_ACCESS_KEY
# in the mounted gateway.env.
#
# The exception is remote mode, the far end of the desktop app's SSH tunnel: there the
# container runs with host networking and MCP_GATEWAY_HOST=127.0.0.1, keyless, exactly like
# the systemd unit in GET_STARTED.md. See examples/remote-compose/.
#
# An environment variable rather than an ENTRYPOINT flag, so a compose file can change it
# without replacing the entrypoint.
ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    VIRTUAL_ENV=/opt/venv \
    PATH="/opt/venv/bin:$PATH" \
    MCP_GATEWAY_HOST=0.0.0.0 \
    MCP_GATEWAY_CONFIG=/config/servers.yaml

WORKDIR /app

RUN python -m venv /opt/venv
COPY pyproject.toml README.md LICENSE /app/
COPY src /app/src
RUN pip install --no-cache-dir --upgrade pip && pip install --no-cache-dir .

# servers.yaml and gateway.env, beside each other as they are everywhere else. A directory
# rather than two files, and writable: the admin UI saves servers.yaml through
# config_writer.py, which writes a .bak next to it and replaces the file atomically.
VOLUME /config

# Drop root. The daemon needs no privilege it would have as root: it binds 8765, which is
# above 1024, reads /config and writes servers.yaml back into it, and spawns backends that
# want the same account, not a stronger one.
#
# What running as root actually cost, before this line: every save from the admin UI landed
# in the bind-mounted ./config as root:root, so the account that laid the directory out
# could no longer hand-edit its own catalogue -- and the editor that refused to save was a
# long way from the UI click that caused it.
#
# A fixed uid rather than a name, because the number is what a bind mount compares. 1000 is
# the first ordinary account on a Debian or Ubuntu host and is unused in python:3.12-slim,
# so the common case -- a single-user box, a checkout owned by that user -- lines up with no
# configuration at all. Any other host sets `user:` in compose, which is what
# examples/remote-compose/compose.yaml does; overriding it needs nothing from this image,
# since every path the daemon reads is world-readable and only /config must be writable.
# Not `--system`: that reserves a uid below SYS_UID_MAX (999 on Debian) and warns when
# handed 1000, and the whole point of the number is that it is the *ordinary* first account.
RUN useradd --uid 1000 --user-group --no-create-home gateway
USER gateway

# The daemon's port. Backends in a container are usually `url:` entries -- other containers
# on their own ports -- and a `command:` backend needs its runtime (node, uv) added to an
# image built FROM this one; this one carries Python and nothing else.
EXPOSE 8765

ENTRYPOINT ["mcp-gateway"]
