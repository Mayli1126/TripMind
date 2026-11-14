"""Account-scoped idempotent submission journal. Payloads are hashed, never stored here."""
import hashlib
import json
import time
from uuid import uuid4
from fastapi import HTTPException
from .auth_service import auth_service
from ..infrastructure.database import application_database


def initialize():
    with application_database.connection() as db:
        db.execute('''CREATE TABLE IF NOT EXISTS job_submissions (
            user_id TEXT NOT NULL, request_key TEXT NOT NULL, fingerprint TEXT NOT NULL,
            job_id TEXT NOT NULL, created_at REAL NOT NULL, create_until REAL NOT NULL,
            create_token TEXT NOT NULL, PRIMARY KEY(user_id,request_key))''')


def submit_once(request,kind,payload,create):
    from .planning_job_service import planning_job_service
    user_id=request.state.user['id']
    raw=json.dumps({'kind':kind,'payload':payload},ensure_ascii=False,sort_keys=True,default=str)
    fingerprint=hashlib.sha256(raw.encode()).hexdigest()
    header=request.headers.get('idempotency-key','').strip()
    if len(header)>128:
        raise HTTPException(400,'提交标识过长')
    request_key=f'{kind}:'+('key:'+header if header else 'active:'+fingerprint)
    now=time.time(); token=uuid4().hex
    initialize()
    with application_database.connection() as db:
        db.execute('BEGIN IMMEDIATE')
        row=db.execute('SELECT * FROM job_submissions WHERE user_id=? AND request_key=?',(user_id,request_key)).fetchone()
        ttl=86400 if header else 60
        if row and now-row['created_at']>=ttl:
            db.execute('DELETE FROM job_submissions WHERE user_id=? AND request_key=?',(user_id,request_key)); row=None
        if row and row['fingerprint']!=fingerprint:
            raise HTTPException(409,'同一提交标识不能用于不同内容，请开始一次新的生成')
        if row:
            job_id=row['job_id']; may_create=row['create_until']<=now
            if may_create:
                db.execute('UPDATE job_submissions SET create_until=?,create_token=? WHERE user_id=? AND request_key=?',
                           (now+30,token,user_id,request_key))
        else:
            job_id=uuid4().hex; may_create=True
            db.execute('INSERT INTO job_submissions VALUES (?,?,?,?,?,?,?)',
                       (user_id,request_key,fingerprint,job_id,now,now+30,token))
        db.execute('DELETE FROM job_submissions WHERE created_at<?',(now-7*86400,))
    try:
        existing=planning_job_service.get(job_id)
        from ..infrastructure.task_trace import log
        log({'trace_id':job_id,'request_id':getattr(request.state,'request_id',''),
             'service':'submission','status':'reused'})
        return existing
    except KeyError:
        pass
    except Exception:
        raise HTTPException(503,'任务队列暂不可用，请保留当前页面稍后重试') from None
    if not may_create:
        raise HTTPException(409,'这次提交正在入队，请稍后重试；不会重复创建任务',headers={'Retry-After':'2'})
    try:
        # Claim before publishing: even a fast worker cannot expose an unowned task.
        auth_service.claim('job',job_id,user_id)
        from .job_admission import submission_owner
        owner_token = submission_owner.set(user_id)
        try:
            job=create(job_id)
        finally:
            submission_owner.reset(owner_token)
        from ..infrastructure.task_trace import log
        log({'trace_id':job_id,'request_id':getattr(request.state,'request_id',''),
             'service':'submission','status':'created'})
        return job
    finally:
        with application_database.connection() as db:
            db.execute('UPDATE job_submissions SET create_until=0 WHERE user_id=? AND request_key=? AND create_token=?',
                       (user_id,request_key,token))
