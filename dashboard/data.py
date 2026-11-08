"""Picks the dashboard's data source. The pages always do `from data import ...`.

    ONCALL_DATA=real  -> data_real.py  (live system: CloudWatch, DynamoDB, scripts/faults.py; server only)
    anything else     -> data_fake.py  (made-up data; works on any laptop)

Both files have the same 7 functions with the same return shapes.
"""
import os

if os.getenv("ONCALL_DATA", "fake").lower() == "real":
    from data_real import (  # noqa: F401
        approve, get_health, get_history, get_incidents, reject, reset, trigger_fault,
    )
    MODE = "real"
else:
    from data_fake import (  # noqa: F401
        approve, get_health, get_history, get_incidents, reject, reset, trigger_fault,
    )
    MODE = "fake"
