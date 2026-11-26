CREATE TABLE IF NOT EXISTS rooms(
 id TEXT PRIMARY KEY, owner TEXT NOT NULL, chair TEXT NOT NULL, proof TEXT NOT NULL,
 title TEXT NOT NULL, objective TEXT NOT NULL, limits TEXT NOT NULL,
 round INTEGER NOT NULL DEFAULT 0, deadline REAL, closure TEXT);
CREATE TABLE IF NOT EXISTS agents(
 id TEXT PRIMARY KEY, owner TEXT NOT NULL, name TEXT NOT NULL, alias TEXT NOT NULL,
 profile TEXT NOT NULL, revision TEXT NOT NULL, instructions TEXT NOT NULL, limits TEXT NOT NULL,
 provider TEXT NOT NULL, deployment TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS members(
 room TEXT REFERENCES rooms, agent TEXT NOT NULL, handle TEXT NOT NULL, session TEXT NOT NULL UNIQUE,
 PRIMARY KEY(room,agent), UNIQUE(room,handle));
CREATE TABLE IF NOT EXISTS messages(
 id TEXT PRIMARY KEY, room TEXT NOT NULL, seq INTEGER NOT NULL, sender TEXT NOT NULL,
 parent TEXT, kind TEXT NOT NULL, envelope TEXT NOT NULL, UNIQUE(room,seq), UNIQUE(room,id),
 FOREIGN KEY(room,sender) REFERENCES members(room,agent),
 FOREIGN KEY(room,parent) REFERENCES messages(room,id));
CREATE TABLE IF NOT EXISTS jobs(
 id TEXT PRIMARY KEY, room TEXT NOT NULL, trigger TEXT NOT NULL, agent TEXT NOT NULL,
 round INTEGER NOT NULL, depth INTEGER NOT NULL, status TEXT NOT NULL, reason TEXT,
 created REAL NOT NULL, expires REAL, attempt TEXT, ledger TEXT,
 UNIQUE(trigger,agent), FOREIGN KEY(room,trigger) REFERENCES messages(room,id),
 FOREIGN KEY(room,agent) REFERENCES members(room,agent));
CREATE UNIQUE INDEX IF NOT EXISTS one_session_call ON jobs(room,agent)
 WHERE status IN ('dispatching','outcome_unknown');
CREATE TABLE IF NOT EXISTS receipts(
 owner TEXT, tool TEXT, key TEXT, digest TEXT NOT NULL, result TEXT NOT NULL,
 PRIMARY KEY(owner,tool,key));
CREATE TABLE IF NOT EXISTS desktop_members(
 room TEXT PRIMARY KEY, agent TEXT NOT NULL UNIQUE,
 FOREIGN KEY(room,agent) REFERENCES members(room,agent));
PRAGMA user_version=2;
