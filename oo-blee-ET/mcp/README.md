# mcp/

`server.py` is the MCP server for the graph tools (official MCP Python SDK, stdio). It is started by
the orchestrator, one process per role. Run it by hand to inspect it:

    python mcp/server.py --role auditor --tenant acme --subject user_ravi

Do NOT add an `__init__.py` here: a top-level package named `mcp` would shadow the SDK.
