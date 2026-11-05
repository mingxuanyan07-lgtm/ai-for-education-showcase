"""Locked native journals and run-scoped, append-only reviewed claim memory."""
from contextlib import contextmanager
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import re
import sqlite3
import threading
import time
import uuid
from .verification import object_sha, validate_binding

_guard = threading.Lock()
_locks = {}


@contextmanager
def file_lock(path, timeout=10):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    with _guard:
        lock = _locks.setdefault(str(path.absolute()), threading.RLock())
    with lock:
        with path.with_name(path.name + ".lock").open("a+b") as stream:
            if stream.seek(0, 2) == 0:
                stream.write(b"0")
                stream.flush()
            deadline = time.monotonic() + timeout
            while True:
                try:
                    stream.seek(0)
                    if os.name == "nt":
                        import msvcrt
                        msvcrt.locking(stream.fileno(), msvcrt.LK_NBLCK, 1)
                    else:
                        import fcntl
                        fcntl.flock(stream, fcntl.LOCK_EX | fcntl.LOCK_NB)
                    break
                except OSError:
                    if time.monotonic() >= deadline: raise TimeoutError("Memory file lock timed out")
                    time.sleep(.02)
            try:
                yield
            finally:
                stream.seek(0)
                if os.name == "nt":
                    msvcrt.locking(stream.fileno(), msvcrt.LK_UNLCK, 1)
                else:
                    fcntl.flock(stream, fcntl.LOCK_UN)


def append_jsonl(path, payload):
    path = Path(path)
    text = json.dumps(payload, ensure_ascii=False, allow_nan=False) + "\n"
    with file_lock(path):
        with path.open("a", encoding="utf-8", newline="\n") as stream:
            stream.write(text)
            stream.flush()
            os.fsync(stream.fileno())


def read_jsonl(path):
    path = Path(path)
    with file_lock(path):
        lines = path.read_text(encoding="utf-8").splitlines() if path.exists() else []
    rows = []
    for number, line in enumerate(lines, 1):
        if not line.strip(): continue
        # Corrupt historical data is reported rather than silently discarded.
        try:
            row = json.loads(line)
        except ValueError as exc:
            raise ValueError(f"Corrupt memory line {number}: {path}") from exc
        if not isinstance(row, dict): raise ValueError(f"Non-object memory line {number}: {path}")
        rows.append(row)
    return rows


def update_meta(path, identity, changes=None):
    path = Path(path)
    with file_lock(path):
        current = json.loads(path.read_text(encoding="utf-8")) if path.exists() and path.stat().st_size else {}
        if not isinstance(current, dict): raise ValueError("Memory metadata is not an object")
        now = datetime.now(timezone.utc).isoformat()
        merged = {"created_at_utc": now, **current, **(changes or {}), **identity, "updated_at_utc": now}
        temporary = path.with_name(path.name + "." + uuid.uuid4().hex + ".tmp")
        try:
            with temporary.open("x", encoding="utf-8") as stream:
                json.dump(merged, stream, ensure_ascii=False, indent=2)
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temporary, path)
        finally:
            temporary.unlink(missing_ok=True)
    return merged


def tokenize(text):
    tokens = re.findall(r"[a-z0-9_]+", text.lower())
    for run in re.findall(r"[\u3400-\u9fff]+", text):
        tokens.extend(run)
        tokens.extend(run[i:i+2] for i in range(len(run)-1))
    return tokens


class ClaimMemory:
    """One database per run; model verdicts are evidence, never formal truth."""
    def __init__(self, path, identity):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.identity = identity
        with self.connect() as db:
            db.execute("CREATE TABLE IF NOT EXISTS identity (key TEXT PRIMARY KEY, value TEXT NOT NULL)")
            old = dict(db.execute("SELECT key,value FROM identity"))
            expected = {key: str(value) for key, value in identity.items()}
            if old and old != expected: raise ValueError("Memory belongs to a different run/question")
            db.executemany("INSERT OR IGNORE INTO identity VALUES (?,?)", expected.items())
            db.execute("CREATE TABLE IF NOT EXISTS reviews (seq INTEGER PRIMARY KEY AUTOINCREMENT, review_id TEXT UNIQUE NOT NULL, payload TEXT NOT NULL)")

    @contextmanager
    def connect(self):
        db = sqlite3.connect(self.path, timeout=10)
        try:
            db.execute("PRAGMA busy_timeout=10000")
            with db:
                yield db
        finally:
            db.close()

    def record(self, statement, proof, report, binding):
        validate_binding(binding, statement, proof, require_correct=False)
        if report != binding["report"]: raise ValueError("Memory report differs from binding")
        claims = []
        for claim in report["claim_checks"]["claims"]:
            state = ("rejected" if claim["status"] in ("error", "gap") else
                     "verified" if claim["status"] == "supported" and report["verdict"] == "correct" else "proposed")
            claims.append({**claim, "state": state,
                "claim_key": object_sha({"statement": claim["statement"], "assumptions": claim["assumptions"]})})
        payload = {"proof_sha256": binding["proof_sha256"], "report_sha256": binding["report_sha256"],
                   "verification_id": binding["verification_id"], "verdict": report["verdict"], "claims": claims}
        with self.connect() as db:
            db.execute("INSERT INTO reviews(review_id,payload) VALUES (?,?)", (binding["verification_id"], json.dumps(payload, ensure_ascii=False)))

    def snapshot(self):
        with self.connect() as db:
            history = [json.loads(row[0]) for row in db.execute("SELECT payload FROM reviews ORDER BY seq")]
        return {"identity": self.identity, "scope": "Current question/run only; verified means the bound model review passed, not Lean or common external review.",
                "current": history[-1] if history else None, "history": history,
                "reuse_policy": "Only current proof claims are active; earlier rejected/proposed versions remain historical evidence."}

    def search(self, query, state=None):
        if state not in (None, "proposed", "verified", "rejected"): raise ValueError("Unknown claim state")
        current = self.snapshot()["current"]
        if not current: return []
        terms = set(tokenize(query))
        results = []
        for claim in current["claims"]:
            if state and claim["state"] != state: continue
            score = len(terms & set(tokenize(claim["statement"] + " " + claim["reason"])))
            if score: results.append({**claim, "score": score, "proof_sha256": current["proof_sha256"]})
        return sorted(results, key=lambda row: row["score"], reverse=True)
