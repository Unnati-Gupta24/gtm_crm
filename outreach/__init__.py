"""Autonomous LinkedIn outreach CRM built on the local LinkedIn MCP server.

Copyright 2026 Yash Yadav
Licensed under the Apache License, Version 2.0.


Lifecycle: DISCOVERED -> INVITED -> CONNECTED -> CONTACTED -> REPLIED.

SQLite owns crash-safe state and exactly-once guarantees; Excel is the
human-readable log rebuilt from SQLite on every run.
"""

__all__ = ["__version__"]

__version__ = "1.0.0"
