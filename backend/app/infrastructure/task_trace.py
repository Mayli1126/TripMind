"""仅记录白名单中的少量运行事件，不序列化提示词、网址或工具参数。"""
import contextvars
import json
import logging
import sys
import threading
import time
from contextlib import contextmanager
from datetime import datetime, timezone
from uuid import uuid4

request_id = contextvars.ContextVar('request_id', default='')
_stage = contextvars.ContextVar('task_stage', default='')
_agent = contextvars.ContextVar('task_agent', default='')
_active = None
_process_metrics = {}
_lock = threading.RLock()
_logger = logging.getLogger('tripmind.runtime')
if not _logger.handlers:
    handler = logging.StreamHandler(sys.stderr)
    handler.setFormatter(logging.Formatter('%(message)s'))
    _logger.addHandler(handler)
_logger.setLevel(logging.INFO)
_logger.propagate = False


def activate(job_id, run_id, sink):
    global _active
    _active = (job_id, run_id, sink)
    _stage.set('initializing'); _agent.set('')


def deactivate():
    global _active
    _active = None


def stage(name, agent=None):
    _stage.set(name)
    _agent.set(agent or '')


def emit(service, operation, status, **fields):
    from ..services.diagnostic_summary import summarize_event
    with _lock:
        summarize_event(_process_metrics, dict(service=service, operation=operation, status=status,
            **{k:v for k,v in fields.items() if k in {'duration_ms','http_status','error_type','hit'}}))
    if _active:
        job_id, run_id, sink = _active
    elif request_id.get():
        job_id,run_id,sink=request_id.get(),'',log
    else:
        return
    allowed = {'call_id','duration_ms','error_type','http_status','model','reason','hit','attempt','input_tokens','output_tokens'}
    event = dict(trace_id=job_id, run_id=run_id, service=service, operation=operation,
                 status=status, stage=_stage.get(), agent=_agent.get(),
                 timestamp=datetime.now(timezone.utc).isoformat(),
                 **{k:v for k,v in fields.items() if k in allowed and isinstance(v,(str,int,float,bool,type(None)))})
    with _lock:
        sink(event)


def log(event):
    _logger.info(json.dumps(event,ensure_ascii=False,default=str))


def process_metrics():
    """这里只统计当前进程启动后的事件，执行器任务摘要单独持久化。"""
    import copy
    with _lock:
        return dict(scope='current_process_since_start', **copy.deepcopy(_process_metrics))


@contextmanager
def span(service, operation, *, call_id=None, **fields):
    # 原生工具调用沿用模型返回的调用标识，方便将执行耗时与工具结果关联。
    call_id, started = call_id or uuid4().hex[:12], time.monotonic()
    emit(service,operation,'started',call_id=call_id,**fields)
    try:
        yield
    except BaseException as exc:
        emit(service,operation,'failed',call_id=call_id,duration_ms=round((time.monotonic()-started)*1000),
             error_type=type(exc).__name__,http_status=getattr(exc,'status_code',None),**fields)
        raise
    else:
        emit(service,operation,'completed',call_id=call_id,duration_ms=round((time.monotonic()-started)*1000),**fields)
