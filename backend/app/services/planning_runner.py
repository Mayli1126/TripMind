"""Child-only graph execution. stdout is an internal JSON channel, never application logs."""
import json
import os
import signal
import subprocess
import sys
import threading
import time


def main():
    output = sys.stdout
    sys.stdout = sys.stderr  # Legacy prints are discarded by the supervisor, not published.
    from ..infrastructure import task_trace
    from ..models.schemas import TripPlan, TripRequest
    from ..graphs.planning_graph import PlanningWaitingInput, run_planning_graph
    from ..graphs.revision_graph import run_revision_graph
    parent_pid = int(sys.argv[1])
    send_lock = threading.RLock()
    guard = {'lease':None}
    def send(kind,job_id,**values):
        with send_lock:
            output.write(json.dumps({'kind':kind,'job_id':job_id,**values},ensure_ascii=False,default=str)+'\n'); output.flush()
    def parent_alive():
        if os.name != 'nt':
            return os.getppid() == parent_pid
        import ctypes
        kernel=ctypes.windll.kernel32
        kernel.OpenProcess.restype=ctypes.c_void_p
        handle=kernel.OpenProcess(0x100000,False,parent_pid)
        if not handle:
            return False
        try:
            return kernel.WaitForSingleObject(ctypes.c_void_p(handle),0)==0x102
        finally:
            kernel.CloseHandle(ctypes.c_void_p(handle))
    def terminate_self():
        if os.name=='nt':
            subprocess.run(['taskkill','/PID',str(os.getpid()),'/T','/F'],
                           stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL,creationflags=subprocess.CREATE_NO_WINDOW)
        else:
            from .job_process import terminate_posix_tree
            terminate_posix_tree(os.getpid())
        os._exit(75)
    def watchdog():
        while True:
            if not parent_alive():
                terminate_self()
            lease=guard['lease']
            if lease:
                from ..infrastructure import redis_client
                try:
                    if not redis_client.owns_job_lease(*lease):
                        terminate_self()
                except Exception:
                    terminate_self()  # Fail closed: an unfenced child must never keep generating.
            time.sleep(2)
    threading.Thread(target=watchdog,daemon=True,name='executor-guardian').start()
    for line in sys.stdin:
        spec=json.loads(line); job_id=spec['job_id']; payload=spec['payload']
        from ..config import get_settings
        if get_settings().redis_enabled!=bool(spec.get('redis')):
            get_settings().redis_enabled=bool(spec.get('redis'))
            from ..graphs.planning_graph import reset_compiled_graph
            reset_compiled_graph()
        guard['lease']=(job_id,spec['run_id']) if spec.get('redis') else None
        task_trace.activate(job_id,spec['run_id'],lambda event:send('diagnostic',job_id,data=event))
        def progress(progress,stage,message,agent=None,agent_status=None):
            task_trace.stage(stage,agent)
            if agent_status=='degraded' or stage in {'degraded','fallback_generation'}:
                task_trace.emit('workflow','fallback','degraded',reason=stage)
            send('progress',job_id,data=dict(progress=progress,stage=stage,message=message,agent=agent,agent_status=agent_status))
        try:
            request=TripRequest.model_validate(payload['request'],context={'from_storage':True})
            if spec['type'] in {'plan','resume'}:
                result=run_planning_graph(request,progress,job_id=job_id,
                    resume_value=payload.get('resume_value') if spec['type']=='resume' else None,
                    expected_question=payload.get('expected_question'),
                    recover=bool(spec.get('recover')))
            else:
                result=run_revision_graph(request,TripPlan.model_validate_json(payload['plan']),payload['instruction'],progress,
                                          base_version=payload.get('base_version'))
            send('result',job_id,data=result.model_dump(mode='json'))
        except PlanningWaitingInput as wait:
            send('waiting_input',job_id,data=wait.question)
        except BaseException as exc:
            send('error',job_id,error_type=type(exc).__name__)
        finally:
            guard['lease']=None
            task_trace.deactivate()
    terminate_self()


if __name__=='__main__':
    main()
