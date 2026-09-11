# The floor of requires-python, deliberately not the newest: an image built on the oldest
# supported interpreter runs everywhere the classifiers claim. Moves in lockstep with
# requires-python, the classifiers, and the CI matrix.
FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    VIRTUAL_ENV=/opt/venv \
    PATH="/opt/venv/bin:$PATH"

WORKDIR /app

RUN python -m venv /opt/venv
COPY pyproject.toml README.md LICENSE /app/
COPY src /app/src
RUN pip install --no-cache-dir --upgrade pip && pip install --no-cache-dir .

# The daemon's port. Backends are stdio children of this process, so nothing else listens.
EXPOSE 8765

# Bind all interfaces: inside a container, loopback would be reachable only from the
# container itself. The daemon's own guard then REFUSES to start without an access key,
# which is the intended outcome -- run it with MCP_GATEWAY_WS_KEY set, or mount a
# gateway.env carrying WS_ACCESS_KEY.
ENTRYPOINT ["mcp-gateway", "--host", "0.0.0.0"]
