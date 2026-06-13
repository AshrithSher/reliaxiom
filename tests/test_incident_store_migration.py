"""TDD: a pre-M4 incidents.db (without remediation_loops/verify_started_at) must still load
after the schema grew — the store migrates missing columns instead of crashing."""
import sqlite3

from sre_agent.incident.lifecycle import IncidentState
from sre_agent.incident.store import IncidentStore


def test_loads_legacy_schema_db(tmp_path):
    path = tmp_path / "incidents.db"
    # simulate an old DB: the M2/M3 schema without the M4 columns
    conn = sqlite3.connect(str(path))
    conn.execute(
        "CREATE TABLE incidents (id TEXT PRIMARY KEY, fingerprint TEXT NOT NULL, "
        "ticket_id TEXT, state TEXT NOT NULL, root_service TEXT NOT NULL, services TEXT NOT NULL, "
        "fault_type TEXT NOT NULL, severity TEXT NOT NULL, first_seen TEXT NOT NULL, "
        "updated_at TEXT NOT NULL, resolved_at TEXT, flap_count INTEGER NOT NULL DEFAULT 0)")
    conn.execute(
        "INSERT INTO incidents VALUES ('INC-1','worker:silence','SRE-1','DETECTED','worker',"
        "'[\"worker\"]','silence','Medium','2026-06-12T12:00:00+00:00','2026-06-12T12:00:00+00:00',"
        "NULL,0)")
    conn.commit()
    conn.close()

    store = IncidentStore(path)            # must migrate, not crash
    inc = store.get("INC-1")
    assert inc is not None
    assert inc.state is IncidentState.DETECTED
    assert inc.remediation_loops == 0 and inc.verify_started_at is None
