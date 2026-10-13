"""Bind package candidates to reviewed testnet deployment; stage native proposals."""
import argparse
from copy import deepcopy
import hashlib
import json
from pathlib import Path

from nerya.core import yaml_io
from nerya.core.paths import WorkspacePaths
from nerya.evolution.patch_proposal import create_proposal
from nerya.strategies.package import load_package_from_dir
from nerya.strategies.validator import static_scan_blockers, validate_proposal_files

ROOT = Path(__file__).resolve().parent
IDS = ('monad_lp_range', 'monad_perp_sma', 'monad_meme_momentum')


def deployment(path):
    raw = Path(path).read_bytes()
    doc = json.loads(raw)
    if (doc.get('chain_id') != 10143 or doc.get('testnet') is not True or doc.get('reviewed') is not True
            or doc.get('official') is not False or doc.get('protocol_variant') != 'uniswap_v3'
            or doc.get('deployment_origin') != 'canonical_test_deployment'):
        raise ValueError('Reviewed canonical UniV3 testnet deployment required')
    for name in ('wmon_nusdc', 'nmeme_wmon'):
        pool = doc['pools'][name]
        if not pool.get('reviewed') or not pool.get('testnet') or not pool.get('code_hash'):
            raise ValueError('Reviewed testnet pool and code hash required')
        for token in (pool['token0'], pool['token1']):
            record = doc['tokens'][token.lower()]
            if not record.get('testnet') or not record.get('reviewed') or not record.get('code_hash'):
                raise ValueError('Reviewed test asset required')
    return doc, hashlib.sha256(raw).hexdigest()


def candidates(path, *, wallet_id, account_id, perpl_account_id, cycle_id='demo_1'):
    doc, sha = deployment(path)
    result = {}
    for sid in IDS:
        root = ROOT / sid
        files = {p.relative_to(root).as_posix(): p.read_text() for p in root.rglob('*') if p.is_file() and '__pycache__' not in p.parts}
        manifest = yaml_io.loads(files['strategy.yml'])
        p = manifest['params']
        p.update(cycle_id=cycle_id, account_id=perpl_account_id if sid=='monad_perp_sma' else account_id)
        manifest['accounts'] = [p['account_id']]
        if sid != 'monad_perp_sma':
            name = 'wmon_nusdc' if sid=='monad_lp_range' else 'nmeme_wmon'
            p.update(wallet_id=wallet_id, deployment_sha256=sha, pool=deepcopy(doc['pools'][name]))
            if sid=='monad_meme_momentum':
                symbols = {doc['tokens'][token.lower()]['symbol']: token for token in (p['pool']['token0'], p['pool']['token1'])}
                if set(symbols) != {'nMEME', 'WMON'}:
                    raise ValueError('Explicit nMEME/WMON test pool required')
                p.update(test_token=symbols['nMEME'], quote_token=symbols['WMON'])
        files['strategy.yml'] = yaml_io.dumps(manifest)
        result[sid] = files
    return result


def stage(paths, packages, *, allow_update=False):
    results = []
    for sid, files in packages.items():
        if paths.strategy(sid).exists() and not allow_update:
            raise ValueError('Existing strategy must be reviewed; no overwrite: '+sid)
        validation = validate_proposal_files(strategy_id=sid, files=files)
        if not validation.ok:
            raise ValueError(validation.asdict())
        # limits.yml is protected operator configuration, not proposal material.
        extras = {'after/strategies/'+sid+'/'+name: text for name, text in files.items() if name != 'limits.yml'}
        extras['validation_report.json'] = json.dumps(validation.asdict(), indent=2)
        prop = create_proposal(paths, kind='strategy_package_proposal', summary='Install '+sid+' on Monad testnet',
            rationale='One bounded testnet cycle through native FinancialAPI; no grant or execution activated.',
            target='strategies/'+sid, extra_files=extras,
            test_plan='Run competition/monad/strategies/tests/test_packages.py; review bound account and deployment before promotion.',
            rollback='Disable schedule and execution; reconcile actions and close exposure before removing the package.')
        results.append({'strategy_id': sid, 'proposal_id': prop.id})
    return results


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--deployment', type=Path, required=True)
    parser.add_argument('--wallet-id', required=True)
    parser.add_argument('--account-id', required=True)
    parser.add_argument('--perpl-account-id', required=True)
    parser.add_argument('--cycle-id', default='demo_1')
    parser.add_argument('--output', type=Path, required=True, help='New candidate directory; never active workspace strategies')
    parser.add_argument('--live-testnet-candidate', action='store_true', help='Review live manifest; schedule stays disabled; creates no grant')
    parser.add_argument('--stage-workspace', type=Path, help='Explicit local proposal write; no promotion, grants or trading')
    args = parser.parse_args()
    packages = candidates(args.deployment, wallet_id=args.wallet_id, account_id=args.account_id,
        perpl_account_id=args.perpl_account_id, cycle_id=args.cycle_id)
    if args.live_testnet_candidate:
        for files in packages.values():
            manifest = yaml_io.loads(files['strategy.yml'])
            manifest['mode'] = 'live'
            manifest['params']['execution_enabled'] = True
            manifest['schedule']['enabled'] = False
            files['strategy.yml'] = yaml_io.dumps(manifest)
    args.output.mkdir(parents=True, exist_ok=False)
    for sid, files in packages.items():
        root = args.output/sid
        root.mkdir()
        for name, text in files.items():
            target = root/name
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(text)
        blockers = static_scan_blockers(load_package_from_dir(root))
        if blockers:
            raise ValueError([row.asdict() for row in blockers])
    try:
        from .integration import plan
    except ImportError:
        from integration import plan
    deployed, _ = deployment(args.deployment)
    manifests = [yaml_io.loads(files['strategy.yml']) for files in packages.values()]
    (args.output/'installation-review.json').write_text(json.dumps(plan(manifests, deployed), indent=2)+chr(10))
    print(json.dumps({'candidates': list(packages), 'execution_enabled': args.live_testnet_candidate,
        'proposals': stage(WorkspacePaths(args.stage_workspace.resolve()), packages) if args.stage_workspace else []}))


if __name__ == '__main__':
    main()
