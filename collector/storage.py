import hashlib
import json
import sqlite3
from contextlib import contextmanager


class Store:
    def __init__(self, path):
        self.connection = sqlite3.connect(path, timeout=10, isolation_level=None)
        self.connection.row_factory = sqlite3.Row
        self.connection.execute("PRAGMA foreign_keys=ON")
        self.connection.execute("PRAGMA journal_mode=WAL")
        self.connection.executescript("""
            CREATE TABLE IF NOT EXISTS raw_objects(
                digest TEXT PRIMARY KEY, payload BLOB NOT NULL);
            CREATE TABLE IF NOT EXISTS imports(
                id INTEGER PRIMARY KEY, source TEXT NOT NULL,
                digest TEXT NOT NULL REFERENCES raw_objects(digest),
                UNIQUE(source,digest));
            CREATE TABLE IF NOT EXISTS records(
                record_key TEXT PRIMARY KEY, payload TEXT NOT NULL,
                import_id INTEGER NOT NULL REFERENCES imports(id));
            CREATE TABLE IF NOT EXISTS aliases(
                old TEXT NOT NULL, new TEXT NOT NULL, effective INTEGER NOT NULL,
                PRIMARY KEY(old, effective));
            CREATE TABLE IF NOT EXISTS decisions(
                id INTEGER PRIMARY KEY, type TEXT NOT NULL, payload TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS human_edges(
                left_key TEXT NOT NULL, right_key TEXT NOT NULL,
                decision_id INTEGER NOT NULL REFERENCES decisions(id),
                PRIMARY KEY(left_key, right_key));
            CREATE TABLE IF NOT EXISTS separations(
                left_key TEXT NOT NULL, right_key TEXT NOT NULL,
                decision_id INTEGER NOT NULL REFERENCES decisions(id),
                PRIMARY KEY(left_key, right_key));
            CREATE TABLE IF NOT EXISTS entities(
                id INTEGER PRIMARY KEY, resource TEXT NOT NULL UNIQUE,
                status TEXT NOT NULL CHECK(status IN ('active','retired')),
                version INTEGER NOT NULL, members TEXT NOT NULL,
                evidence TEXT NOT NULL, attribution TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS entity_events(
                id INTEGER PRIMARY KEY, resource TEXT NOT NULL,
                version INTEGER NOT NULL, action TEXT NOT NULL, detail TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS membership_events(
                id INTEGER PRIMARY KEY, record_key TEXT NOT NULL,
                resource TEXT NOT NULL, action TEXT NOT NULL, detail TEXT NOT NULL);
        """)

    @contextmanager
    def transaction(self):
        self.connection.execute("BEGIN IMMEDIATE")
        try:
            yield
        except BaseException:
            self.connection.rollback()
            raise
        else:
            self.connection.commit()

    def ingest(self, source, raw, records, aliases=()):
        """登记原文与当前记录，返回原文摘要。兼容旧调用方。"""
        with self.transaction():
            return self.ingest_locked(source, raw, records, aliases)[0]

    def ingest_locked(self, source, raw, records, aliases=()):
        """在调用方持有的事务内登记；携带 observed 的记录拒绝被更旧的快照覆盖。

        返回 (digest, skipped)：skipped 为因观察时间更旧而被保留现状的记录键，
        其原文与导入凭据仍完整登记，证据不丢失。
        """
        digest = hashlib.sha256(raw).hexdigest()
        skipped = []
        self.connection.execute("INSERT OR IGNORE INTO raw_objects VALUES (?,?)", (digest, raw))
        self.connection.execute("INSERT OR IGNORE INTO imports(source,digest) VALUES (?,?)", (source, digest))
        import_id = self.connection.execute("SELECT id FROM imports WHERE source=? AND digest=?", (source, digest)).fetchone()[0]
        for alias in aliases:
            self.connection.execute("INSERT OR REPLACE INTO aliases VALUES (?,?,?)",
                (alias['old'], alias['new'], int(alias['effective'])))
        for record in records:
            key = record.get('key')
            if not isinstance(key, str) or not key:
                raise ValueError('记录缺少有效标识')
            observed = record.get('observed')
            if observed is not None:
                row = self.connection.execute("SELECT payload FROM records WHERE record_key=?", (key,)).fetchone()
                if row is not None:
                    current = json.loads(row[0]).get('observed')
                    if current is not None and int(observed) < int(current):
                        skipped.append(key)
                        continue
            encoded = json.dumps(record, ensure_ascii=False, sort_keys=True, allow_nan=False)
            self.connection.execute("INSERT INTO records VALUES (?,?,?) ON CONFLICT(record_key) DO UPDATE SET payload=excluded.payload, import_id=excluded.import_id", (key, encoded, import_id))
        return digest, skipped

    def records(self):
        return [json.loads(row[0]) for row in self.connection.execute("SELECT payload FROM records ORDER BY record_key")]

    def provenance(self, key):
        row = self.connection.execute("SELECT imports.source, imports.digest FROM records JOIN imports ON imports.id=records.import_id WHERE record_key=?", (key,)).fetchone()
        return dict(row) if row else None

    # ---- 机构别名 ----

    def aliases(self):
        return [dict(row) for row in self.connection.execute("SELECT old, new, effective FROM aliases ORDER BY old, effective")]

    # ---- 人工决定与归并约束 ----

    def add_decision(self, dtype, payload):
        cursor = self.connection.execute("INSERT INTO decisions(type, payload) VALUES (?,?)",
            (dtype, json.dumps(payload, ensure_ascii=False, sort_keys=True)))
        return cursor.lastrowid

    def decisions(self):
        return [dict(id=row['id'], type=row['type'], payload=json.loads(row['payload']))
            for row in self.connection.execute("SELECT * FROM decisions ORDER BY id")]

    def human_edges(self):
        return [(row['left_key'], row['right_key'], row['decision_id'])
                for row in self.connection.execute("SELECT left_key, right_key, decision_id FROM human_edges")]

    def add_human_edges(self, pairs, decision_id):
        for left, right in pairs:
            self.connection.execute("INSERT OR IGNORE INTO human_edges VALUES (?,?,?)", (left, right, decision_id))

    def remove_human_edges_among(self, keys):
        marks = ','.join('?' * len(keys))
        self.connection.execute(
            f"DELETE FROM human_edges WHERE left_key IN ({marks}) AND right_key IN ({marks})", (*keys, *keys))

    def separations(self):
        return [(row['left_key'], row['right_key'], row['decision_id'])
                for row in self.connection.execute("SELECT left_key, right_key, decision_id FROM separations")]

    def add_separations(self, pairs, decision_id):
        for left, right in pairs:
            self.connection.execute("INSERT OR IGNORE INTO separations VALUES (?,?,?)", (left, right, decision_id))

    def remove_separations_among(self, keys):
        marks = ','.join('?' * len(keys))
        self.connection.execute(
            f"DELETE FROM separations WHERE left_key IN ({marks}) AND right_key IN ({marks})", (*keys, *keys))

    # ---- 实体与版本 ----

    def entities(self, status=None):
        sql = "SELECT * FROM entities"
        args = ()
        if status:
            sql += " WHERE status=?"
            args = (status,)
        return [dict(id=row['id'], resource=row['resource'], status=row['status'], version=row['version'],
            members=json.loads(row['members']), evidence=json.loads(row['evidence']),
            attribution=json.loads(row['attribution']))
            for row in self.connection.execute(sql + " ORDER BY id", args)]

    def entity(self, resource):
        row = self.connection.execute("SELECT * FROM entities WHERE resource=?", (resource,)).fetchone()
        if row is None:
            return None
        return dict(id=row['id'], resource=row['resource'], status=row['status'], version=row['version'],
            members=json.loads(row['members']), evidence=json.loads(row['evidence']),
            attribution=json.loads(row['attribution']))

    def create_entity(self, members, evidence, attribution):
        cursor = self.connection.execute(
            "INSERT INTO entities(resource, status, version, members, evidence, attribution)"
            " VALUES ((SELECT 'resource-' || (COALESCE(MAX(id),0) + 1) FROM entities), 'active', 1, ?,?,?)",
            (json.dumps(members, ensure_ascii=False), json.dumps(evidence, ensure_ascii=False),
             json.dumps(attribution, ensure_ascii=False)))
        return self.connection.execute("SELECT resource FROM entities WHERE id=?", (cursor.lastrowid,)).fetchone()[0]

    def update_entity(self, resource, members, evidence, attribution):
        self.connection.execute(
            "UPDATE entities SET version=version+1, members=?, evidence=?, attribution=? WHERE resource=?",
            (json.dumps(members, ensure_ascii=False), json.dumps(evidence, ensure_ascii=False),
             json.dumps(attribution, ensure_ascii=False), resource))
        return self.entity(resource)['version']

    def retire_entity(self, resource):
        self.connection.execute("UPDATE entities SET status='retired' WHERE resource=?", (resource,))

    def add_entity_event(self, resource, version, action, detail):
        self.connection.execute("INSERT INTO entity_events(resource, version, action, detail) VALUES (?,?,?,?)",
            (resource, version, action, json.dumps(detail, ensure_ascii=False, sort_keys=True)))

    def entity_events(self, resource=None):
        sql = "SELECT * FROM entity_events"
        args = ()
        if resource:
            sql += " WHERE resource=?"
            args = (resource,)
        return [dict(id=row['id'], resource=row['resource'], version=row['version'],
            action=row['action'], detail=json.loads(row['detail']))
            for row in self.connection.execute(sql + " ORDER BY id", args)]

    def add_membership_event(self, record_key, resource, action, detail):
        self.connection.execute("INSERT INTO membership_events(record_key, resource, action, detail) VALUES (?,?,?,?)",
            (record_key, resource, action, json.dumps(detail, ensure_ascii=False, sort_keys=True)))

    def membership_history(self, record_key):
        return [dict(id=row['id'], resource=row['resource'], action=row['action'], detail=json.loads(row['detail']))
            for row in self.connection.execute("SELECT * FROM membership_events WHERE record_key=? ORDER BY id", (record_key,))]

    def close(self):
        self.connection.close()
