"""可复用的隔离执行器；取消任务仅终止其子进程树，不影响 API 或 Redis。"""
import json
import os
import queue
import signal
import subprocess
import sys
import threading
import time
from pathlib import Path


class JobCancelled(Exception):
    pass


class ExecutorStopped(Exception):
    pass


class ExecutorFailure(RuntimeError):
    def __init__(self,error_type,public_message=None):
        self.error_type=error_type
        # 只接受执行器明确标记的选点校验原因，不能透传一般异常的敏感内容。
        self.public_message=public_message if error_type=='TourismResearchError' else None
        super().__init__(error_type)


def terminate_posix_tree(pid):
    # MCP 会创建自己的会话，因此只终止当前进程组还不足以清理子进程。
    try:
        snapshot=subprocess.run(['ps','-eo','pid=,ppid='],capture_output=True,text=True,timeout=5,check=True)
    except (OSError,subprocess.SubprocessError):
        snapshot=None
    children={}
    for line in snapshot.stdout.splitlines() if snapshot else []:
        child,parent=map(int,line.split())
        children.setdefault(parent,[]).append(child)
    descendants=[]
    def visit(parent):
        for child in children.get(parent,[]):
            visit(child); descendants.append(child)
    visit(pid)
    for child in descendants:
        try:
            if os.getpgid(child)==child:
                os.killpg(child,signal.SIGKILL)
            else:
                os.kill(child,signal.SIGKILL)
        except ProcessLookupError:
            pass
    try:
        os.killpg(pid,signal.SIGKILL)
    except ProcessLookupError:
        pass


def kill_tree(process):
    if process is None:
        return
    if os.name == 'nt':
        if process.poll() is None:
            subprocess.run(['taskkill','/PID',str(process.pid),'/T','/F'],
                       stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL,
                       creationflags=subprocess.CREATE_NO_WINDOW,timeout=10)
    else:
        terminate_posix_tree(process.pid)
    process.wait(timeout=10)


def contain_windows(process):
    """即使 API 进程崩溃，Windows 作业对象也会关闭整棵子进程树。"""
    if os.name!='nt':
        return None
    import ctypes
    from ctypes import wintypes as w
    class Basic(ctypes.Structure):
        _fields_=[('process_time',ctypes.c_longlong),('job_time',ctypes.c_longlong),('flags',w.DWORD),
                  ('min_working',ctypes.c_size_t),('max_working',ctypes.c_size_t),('active',w.DWORD),
                  ('affinity',ctypes.c_size_t),('priority',w.DWORD),('scheduling',w.DWORD)]
    class IO(ctypes.Structure):
        _fields_=[(name,ctypes.c_ulonglong) for name in ('read_ops','write_ops','other_ops','read_bytes','write_bytes','other_bytes')]
    class Extended(ctypes.Structure):
        _fields_=[('basic',Basic),('io',IO),('process_memory',ctypes.c_size_t),('job_memory',ctypes.c_size_t),
                  ('peak_process',ctypes.c_size_t),('peak_job',ctypes.c_size_t)]
    kernel=ctypes.WinDLL('kernel32',use_last_error=True)
    kernel.CreateJobObjectW.argtypes=[ctypes.c_void_p,w.LPCWSTR]; kernel.CreateJobObjectW.restype=w.HANDLE
    kernel.SetInformationJobObject.argtypes=[w.HANDLE,ctypes.c_int,ctypes.c_void_p,w.DWORD]
    kernel.AssignProcessToJobObject.argtypes=[w.HANDLE,w.HANDLE]
    kernel.CloseHandle.argtypes=[w.HANDLE]
    handle=kernel.CreateJobObjectW(None,None)
    info=Extended(); info.basic.flags=0x2000  # 关闭作业对象时终止整棵子进程树，对应 JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE。
    if not handle or not kernel.SetInformationJobObject(handle,9,ctypes.byref(info),ctypes.sizeof(info)) or not kernel.AssignProcessToJobObject(handle,w.HANDLE(int(process._handle))):
        if handle: kernel.CloseHandle(handle)
        raise OSError(ctypes.get_last_error(),'executor_containment_failed')
    return (kernel,handle)


class IsolatedExecutor:
    def __init__(self):
        self.process = None
        self.containment=None
        self.events = queue.Queue()
        self.lock = threading.Lock()

    def _start(self):
        self.events = queue.Queue()
        options = {'creationflags':subprocess.CREATE_NO_WINDOW} if os.name == 'nt' else {'start_new_session':True}
        self.process = subprocess.Popen([sys.executable,'-m','app.services.planning_runner',str(os.getpid())],
            cwd=str(Path(__file__).resolve().parents[2]),stdin=subprocess.PIPE,stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,text=True,encoding='utf-8',bufsize=1,
            env={**os.environ,'PYTHONUTF8':'1','PYTHONUNBUFFERED':'1'},**options)
        try:
            self.containment=contain_windows(self.process)
        except BaseException:
            kill_tree(self.process); self.process=None; raise
        process, events = self.process, self.events
        def read():
            try:
                for line in process.stdout:
                    try:
                        events.put(json.loads(line))
                    except ValueError:
                        pass  # 不向公开诊断信息复制非结构化输出。
            except (ValueError,OSError):
                pass
            finally:
                events.put({'kind':'exit'})
        threading.Thread(target=read,daemon=True,name='planning-ipc').start()

    def run(self, spec, callback, diagnostic, should_cancel, heartbeat=None, stopping=None):
        with self.lock:
            if should_cancel():
                raise JobCancelled()
            if self.process is None or self.process.poll() is not None:
                self.stop()
                self._start()
            try:
                self.process.stdin.write(json.dumps(spec,ensure_ascii=False)+'\n'); self.process.stdin.flush()
            except (OSError,ValueError):
                self.stop(); raise ExecutorFailure('executor_channel_closed')
            next_heartbeat = 0
            from ..config import get_settings
            deadline=time.monotonic()+get_settings().planning_job_timeout_seconds
            def guarded(call,*args,**kwargs):
                try:
                    return call(*args,**kwargs)
                except BaseException:
                    self.stop(); raise
            while True:
                if time.monotonic()>=deadline:
                    self.stop(); raise TimeoutError('job_deadline_exceeded')
                if guarded(should_cancel):
                    self.stop(); raise JobCancelled()
                if stopping and stopping():
                    self.stop(); raise ExecutorStopped()
                if heartbeat and time.monotonic() >= next_heartbeat:
                    if not guarded(heartbeat):
                        self.stop(); raise ExecutorStopped('lease_lost')
                    next_heartbeat = time.monotonic()+5
                try:
                    event = self.events.get(timeout=.25)
                except queue.Empty:
                    continue
                if event.get('job_id') not in (None,spec['job_id']):
                    continue
                kind = event.get('kind')
                if kind == 'progress':
                    guarded(callback,**event['data'])
                elif kind == 'diagnostic':
                    guarded(diagnostic,event['data'])
                elif kind == 'result':
                    return event['data']
                elif kind == 'waiting_input':
                    from ..graphs.planning_graph import PlanningWaitingInput
                    raise PlanningWaitingInput(event['data'])
                elif kind in {'error','exit'}:
                    self.stop()
                    raise ExecutorFailure(event.get('error_type','executor_exited'),event.get('public_message'))

    def stop(self):
        process, self.process = self.process, None
        if self.containment:
            kernel,handle=self.containment; self.containment=None
            kernel.CloseHandle(handle)
        kill_tree(process)
        if process:
            for pipe in (process.stdin,process.stdout):
                if pipe:
                    pipe.close()


isolated_executor = IsolatedExecutor()
