"""Single authoritative version for Conductor.

Everything else derives from this:
  - app.py            : VERSION (and the auto-update GitHub release comparison)
  - server.py         : the MCP server's advertised "version"
  - pyproject.toml    : [tool.setuptools.dynamic] version = {attr = "_version.__version__"}

Keep this a plain string literal so setuptools can read it statically
without importing the module (which would pull in Flask at build time).
"""

__version__ = "4.0.0"
