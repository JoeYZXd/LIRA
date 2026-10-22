"""Labelled matching evidence for later calibration, never template training."""
import json
import time
import uuid

import numpy as np

CANDIDATE_THRESHOLD = .5
POLICY_VERSION = 'candidate-confirmation-v1'


class IdentityJournal:
    def __init__(self, db):
        self.db=db
        db.executescript('''
            CREATE TABLE IF NOT EXISTS identity_match_events(
                id TEXT PRIMARY KEY,created REAL NOT NULL,source TEXT NOT NULL,
                session_id TEXT,turn_number INTEGER,selected_member_id INTEGER,
                model TEXT NOT NULL,details TEXT NOT NULL,scores TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS identity_match_feedback(
                id INTEGER PRIMARY KEY AUTOINCREMENT,event_id TEXT NOT NULL,
                created REAL NOT NULL,decision TEXT NOT NULL,member_id INTEGER,
                against_member_id INTEGER,source TEXT NOT NULL,reported_name TEXT);
            CREATE INDEX IF NOT EXISTS identity_feedback_event ON identity_match_feedback(event_id,id);
        ''')
        if 'reported_name' not in {r[1] for r in db.execute('PRAGMA table_info(identity_match_feedback)')}:
            db.execute('ALTER TABLE identity_match_feedback ADD COLUMN reported_name TEXT')
            db.commit()

    @staticmethod
    def ranking(identity, vector):
        return sorted([{'member_id':mid,'score':float(np.dot(vector,v))}
                       for mid,v in identity.memory.profiles()
                       if v.shape==vector.shape and np.isfinite(v).all()],key=lambda r:r['score'],reverse=True)

    def record(self, identity, vector, details, *, source, session_id=None, turn=0, selected_member_id=None):
        event_id=uuid.uuid4().hex
        safe={key:value for key,value in details.items() if isinstance(value,(str,int,float,bool)) or value is None}
        safe.update(policy=POLICY_VERSION,candidate_threshold=CANDIDATE_THRESHOLD)
        model=identity.model_fingerprint
        path=getattr(identity,'model_path',None)
        if path is not None:
            try:
                from pathlib import Path
                stat=Path(path).stat();safe.update(model_size=stat.st_size,model_mtime=stat.st_mtime)
            except OSError:pass
        scores=self.ranking(identity,vector)[:100] if vector is not None else []
        with self.db:
            self.db.execute('INSERT INTO identity_match_events VALUES (?,?,?,?,?,?,?,?,?)',
                (event_id,time.time(),source,session_id,turn,selected_member_id,model,json.dumps(safe,allow_nan=False),json.dumps(scores,allow_nan=False)))
            # Preserve labelled evidence for future calibration even when the
            # rolling, unlabelled diagnostic history has reached its limit.
            old=[r[0] for r in self.db.execute('''SELECT e.id FROM identity_match_events e
                WHERE NOT EXISTS(SELECT 1 FROM identity_match_feedback f WHERE f.event_id=e.id)
                ORDER BY e.created DESC LIMIT -1 OFFSET 5000''')]
            for key in old:
                self.db.execute('DELETE FROM identity_match_feedback WHERE event_id=?',(key,))
                self.db.execute('DELETE FROM identity_match_events WHERE id=?',(key,))
        return event_id

    def feedback(self,event_id,decision,member_id,against_member_id,source,reported_name=None):
        if decision not in ('confirmed','rejected','corrected'):
            raise ValueError('反馈类型无效。')
        if not self.db.execute('SELECT 1 FROM identity_match_events WHERE id=?',(event_id,)).fetchone():
            raise ValueError('匹配记录已不存在。')
        with self.db:
            self.db.execute('INSERT INTO identity_match_feedback(event_id,created,decision,member_id,against_member_id,source,reported_name) VALUES (?,?,?,?,?,?,?)',
                (event_id,time.time(),decision,member_id,against_member_id,source,reported_name))

    def recent(self):
        rows=[]
        for row in self.db.execute('SELECT * FROM identity_match_events ORDER BY created DESC LIMIT 50'):
            data=dict(row);data['details']=json.loads(data['details']);data['scores']=json.loads(data['scores'])
            f=self.db.execute('SELECT * FROM identity_match_feedback WHERE event_id=? ORDER BY id DESC LIMIT 1',(data['id'],)).fetchone()
            data['feedback']=dict(f) if f else None;rows.append(data)
        counts={r['decision']:r['n'] for r in self.db.execute('SELECT decision,COUNT(*) n FROM identity_match_feedback WHERE id IN (SELECT MAX(id) FROM identity_match_feedback GROUP BY event_id) GROUP BY decision')}
        return {'events':rows,'labels':counts,'policy':POLICY_VERSION,'candidate_threshold':CANDIDATE_THRESHOLD,
                'note':'仅积累分数和用户标签，未自动调整门槛或声纹模板。'}
