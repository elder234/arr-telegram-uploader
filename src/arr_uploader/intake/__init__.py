"""Job intake.

Three independent paths feed the queue, all converging on the same idempotent
``Store.upsert_job`` keyed by folder path:

* :mod:`.inbox` -- Radarr's Custom Script atomically drops a JSON file;
* :mod:`.webhook` -- optional HTTP endpoint, secret-checked;
* :mod:`.reconciler` -- periodic sweep that recovers jobs whose intake was lost.

Multiple paths firing for one movie is expected and harmless.
"""

from .inbox import InboxWatcher, read_job_file
from .reconciler import Reconciler

__all__ = ["InboxWatcher", "Reconciler", "read_job_file"]