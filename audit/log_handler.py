"""A logging handler that files the worker's warnings and errors with the audit.

It goes on the root logger, which is the one place records from the scraper, the
analyzer, the compliance package and the API client all arrive at — so none of them has
to know the audit exists. It adds a destination and replaces none: what the worker
prints on the terminal is exactly what it printed before.
"""

import logging
from collections.abc import Callable

# The hierarchy the audit logs its own failures under, and the reason this handler has a
# filter at all: filing those would let a broken outbox feed on itself, since each failed
# write logs a warning, which is a record, which is another failed write.
_OWN_HIERARCHY = "audit"


class AuditLogHandler(logging.Handler):
    """Hands every warning and error to a sink, and nothing anywhere else.

    What a record becomes is the sink's business — the row has to carry the run and the
    work in hand, which only the recorder knows — so this class is just the part logging
    insists on: a level, one filter, and an emit that cannot misbehave.
    """

    def __init__(self, sink: Callable[[logging.LogRecord], None]) -> None:
        super().__init__(level=logging.WARNING)
        self._sink = sink

    def emit(self, record: logging.LogRecord) -> None:
        """File the record, or let it go. Never raises and never prints."""
        if record.name == _OWN_HIERARCHY or record.name.startswith(f"{_OWN_HIERARCHY}."):
            return
        try:
            self._sink(record)
        except Exception:
            # Swallowed rather than handed to handleError(), which writes the failure to
            # stderr. The worker's output is something a person reads while a run goes,
            # and an audit that cannot write must not start writing there instead.
            pass
