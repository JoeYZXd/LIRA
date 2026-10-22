"""Durable dictation receipts. Callers supply authenticated device and frozen owner.

receive events are a contiguous TV-owned sequence starting at one. ACK means durable
receipt, not necessarily applied. Local cancellation remains requested until TV emits
its ordered cancelled event. No model-facing counter mutation exists.
"""
from __future__ import annotations
import json
from contextlib import contextmanager
from lira.dictation_task_contract import validate_task,canonical_json

KINDS=frozenset(('created','started','progress','paused','resumed','lesson_dictation_completed','recitation_completed','task_completed','cancelled','failed'))
TERMINAL=frozenset(('task_completed','cancelled','failed'))
FIELDS=frozenset(('task_id','event_id','run_epoch','sequence','state_revision','kind','lesson_id','content_hash'))

class RepositoryError(ValueError):pass

class DictationRepository:
 def __init__(self,conn):
  self.db=conn
  schema='''
  CREATE TABLE IF NOT EXISTS dictation_owner_epochs(owner_id INTEGER PRIMARY KEY,epoch INTEGER NOT NULL);
  CREATE TABLE IF NOT EXISTS dictation_tasks(task_id TEXT PRIMARY KEY,digest TEXT NOT NULL,snapshot TEXT,device TEXT NOT NULL,owner_id INTEGER,owner_epoch INTEGER NOT NULL,run_epoch INTEGER NOT NULL,last_sequence INTEGER NOT NULL DEFAULT 0,state_revision INTEGER NOT NULL DEFAULT 0,state TEXT NOT NULL DEFAULT 'prepared',revoked INTEGER NOT NULL DEFAULT 0,cancel_requested INTEGER NOT NULL DEFAULT 0);
  CREATE TABLE IF NOT EXISTS dictation_lesson_runs(task_id TEXT NOT NULL,lesson_id TEXT NOT NULL,book_id TEXT NOT NULL,unit INTEGER NOT NULL,number INTEGER NOT NULL,revision INTEGER NOT NULL,state TEXT NOT NULL DEFAULT 'pending',PRIMARY KEY(task_id,lesson_id));
  CREATE TABLE IF NOT EXISTS dictation_event_inbox(task_id TEXT NOT NULL,event_id TEXT NOT NULL,sequence INTEGER NOT NULL,payload TEXT NOT NULL,applied INTEGER NOT NULL DEFAULT 0,PRIMARY KEY(task_id,event_id),UNIQUE(task_id,sequence));
  CREATE TABLE IF NOT EXISTS dictation_completion_ledger(task_id TEXT NOT NULL,book_id TEXT NOT NULL,unit INTEGER NOT NULL,number INTEGER NOT NULL,owner_id INTEGER NOT NULL,revision INTEGER NOT NULL,PRIMARY KEY(task_id,book_id,unit,number));
  CREATE TABLE IF NOT EXISTS dictation_word_history(task_id TEXT NOT NULL,book_id TEXT NOT NULL,unit INTEGER NOT NULL,number INTEGER NOT NULL,owner_id INTEGER NOT NULL,words TEXT NOT NULL,PRIMARY KEY(task_id,book_id,unit,number));
  '''
  schema += '''CREATE TABLE IF NOT EXISTS dictation_v2_commands(task_id TEXT NOT NULL,command_id TEXT NOT NULL,payload TEXT NOT NULL,result TEXT,PRIMARY KEY(task_id,command_id));CREATE TABLE IF NOT EXISTS dictation_v2_actual(task_id TEXT PRIMARY KEY,payload TEXT NOT NULL);'''
  for statement in schema.split(';'):
   if statement.strip():conn.execute(statement)
 @contextmanager
 def _transaction(self):
  self.db.execute('SAVEPOINT dictation_repository')
  try:
   yield
   self.db.execute('RELEASE SAVEPOINT dictation_repository')
  except BaseException:
   self.db.execute('ROLLBACK TO SAVEPOINT dictation_repository');self.db.execute('RELEASE SAVEPOINT dictation_repository');raise
 def _task(self,tid):
  cur=self.db.execute('SELECT * FROM dictation_tasks WHERE task_id=?',(tid,));row=cur.fetchone()
  if row is None:raise RepositoryError('unknown task')
  return dict(zip((d[0] for d in cur.description),row))
 def submit(self,task,*,paired_device_id,owner_id=None,run_epoch=1):
  task=validate_task(task)
  if not isinstance(paired_device_id,str) or not paired_device_id:raise RepositoryError('paired device required')
  if owner_id is not None and (type(owner_id) is not int or owner_id<1):raise RepositoryError('invalid owner')
  if type(run_epoch) is not int or run_epoch<1:raise RepositoryError('invalid epoch')
  stable=[(x['source']['book_id'],x['source']['unit'],x['source']['number']) for x in task['lessons']]
  if len(set(stable))!=len(stable):raise RepositoryError('duplicate stable lesson')
  with self._transaction():
   exists=self.db.execute('SELECT task_id FROM dictation_tasks WHERE task_id=?',(task['task_id'],)).fetchone()
   if exists:
    old=self._task(task['task_id'])
    if (old['digest'],old['device'],old['owner_id'],old['run_epoch'])!=(task['content_hash'],paired_device_id,owner_id,run_epoch):raise RepositoryError('task conflict')
    return self.status(task['task_id'])
   epoch=self.db.execute('SELECT epoch FROM dictation_owner_epochs WHERE owner_id=?',(owner_id,)).fetchone()
   self.db.execute('INSERT INTO dictation_tasks(task_id,digest,snapshot,device,owner_id,owner_epoch,run_epoch) VALUES(?,?,?,?,?,?,?)',(task['task_id'],task['content_hash'],canonical_json(task),paired_device_id,owner_id,epoch[0] if epoch else 0,run_epoch))
   if task['schema_version']==2:self.db.execute("UPDATE dictation_tasks SET state='ready' WHERE task_id=?",(task['task_id'],))
   for lesson in task['lessons']:
    s=lesson['source'];self.db.execute('INSERT INTO dictation_lesson_runs(task_id,lesson_id,book_id,unit,number,revision) VALUES(?,?,?,?,?,?)',(task['task_id'],lesson['lesson_id'],s['book_id'],s['unit'],s['number'],s['revision']))
  return self.status(task['task_id'])
 def status(self,task_id):
  t=self._task(task_id)
  result={k:t[k] for k in ('task_id','run_epoch','last_sequence','state_revision','state','revoked','cancel_requested')}
  row=self.db.execute('SELECT payload FROM dictation_v2_actual WHERE task_id=?',(task_id,)).fetchone()
  if row:result.update(json.loads(row[0]))
  return result
 def request_cancel(self,task_id):
  with self._transaction():self.db.execute('UPDATE dictation_tasks SET cancel_requested=1 WHERE task_id=?',(task_id,))
  return self.status(task_id)
 def receive(self,event,*,paired_device_id):
  if type(event) is dict and event.get('schema_version')==2:return self._receive_v2(event,paired_device_id=paired_device_id)
  if type(event) is not dict or set(event)!=FIELDS:raise RepositoryError('invalid event fields')
  for k in ('task_id','event_id','content_hash'):
   if type(event[k]) is not str or not 0<len(event[k])<=128:raise RepositoryError('invalid identifier')
  for k in ('run_epoch','sequence','state_revision'):
   if type(event[k]) is not int or not 1<=event[k]<=2147483647:raise RepositoryError('invalid sequence/revision')
  if event['kind'] not in KINDS:raise RepositoryError('unknown event kind')
  if event['lesson_id'] is not None and (type(event['lesson_id']) is not str or not 0<len(event['lesson_id'])<=128):raise RepositoryError('invalid lesson')
  raw=canonical_json(event)
  with self._transaction():
   t=self._task(event['task_id'])
   if t['device']!=paired_device_id or t['digest']!=event['content_hash'] or t['run_epoch']!=event['run_epoch']:raise RepositoryError('unauthorized or stale event')
   if t['revoked']:return {'ack':True,'status':'revoked',**self.status(t['task_id'])}
   if event['lesson_id'] is not None and not self.db.execute('SELECT 1 FROM dictation_lesson_runs WHERE task_id=? AND lesson_id=?',(t['task_id'],event['lesson_id'])).fetchone():raise RepositoryError('unknown lesson')
   if event['kind'] in ('lesson_dictation_completed','recitation_completed') and event['lesson_id'] is None:raise RepositoryError('lesson required')
   old=self.db.execute('SELECT payload FROM dictation_event_inbox WHERE task_id=? AND (event_id=? OR sequence=?)',(t['task_id'],event['event_id'],event['sequence'])).fetchall()
   if old:
    if len(old)!=1 or old[0][0]!=raw:raise RepositoryError('event conflict')
   else:self.db.execute('INSERT INTO dictation_event_inbox(task_id,event_id,sequence,payload) VALUES(?,?,?,?)',(t['task_id'],event['event_id'],event['sequence'],raw))
   while True:
    row=self.db.execute('SELECT payload FROM dictation_event_inbox WHERE task_id=? AND sequence=?',(t['task_id'],t['last_sequence']+1)).fetchone()
    if not row:break
    e=json.loads(row[0])
    if e['state_revision']<=t['state_revision']:raise RepositoryError('non-monotonic revision')
    self._apply(t,e)
    t['last_sequence']=e['sequence'];t['state_revision']=e['state_revision']
    self.db.execute('UPDATE dictation_tasks SET last_sequence=?,state_revision=?,state=? WHERE task_id=?',(t['last_sequence'],t['state_revision'],t['state'],t['task_id']))
    self.db.execute('UPDATE dictation_event_inbox SET applied=1 WHERE task_id=? AND sequence=?',(t['task_id'],e['sequence']))
  return {'ack':True,'status':'received',**self.status(event['task_id'])}
 def _apply(self,t,e):
  if t['state'] in TERMINAL:return
  kind=e['kind']
  if kind=='lesson_dictation_completed':
   if t['state'] not in ('started','progress','paused','resumed','lesson_dictation_completed','recitation_completed'):raise RepositoryError('completion before start')
   lesson=next(x for x in json.loads(t['snapshot'])['lessons'] if x['lesson_id']==e['lesson_id'])
   self.db.execute("UPDATE dictation_lesson_runs SET state='completed' WHERE task_id=? AND lesson_id=?",(t['task_id'],e['lesson_id']))
   s=lesson['source'];values=(t['task_id'],s['book_id'],s['unit'],s['number'],t['owner_id'] if t['owner_id'] is not None else 0)
   self.db.execute('INSERT OR IGNORE INTO dictation_completion_ledger VALUES(?,?,?,?,?,?)',values+(s['revision'],))
   if t['owner_id'] is not None:
    words=canonical_json({k:lesson[k] for k in ('expanded_words','recognition_words')})
    self.db.execute('INSERT OR IGNORE INTO dictation_word_history VALUES(?,?,?,?,?,?)',values+(words,))
  if kind=='task_completed' and self.db.execute("SELECT 1 FROM dictation_lesson_runs WHERE task_id=? AND state!='completed'",(t['task_id'],)).fetchone():raise RepositoryError('unfinished lessons')
  t['state']=kind
 def counts(self,owner_id):
  # Zero is an explicit device-anonymous bucket, never merged into a member.
  if owner_id is None:owner_id=0
  return [dict(zip(('book_id','unit','number','count'),r)) for r in self.db.execute('SELECT book_id,unit,number,COUNT(*) FROM dictation_completion_ledger WHERE owner_id=? GROUP BY book_id,unit,number ORDER BY book_id,unit,number',(owner_id,))]
 def word_history(self,owner_id,book_id,unit,number):
  if owner_id is None:return []
  return [dict(task_id=r[0],**json.loads(r[1])) for r in self.db.execute('SELECT task_id,words FROM dictation_word_history WHERE owner_id=? AND book_id=? AND unit=? AND number=? ORDER BY rowid',(owner_id,book_id,unit,number))]
 def revoke_owner(self,owner_id):
  """Commit durable tombstones before caller starts any personal-memory cleanup."""
  with self._transaction():
   self.db.execute('INSERT INTO dictation_owner_epochs VALUES(?,1) ON CONFLICT(owner_id) DO UPDATE SET epoch=epoch+1',(owner_id,))
   ids=[r[0] for r in self.db.execute('SELECT task_id FROM dictation_tasks WHERE owner_id=?',(owner_id,))]
   for tid in ids:
    self.db.execute('UPDATE dictation_tasks SET revoked=1,snapshot=NULL WHERE task_id=?',(tid,))
    self.db.execute('DELETE FROM dictation_event_inbox WHERE task_id=?',(tid,))
    self.db.execute('DELETE FROM dictation_lesson_runs WHERE task_id=?',(tid,))
   for table in ('dictation_completion_ledger','dictation_word_history'):self.db.execute('DELETE FROM '+table+' WHERE owner_id=?',(owner_id,))
 def snapshot(self,task_id):
  raw=self._task(task_id)['snapshot'];return json.loads(raw) if raw else None
 def events(self,task_id):
  self._task(task_id)
  return [json.loads(r[0]) for r in self.db.execute('SELECT payload FROM dictation_event_inbox WHERE task_id=? ORDER BY sequence',(task_id,))]
 def list_tasks(self,owner_id):
  return [self.status(r[0]) for r in self.db.execute('SELECT task_id FROM dictation_tasks WHERE owner_id IS ? ORDER BY rowid DESC',(owner_id,))]
 def _v2_context(self,value):
  t=self._task(value['task_id']);snapshot=json.loads(t['snapshot']) if t['snapshot'] else None
  if not snapshot or snapshot['schema_version']!=2 or t['revoked']:raise RepositoryError('not an active v2 task')
  if value['content_hash']!=t['digest'] or value['run_epoch']!=t['run_epoch']:raise RepositoryError('stale task or epoch')
  phase=value['phase_id'];item=value['item_id']
  phases={p['phase_id']:p for p in snapshot['phases']}
  if phase is not None and (type(phase) is not str or phase not in phases):raise RepositoryError('unknown phase')
  if item is not None and (phase is None or not any(i['item_id']==item for i in phases[phase]['items'])):raise RepositoryError('unknown item')
  return t,snapshot
 def request_command(self,command):
  fields={'schema_version','command_id','task_id','content_hash','run_epoch','expected_state_revision','phase_id','item_id','action'}
  if type(command) is not dict or set(command)!=fields or type(command['schema_version']) is not int or command['schema_version']!=2:raise RepositoryError('invalid command fields')
  if type(command['command_id']) is not str or not 0<len(command['command_id'])<=128:raise RepositoryError('invalid command id')
  if command['action'] not in ('start','confirm','pause','resume','replay','previous','cancel'):raise RepositoryError('unsupported command')
  raw=canonical_json(command)
  with self._transaction():
   t,_=self._v2_context(command)
   old=self.db.execute('SELECT payload,result FROM dictation_v2_commands WHERE task_id=? AND command_id=?',(t['task_id'],command['command_id'])).fetchone()
   if old:
    if old[0]!=raw:raise RepositoryError('command conflict')
    return json.loads(old[1]) if old[1] else {'command_id':command['command_id'],'status':'pending'}
   if type(command['expected_state_revision']) is not int or command['expected_state_revision']!=t['state_revision']:raise RepositoryError('stale state revision')
   if t['state'] in ('completed','cancelled'):raise RepositoryError('terminal task')
   self.db.execute('INSERT INTO dictation_v2_commands(task_id,command_id,payload) VALUES(?,?,?)',(t['task_id'],command['command_id'],raw))
  return {'command_id':command['command_id'],'status':'pending'}
 def reject_command(self,command,*,reason):
  if reason!='stale_revision':raise RepositoryError('invalid rejection reason')
  with self._transaction():
   self._v2_context(command)
   row=self.db.execute('SELECT payload,result FROM dictation_v2_commands WHERE task_id=? AND command_id=?',(command['task_id'],command['command_id'])).fetchone()
   if not row or row[0]!=canonical_json(command):raise RepositoryError('unknown or conflicting command')
   if row[1]:return json.loads(row[1])
   result={'command_id':command['command_id'],'status':'rejected','reason':reason}
   self.db.execute('UPDATE dictation_v2_commands SET result=? WHERE task_id=? AND command_id=?',(canonical_json(result),command['task_id'],command['command_id']))
  return result
 def _receive_v2(self,event,*,paired_device_id):
  fields={'schema_version','task_id','content_hash','event_id','run_epoch','sequence','state_revision','command_id','phase_id','item_id','action','state','actual_repeats','boot_id','session_id','occurred_at'}
  if not fields<=set(event) or set(event)-fields-{'elapsed_ms'} or type(event['schema_version']) is not int:raise RepositoryError('invalid v2 event fields')
  if 'elapsed_ms' in event and (type(event['elapsed_ms']) is not int or not 0<=event['elapsed_ms']<=9007199254740991):raise RepositoryError('invalid elapsed_ms')
  for key in ('event_id','boot_id','session_id','occurred_at','task_id','content_hash'):
   if type(event[key]) is not str or not 0<len(event[key])<=128:raise RepositoryError('invalid '+key)
  for key in ('run_epoch','sequence','state_revision'):
   if type(event[key]) is not int or not 1<=event[key]<=2147483647:raise RepositoryError('invalid '+key)
  if type(event['actual_repeats']) is not int or not 0<=event['actual_repeats']<=2147483647:raise RepositoryError('invalid actual_repeats')
  actions={'task_received','page_presented','playback_started','playback_completed','waiting_child','item_confirmed','paused','resumed','replayed','item_failed','task_completed','cancelled','lesson_completed','resume_requested','navigated_previous'}
  states={'waiting_start','presenting','playing','waiting_child','paused','failed_item','completed','cancelled'}
  if event['action'] not in actions or event['state'] not in states:raise RepositoryError('unsupported action/state')
  if (event['state']=='completed')!=(event['action']=='task_completed'):raise RepositoryError('invalid completion state')
  raw=canonical_json(event)
  with self._transaction():
   t=self._task(event['task_id'])
   if t['device']!=paired_device_id or t['digest']!=event['content_hash'] or t['run_epoch']!=event['run_epoch']:raise RepositoryError('unauthorized or stale event')
   if t['revoked']:return {'ack':True,'status':'revoked',**self.status(t['task_id'])}
   self._v2_context(event)
   if event['command_id'] is not None and (type(event['command_id']) is not str or not self.db.execute('SELECT 1 FROM dictation_v2_commands WHERE task_id=? AND command_id=?',(t['task_id'],event['command_id'])).fetchone()):raise RepositoryError('unknown command')
   old=self.db.execute('SELECT payload FROM dictation_event_inbox WHERE task_id=? AND (event_id=? OR sequence=?)',(t['task_id'],event['event_id'],event['sequence'])).fetchall()
   if old:
    if len(old)!=1 or old[0][0]!=raw:raise RepositoryError('event conflict')
   else:self.db.execute('INSERT INTO dictation_event_inbox(task_id,event_id,sequence,payload) VALUES(?,?,?,?)',(t['task_id'],event['event_id'],event['sequence'],raw))
   while True:
    row=self.db.execute('SELECT payload FROM dictation_event_inbox WHERE task_id=? AND sequence=?',(t['task_id'],t['last_sequence']+1)).fetchone()
    if not row:break
    e=json.loads(row[0])
    if e['state_revision']<=t['state_revision']:raise RepositoryError('non-monotonic revision')
    previous=self.db.execute('SELECT payload FROM dictation_v2_actual WHERE task_id=?',(t['task_id'],)).fetchone()
    previous=json.loads(previous[0]) if previous else {}
    if 'elapsed_ms' in e and 'elapsed_ms' in previous and (e['boot_id'],e['session_id'])==(previous.get('boot_id'),previous.get('session_id')) and e['elapsed_ms']<previous['elapsed_ms']:raise RepositoryError('non-monotonic elapsed_ms')
    if t['state'] not in ('completed','cancelled'):
     if e['action']=='task_completed':self._complete_v2(t)
     t['state']=e['state']
     actual={k:e[k] for k in ('phase_id','item_id','actual_repeats','boot_id','session_id','occurred_at','action')}
     if 'elapsed_ms' in e:actual['elapsed_ms']=e['elapsed_ms']
     self.db.execute('INSERT OR REPLACE INTO dictation_v2_actual VALUES(?,?)',(t['task_id'],canonical_json(actual)))
     if e['command_id'] is not None:
      command=json.loads(self.db.execute('SELECT payload FROM dictation_v2_commands WHERE task_id=? AND command_id=?',(t['task_id'],e['command_id'])).fetchone()[0])
      # Pause receipts unlock explanations; unrelated correlated audio events cannot.
      if command['action']!='pause' or (e['action']=='paused' and e['state']=='paused'):
       self.db.execute('UPDATE dictation_v2_commands SET result=? WHERE task_id=? AND command_id=? AND result IS NULL',(canonical_json({'command_id':e['command_id'],'status':'applied','state':e['state'],'state_revision':e['state_revision']}),t['task_id'],e['command_id']))
    t['last_sequence']=e['sequence'];t['state_revision']=e['state_revision']
    self.db.execute('UPDATE dictation_tasks SET last_sequence=?,state_revision=?,state=? WHERE task_id=?',(t['last_sequence'],t['state_revision'],t['state'],t['task_id']))
    self.db.execute('UPDATE dictation_event_inbox SET applied=1 WHERE task_id=? AND sequence=?',(t['task_id'],e['sequence']))
  return {'ack':True,'status':'received',**self.status(event['task_id'])}

 def _complete_v2(self,t):
  task=json.loads(t['snapshot'])
  events=(json.loads(r[0]) for r in self.db.execute('SELECT payload FROM dictation_event_inbox WHERE task_id=? AND applied=1',(t['task_id'],)))
  confirmed={event['item_id'] for event in events if event['action']=='item_confirmed'}
  required={i['item_id'] for p in task['phases'] for i in p['items']}
  if not required.issubset(confirmed):raise RepositoryError('unfinished items')
  for lesson in task['lessons']:
   src=lesson['source'];values=(t['task_id'],src['book_id'],src['unit'],src['number'],t['owner_id'] if t['owner_id'] is not None else 0)
   self.db.execute("UPDATE dictation_lesson_runs SET state='completed' WHERE task_id=? AND lesson_id=?",(t['task_id'],lesson['lesson_id']))
   self.db.execute('INSERT OR IGNORE INTO dictation_completion_ledger VALUES(?,?,?,?,?,?)',values+(src['revision'],))
   if t['owner_id'] is not None:
    words=[i['text'] for p in task['phases'] if p['type']=='dictation' for i in p['items'] if i['lesson_id']==lesson['lesson_id']]
    self.db.execute('INSERT OR IGNORE INTO dictation_word_history VALUES(?,?,?,?,?,?)',values+(canonical_json({'expanded_words':words,'recognition_words':[i['text'] for p in task['phases'] if p['type']=='recognition' for i in p['items'] if i['lesson_id']==lesson['lesson_id']]}),))
