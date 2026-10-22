"""Additive, revisioned backend configuration on the existing SQLite connection."""
import json
import sqlite3
from .dictation_task_contract import validate_parameters, ContractError
from .dictation_task_contract import validate_parameters_v2
from .db_atomic import atomic, revision

class DictationSettings:
    def __init__(self, connection):
        self.db=connection
        self.db.execute('CREATE TABLE IF NOT EXISTS dictation_config (singleton INTEGER PRIMARY KEY CHECK(singleton=1), revision INTEGER NOT NULL, parameters TEXT NOT NULL)')
        self.db.execute('CREATE TABLE IF NOT EXISTS dictation_config_v2_revisions (revision INTEGER PRIMARY KEY, parameters TEXT NOT NULL)')

    def get_display(self):
        self.db.execute('CREATE TABLE IF NOT EXISTS dictation_display_settings(singleton INTEGER PRIMARY KEY CHECK(singleton=1),revision INTEGER NOT NULL,debug_enabled INTEGER NOT NULL)')
        row=self.db.execute('SELECT revision,debug_enabled FROM dictation_display_settings WHERE singleton=1').fetchone()
        return dict(revision=row[0],debug_enabled=bool(row[1])) if row else dict(revision=0,debug_enabled=False)

    def save_display(self, debug_enabled, *, expected_revision):
        revision(expected_revision)
        if type(debug_enabled) is not bool:raise ContractError('debug_enabled: expected boolean')
        with atomic(self.db):
            current=self.get_display()
            if current['revision']!=expected_revision:raise ContractError('revision: conflict')
            self.db.execute('INSERT OR REPLACE INTO dictation_display_settings VALUES(1,?,?)',(expected_revision+1,int(debug_enabled)))
        return self.get_display()

    def get_v2(self, revision=None):
        if revision is None:
            row=self.db.execute('SELECT revision,parameters FROM dictation_config_v2_revisions ORDER BY revision DESC LIMIT 1').fetchone()
        else:
            if type(revision) is not int or revision<1:raise ContractError('revision: invalid revision')
            row=self.db.execute('SELECT revision,parameters FROM dictation_config_v2_revisions WHERE revision=?',(revision,)).fetchone()
        return None if row is None else {'revision':row[0],'parameters':json.loads(row[1])}

    def save_v2(self, parameters, *, expected_revision):
        revision(expected_revision)
        if type(parameters) is not dict or not {'phases','playback'}<=set(parameters) or set(parameters)-{'phases','playback','tts'}:
            raise ContractError('parameters: expected phases and playback')
        phases=parameters['phases']
        if type(phases) is not list or not phases or any(type(p) is not str or p not in ('recognition','dictation') for p in phases) or len(set(phases))!=len(phases):
            raise ContractError('phases: expected ordered enabled subset')
        checked={'phases':list(phases),'playback':validate_parameters_v2(parameters['playback'])}
        if 'tts' in parameters:
            from .dictation_cloud_tts import validate_tts
            checked['tts']=validate_tts(parameters['tts'])
        with atomic(self.db):
            latest=self.get_v2()
            if (latest['revision'] if latest else 0)!=expected_revision:raise ContractError('revision: conflict')
            self.db.execute('INSERT INTO dictation_config_v2_revisions VALUES (?,?)',(expected_revision+1,json.dumps(checked,ensure_ascii=False)))
        return self.get_v2(expected_revision+1)

    def get(self):
        row=self.db.execute('SELECT revision,parameters FROM dictation_config WHERE singleton=1').fetchone()
        return None if row is None else {'revision':row[0],'parameters':validate_parameters(json.loads(row[1]))}

    def save(self, parameters, *, expected_revision):
        checked=validate_parameters(parameters)
        if type(expected_revision) is not int or expected_revision<0:raise ContractError('revision: invalid revision')
        with atomic(self.db):
            if expected_revision==0:
                try:self.db.execute('INSERT INTO dictation_config VALUES (1,1,?)',(json.dumps(checked,ensure_ascii=False),))
                except sqlite3.IntegrityError as exc:
                    raise ContractError('revision: conflict') from exc
            else:
                cursor=self.db.execute('UPDATE dictation_config SET revision=revision+1,parameters=? WHERE singleton=1 AND revision=?',(json.dumps(checked,ensure_ascii=False),expected_revision))
                if cursor.rowcount!=1:raise ContractError('revision: conflict')
        return {'revision':expected_revision+1,'parameters':checked}
