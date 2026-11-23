"""``virtuozzo-am-mcp`` — one-command MCP onboarding CLI for the Virtuozzo
Application Manager (Jelastic/VAP).

Subcommands (see ``virtuozzo-am-mcp --help``):

- ``login``   — authenticate against the platform (password or SSO browser
  flow), store the session in ``~/.virtuozzo-am/config.json`` (0600).
- ``install`` — detect the user's agent (Claude Code / Cursor / Hermes /
  generic) and write its MCP server config in one command; prints the
  equivalent manual one-liner.
- ``serve``   — stdio MCP bridge to the platform API (same tool surface as
  the ``mcp/`` server package).

The zero-install hosted variant needs no CLI at all: one
``claude mcp add --transport http ...`` line against the hosted ``/mcp``
endpoint (see the README).
"""

__version__ = "0.1.0"

__all__ = ["__version__"]
