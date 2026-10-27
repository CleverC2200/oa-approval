from __future__ import annotations

import argparse
import json
from pathlib import Path
import sqlite3

from apps.oa.approvals.client import emit, failure, send, socket_path, query
from apps.oa.runtime.paths import RUNTIME_ROOT


DEFAULT_ROOT = RUNTIME_ROOT


def main(argv=None):
    parser = argparse.ArgumentParser(description='单轮 SQLite 审批；默认预览，不写 OA。')
    parser.add_argument('--root', type=Path, default=DEFAULT_ROOT)
    commands = parser.add_subparsers(dest='operation', required=True)
    pending = commands.add_parser('start', aliases=['pending'], help='重新拉取待办并保持前台owner；EOF或finish安全结束')
    pending.add_argument('--workflow', action='append')
    pending.add_argument('--request-id', action='append', help='仅纳入重新拉取待办中匹配的申请，可重复指定')
    pending.add_argument('--limit', type=int, default=20, help='紧凑队列展示数，默认 20，0 只显示统计')
    pending.add_argument('--batch-size', type=int, help='冻结本轮可领取的全局 FIFO 排队前缀数量，独立于展示数')
    pending.add_argument('--full', action='store_true', help='返回全部待办完整数据，忽略 --limit')
    pending.add_argument('--demo', action='store_true', help='仅使用合成 OA 与本地附件，不访问网络')
    pending.add_argument('--continue-from', help='仅消费指定已关闭轮次的接续，保留流程范围与链内已处理身份')
    status = commands.add_parser('status', help='只读已有轮次、队列和owner可达性，不刷新OA')
    status.add_argument('--run-id')
    status.add_argument('--full', action='store_true')
    status.add_argument('--view', choices=['batch'], help='紧凑批次范围、Agent 占用和账本验收事实')
    show = commands.add_parser('show', help='只读任务、材料、审核和提交事实，不调用OA')
    show.add_argument('--task-id', type=int, required=True)
    show.add_argument('--full', action='store_true')
    finish = commands.add_parser('finish', help='结束指定轮次；等待当前采集落盘，不代表业务全部完成')
    finish.add_argument('--run-id', required=True)
    finish.add_argument('--handoff', action='store_true', help='排空后关闭并预留下一轮；返回原生独立对话接续信息')
    finish.add_argument('--orphaned', action='store_true', help='显式核对后关闭失联轮次；仅写本地事实，不恢复审核或创建接续')
    collect = commands.add_parser('collect', help='领取当前轮的一单并冻结完整材料')
    collect.add_argument('--workflow-key')
    collect.add_argument('--agent-id', required=True)
    collect.add_argument('--expected-task-id', type=int, required=True)
    collect.add_argument('--agent-workflow-key', required=True)
    submit = commands.add_parser('submit', help='保存意见，显式确认时提交并回查')
    submit.add_argument('--task-id', type=int, required=True)
    submit.add_argument('--opinion-file', type=Path)
    submit.add_argument('--recognition-file', type=Path, help='海外识别结果或带附件来源的 DeepSeek 报告；可与意见一起保存')
    submit.add_argument('--observations-file', type=Path)
    submit.add_argument('--confirm-write', choices=['YES'])
    submit.add_argument('--allow-overseas', choices=['YES'])
    preview = commands.add_parser('preview', help='保存本地审核并展示意见和限定字段计划，不写OA')
    preview.add_argument('--task-id', type=int, required=True)
    preview.add_argument('--opinion-file', type=Path)
    preview.add_argument('--recognition-file', type=Path)
    preview.add_argument('--observations-file', type=Path)
    check = commands.add_parser('postcheck', help='仅核对未知提交，不重发')
    check.add_argument('--task-id', type=int, required=True)
    failed = commands.add_parser('fail', help='登记当前轮活动审核的技术失败，不访问 OA')
    failed.add_argument('--task-id', type=int, required=True)
    failed.add_argument('--reason', required=True)
    args = parser.parse_args(argv)
    if args.operation == 'finish' and args.orphaned and args.handoff:
        parser.error('--orphaned cannot create a handoff')
    if args.operation in {'pending', 'start'} and args.limit < 0:
        parser.error('--limit must be non-negative')
    if args.operation in {'pending', 'start'} and args.batch_size is not None and args.batch_size < 0:
        parser.error('--batch-size must be non-negative')
    if args.operation in {'pending', 'start'} and args.continue_from and (
            args.workflow is not None or args.request_id is not None or args.batch_size is not None):
        parser.error('--continue-from preserves the original selection; omit --workflow, --request-id and --batch-size')
    if args.operation in {'submit', 'preview'} and not (args.opinion_file or args.recognition_file):
        parser.error('submit requires --opinion-file or --recognition-file')
    root = args.root.expanduser().resolve()
    try:
        if args.operation in {'pending', 'start'}:
            from apps.oa.approvals import owner

            try:
                return owner.serve(root, demo=args.demo, workflows=args.workflow, limit=args.limit, full=args.full,
                             batch_size=args.batch_size, request_ids=args.request_id, continue_from=args.continue_from)
            except BlockingIOError:
                emit({'status': 'already_running'})
                return 0
        if args.operation in {'status', 'show'}:
            result = query(root, args.operation, run_id=getattr(args, 'run_id', None),
                           task_id=getattr(args, 'task_id', None), full=args.full, view=getattr(args, 'view', None))
            emit(result)
            return 0
        if args.operation == 'finish' and args.orphaned:
            from apps.oa.approvals.owner import finish_orphaned

            emit(finish_orphaned(root, args.run_id))
            return 0
        request = {'operation': args.operation}
        if args.operation == 'collect':
            request.update(workflow_key=args.workflow_key, agent_id=args.agent_id,
                           expected_task_id=args.expected_task_id, agent_workflow_key=args.agent_workflow_key)
        elif args.operation == 'finish':
            request['run_id'] = args.run_id
            request['handoff'] = args.handoff
        else:
            request['task_id'] = args.task_id
        if args.operation in {'submit', 'preview'}:
            request.update(opinion=args.opinion_file.read_text() if args.opinion_file else None,
                           recognition=json.loads(args.recognition_file.read_text()) if args.recognition_file else None,
                           observations=json.loads(args.observations_file.read_text()) if args.observations_file else None,
                           confirm_write=getattr(args, 'confirm_write', None) == 'YES',
                           allow_overseas=getattr(args, 'allow_overseas', None) == 'YES')
        if args.operation == 'fail':
            request['reason'] = args.reason
        result = send(root, request)
        emit(result)
        return 1 if result.get('status') == 'refused' or (result.get('status') == 'failed' and (args.operation != 'fail' or 'error' in result)) else 0
    except KeyboardInterrupt:
        return 130
    except (OSError, RuntimeError, ValueError, KeyError, TypeError, sqlite3.Error) as error:
        emit(failure(error))
        return 1


if __name__ == '__main__':
    raise SystemExit(main())
