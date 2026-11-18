"""仅在显式隔离的本机 Redis 测试实例上进行小规模真实验收。"""
import argparse
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
import os
import subprocess
import sys
import time
from uuid import uuid4


def main():
    parser=argparse.ArgumentParser()
    parser.add_argument('--redis-url',required=True)
    args=parser.parse_args()
    from urllib.parse import urlparse
    url=urlparse(args.redis_url)
    if url.hostname not in ('127.0.0.1','localhost') or url.port in (None,6379):
        parser.error('Use an isolated temporary Redis on a non-default loopback port, never the project Redis.')
    os.environ.update(REDIS_ENABLED='true',CACHE_ENABLED='true',REDIS_URL=args.redis_url,CACHE_REDIS_URL=args.redis_url,
        PROVIDER_WAIT_SECONDS='0',PROVIDER_MAP_INTERVAL_SECONDS='0',PROVIDER_MAP_CONCURRENCY='1',
        JOB_USER_ACTIVE_LIMIT='2',JOB_GLOBAL_ACTIVE_LIMIT='3',JOB_QUEUE_CAPACITY='3')
    import redis
    from fastapi import HTTPException
    from app.infrastructure import redis_client
    from app.infrastructure.shared_cache import shared_cache, make_key
    from app.infrastructure.cache_rebuild import cache_rebuilder, CacheBusyError
    from app.infrastructure.provider_guard import provider_guard
    from app.services.cache_policy import make_entry
    from app.services.job_admission import submission_owner, ADMISSION_KEY
    from app.services.operations_service import queue_snapshot
    client=redis.Redis.from_url(args.redis_url,decode_responses=True)
    client.ping(); redis_client._client=client; redis_client._group_ready=False
    stamp=uuid4().hex
    # 验证真实 Lua 发布、原始数据年龄、空结果缓存与失去所有权后的隔离。
    key=make_key('poi',dict(fixture=stamp))
    entry=cache_rebuilder.get(key,lambda:make_entry('poi',[],'fixture',empty=True))
    assert shared_cache.read(key).status=='negative'
    assert 0 < client.pttl(key.data_key) <= 20000
    assert shared_cache.read(key).entry.hard_until==entry.hard_until
    lost=make_key('poi',dict(fixture=stamp+'lost'))
    def stale_owner():
        client.set(lost.lock_key,'replacement',px=45000)
        return make_entry('poi',[],'fixture',empty=True)
    try:
        cache_rebuilder.get(lost,stale_owner)
        raise AssertionError('lost owner published')
    except CacheBusyError:
        pass
    assert client.get(lost.lock_key)=='replacement' and not client.exists(lost.data_key)
    print('PASS real Lua cache publication, absolute expiry, negative cache and owner fencing')
    # 第二个进程必须能观察到第一个进程持有的调用许可。
    with provider_guard.slot('amap',stamp) as permit:
        child="from app.infrastructure.provider_guard import provider_guard,ProviderBusyError\ntry: provider_guard.acquire('amap',"+repr(stamp)+")\nexcept ProviderBusyError: print('blocked')\nelse: raise AssertionError('cross-process limit bypassed')"
        result=subprocess.run([sys.executable,'-c',child],capture_output=True,text=True,timeout=10)
        assert result.returncode==0 and 'blocked' in result.stdout, result.stderr[-500:]
        time.sleep(11)
        assert client.pttl(permit[0]+':active')>50000
    assert client.zcard(permit[0]+':active')==0
    print('PASS cross-process provider limit, real renewal and release')
    now=datetime.now(timezone.utc).isoformat()
    def publish(index):
        token=submission_owner.set('fixture-'+str(index))
        try:
            return redis_client.publish_job(stamp+str(index),'plan',{'request':{}},dict(
                status='queued',request={},created_at=now,updated_at=now,metrics={},agents={}))
        except HTTPException as exc:
            return exc.status_code
        finally:
            submission_owner.reset(token)
    with ThreadPoolExecutor(max_workers=4) as pool:
        outcomes=list(pool.map(publish,range(4)))
    assert sum(value is True for value in outcomes)==3 and outcomes.count(503)==1, outcomes
    assert client.hlen(ADMISSION_KEY)==3 and client.xlen('trip:jobs')==3
    existing=next(stamp+str(i) for i,value in enumerate(outcomes) if value is True)
    assert redis_client.publish_job(existing,'plan',{},dict(status='queued')) is False
    assert client.hlen(ADMISSION_KEY)==3
    assert redis_client.guarded_job_write(existing,'',dict(status='cancelled'),allowed={'queued'})
    assert client.hlen(ADMISSION_KEY)==2
    for mid,_ in redis_client.dequeue_jobs(block_ms=10,count=10):
        redis_client.ack_job(mid)
    assert client.xlen('trip:jobs')==0
    snapshot=queue_snapshot()
    assert snapshot['available'] and snapshot['stream_messages']==0
    print('PASS concurrent admission/capacity, idempotency, terminal release, ACK cleanup and queue diagnostics')
    shared_cache.close(); provider_guard.close(); client.close()


if __name__=='__main__':
    main()
