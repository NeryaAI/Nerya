"""Parent-invoked gateway runner. No credentials in arguments, no enrollment/funding."""
import argparse
import json
from pathlib import Path
from nerya.core.paths import WorkspacePaths
from nerya.core.config import load_config
from nerya.financial.contracts import FinancialContext
from nerya.financial.gateway import FinancialGateway


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('operation', choices=['account', 'prepare', 'execute', 'status'])
    parser.add_argument('--workspace', type=Path, required=True)
    parser.add_argument('--actor', required=True)
    parser.add_argument('--request', type=Path)
    parser.add_argument('--action-key')
    parser.add_argument('--action-id')
    parser.add_argument('--quote-hash')
    args = parser.parse_args()
    config = load_config(WorkspacePaths(args.workspace.resolve()))
    gateway = FinancialGateway(config)
    # Explicit operator process only; agent callers must use their canonical runtime context.
    context = FinancialContext(args.actor, frozenset({'api:all'}))
    if args.operation in {'account', 'prepare'}:
        if not args.request:
            parser.error('--request is required')
        request = json.loads(args.request.read_text())
        if args.operation == 'account':
            result = gateway.account_state(context, request)
        else:
            if not args.action_key: parser.error('--action-key is required')
            result = gateway.prepare(context, request, action_key=args.action_key)
    else:
        if not args.action_id: parser.error('--action-id is required')
        if args.operation == 'execute':
            if not args.quote_hash: parser.error('--quote-hash is required')
            result = gateway.execute(context, args.action_id, quote_hash=args.quote_hash)
        else:
            result = gateway.reconcile(context, args.action_id)
    print(json.dumps(result, default=str))


if __name__ == '__main__': main()
