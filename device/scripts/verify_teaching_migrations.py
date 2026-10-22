"""Back up the device DB locally, exercise additive migrations on a second copy.

The live database is opened read-only. No household rows or credentials leave
the device; the pristine backup and the test copy use owner-only permissions.
"""
import json,os,sqlite3,subprocess,sys,time
from pathlib import Path
from types import SimpleNamespace as NS
sys.path.insert(0,os.environ.get('LIRA_PROBE_CODE_ROOT',str(Path(__file__).resolve().parents[1])))
from lira.config import load_config
from lira.agent_task_state import AgentTaskState
from lira.teaching_content import TeachingContentRepository
from lira.teaching_memory import TeachingMemory

def verify_copy(source_path, output_dir):
    """Offline v2 rehearsal. Source is opened read-only; failures affect copies only."""
    import hashlib
    from lira.dictation_service import DictationService
    from lira.db_atomic import atomic
    from lira.textbook_practice import TextbookPractice
    source_path=Path(source_path).resolve();root=Path(output_dir).resolve()
    root.mkdir(mode=0o700,parents=True,exist_ok=False)
    backup=root/'pristine.sqlite';target=root/'migration.sqlite'
    source=sqlite3.connect(source_path.as_uri()+'?mode=ro',uri=True)
    pristine=sqlite3.connect(backup)
    os.chmod(backup,0o600)
    try:source.backup(pristine)
    finally:source.close()
    db=sqlite3.connect(target);os.chmod(target,0o600);pristine.backup(db);pristine.close()
    def snapshot():
        result={}
        for name,sql in db.execute("SELECT name,sql FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'").fetchall():
            quoted='"'+name.replace('"','""')+'"'
            rows=sorted(repr(tuple(row)) for row in db.execute('SELECT * FROM '+quoted))
            result[name]=(sql,hashlib.sha256('\n'.join(rows).encode()).hexdigest(),len(rows))
        return result
    before=snapshot();books=root/'books';books.mkdir()
    try:
        for attempt in range(2):
            TeachingContentRepository(db)
            rt=NS(store=NS(_conn=db),privacy=NS(is_on=False))
            TeachingMemory(rt,AgentTaskState(),lambda:None)
            with atomic(db):
                DictationService(db,TextbookPractice(books),root/'cache',member_exists=lambda _:False)
            current=snapshot()
            assert all(current.get(name)==value for name,value in before.items()),'existing rows/schema changed'
            assert db.execute('PRAGMA quick_check').fetchone()[0]=='ok'
            assert not db.execute('PRAGMA foreign_key_check').fetchall()
            if attempt==0:first=current
            else:assert current==first,'second migration changed schema/data'
        stable=snapshot()
        rollback=sqlite3.connect(':memory:')
        pristine=sqlite3.connect(backup);pristine.backup(rollback);pristine.close()
        original_schema=rollback.execute('SELECT type,name,sql FROM sqlite_master ORDER BY type,name').fetchall()
        try:
            with atomic(rollback):
                DictationService(rollback,TextbookPractice(books),root/'rollback-cache',member_exists=lambda _:False)
                raise RuntimeError('injected copy-only migration failure')
        except RuntimeError:pass
        assert rollback.execute('SELECT type,name,sql FROM sqlite_master ORDER BY type,name').fetchall()==original_schema,'v2 schema/seed rollback failed'
        rollback.close()
        assert snapshot()==stable,'failure did not roll back'
        return dict(passed=True,evidence_type='isolated database copy; no production migration',source_read_only=True,pristine_backup=str(backup),migration_test_copy=str(target),existing_tables_unchanged=sorted(before),additive_tables=sorted(set(stable)-set(before)),repeated_migration_idempotent=True,failure_rollback=True,quick_check='ok',foreign_key_check=[],historical_completion_not_reinterpreted=True,service_restarted=False)
    finally:db.close()

def main():
    pid=subprocess.check_output(['systemctl','--user','show','lira','--property=MainPID','--value'],text=True).strip()
    env=dict(part.decode().split('=',1) for part in Path('/proc/'+pid+'/environ').read_bytes().split(b'\0') if b'=' in part)
    os.chdir('/home/orangepi/lira/device');cfg=load_config(env=env)
    root=Path('/home/orangepi/lira/backups');root.mkdir(mode=0o700,exist_ok=True)
    stamp=time.strftime('%Y%m%d-%H%M%S')
    backup=root/('pre-teaching-'+stamp+'.sqlite');test=Path('/tmp')/('lira-teaching-migration-'+stamp+'.sqlite')
    for target in (backup,test):
        fd=os.open(target,os.O_WRONLY|os.O_CREAT|os.O_EXCL,0o600);os.close(fd)
    source=sqlite3.connect(cfg.db_path.resolve().as_uri()+'?mode=ro',uri=True)
    pristine=sqlite3.connect(backup);source.backup(pristine);source.close()
    db=sqlite3.connect(test);pristine.backup(db);pristine.close();db.row_factory=sqlite3.Row
    db.execute('PRAGMA foreign_keys=ON')
    def schema():return {r[0]:r[1] for r in db.execute("SELECT name,sql FROM sqlite_master WHERE type='table'")}
    before=schema()
    def count(name):return db.execute('SELECT COUNT(*) FROM "'+name.replace('"','""')+'"').fetchone()[0]
    counts={name:count(name) for name in before}
    rt=NS(store=NS(_conn=db),privacy=NS(is_on=False))
    state=AgentTaskState()
    for _ in range(2):
        TeachingContentRepository(db)
        TeachingMemory(rt,state,lambda:None)
        assert all(schema().get(name)==definition and count(name)==counts[name] for name,definition in before.items())
        assert db.execute('PRAGMA quick_check').fetchone()[0]=='ok'
        if _==0:first=schema()
        else:assert schema()==first
    result=dict(passed=True,live_database_read_only=True,pristine_backup=str(backup),migration_test_copy=str(test),
        backup_mode=oct(backup.stat().st_mode&0o777),test_mode=oct(test.stat().st_mode&0o777),
        additive_tables=sorted(set(schema())-set(before)),existing_schema_and_row_counts_unchanged=True,
        repeated_migration_idempotent=True,quick_check='ok',service_restarted=False)
    db.close();print(json.dumps(result),flush=True)
if __name__=='__main__':
    import argparse
    parser=argparse.ArgumentParser()
    parser.add_argument('--source',help='Existing SQLite file; opened read-only')
    parser.add_argument('--output-dir',help='New directory for pristine and rehearsal copies')
    args=parser.parse_args()
    if args.source:
        if not args.output_dir:parser.error('--source requires --output-dir')
        print(json.dumps(verify_copy(args.source,args.output_dir),ensure_ascii=False,indent=2))
    else:
        if args.output_dir:parser.error('--output-dir requires --source')
        main()
