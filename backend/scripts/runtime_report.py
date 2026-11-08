"""Read-only operator report. Default does not connect to Redis or upstream providers."""
import argparse
import json
from app.services.runtime_service import runtime_status
from app.services.operations_service import deployment_warnings, queue_snapshot
from app.infrastructure.task_trace import process_metrics


def main():
    parser = argparse.ArgumentParser(description='只读运行诊断，不调用模型/地图，不修改任务')
    parser.add_argument('--queues', action='store_true', help='额外读取Redis队列统计；不修复或回收')
    args = parser.parse_args()
    runtime = runtime_status()
    report = dict(runtime=runtime, warnings=deployment_warnings(runtime), process_metrics=process_metrics(),
                  note='CLI is a separate process; its in-process MCP/worker/counters are not the live API process.')
    if args.queues:
        report['queues'] = queue_snapshot()
    print(json.dumps(report, ensure_ascii=False, indent=2))


if __name__ == '__main__':
    main()
