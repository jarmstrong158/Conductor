# Container image for the Conductor MCP server (server.py).
#
# Exists so registries and sandboxes (e.g. Glama) can build and introspect this
# server rather than inferring an image. Introspection needs the server to
# start and answer tools/list, which is pure protocol.
#
# Note this image is the MCP SERVER only, not the Conductor app. server.py is a
# thin client over Conductor's local REST API, so tools/list works standalone
# but tool CALLS need that API reachable at CONDUCTOR_BASE_URL. Point it at a
# running Conductor instance to make the tools functional.
#
# Only `mcp` is installed, not requirements.txt: server.py imports nothing but
# the stdlib and the MCP SDK, while requirements.txt describes the full desktop
# app (flask, apscheduler, pystray) whose GUI dependencies have no place in a
# headless image.

FROM python:3.12-slim

WORKDIR /app

RUN pip install --no-cache-dir "mcp>=2.0,<3"

COPY . .

# Unbuffered: stdio IS the transport, so a buffered reply looks like a hang.
ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1

CMD ["python", "server.py"]
