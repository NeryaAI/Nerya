"""Current validation receipts are evidence, never proposal approval."""
import json
import shutil
import sys
from types import SimpleNamespace

import pytest

from nerya.core.config import Config
from nerya.core.paths import WorkspacePaths
from nerya.evolution.patch_proposal import list_proposals, set_state
from nerya.evolution.promotion import apply_proposal, proposal_action_gates
from nerya.evolution.strategy_code_generator import StrategyCodeGenerator, StrategyGenerationRequest
from nerya.evolution.candidate_bundle import verify_candidate_bundle
from nerya.core import yaml_io
from nerya.strategies.workflow_service import view_workflow
from nerya.tools.native import strategy_runtime
from nerya.tools.types import ToolCall
from nerya.sdk.strategy_api import StrategyAPI
from nerya.api.routes_strategies_runtime import routes

pytestmark = pytest.mark.smoke

VALID_MAIN = "def run(ctx):\n    return ctx.result.hold(reason='fixture')\n"


def draft(tmp_path):
    cfg = Config(paths=WorkspacePaths(tmp_path), data={})
    result = strategy_runtime.strategy_draft_proposal_handler(
        ToolCall(name="strategy_draft_proposal", arguments={
            "strategy_id": "receipt_fixture", "markets": ["mock:BTC/USDT"],
            "accounts": ["paper_main"], "create_tuning": False,
            "files": {
                "main.py": "def run(ctx)\n    return None\n",
                "tests/test_contract.py": (
                    "def test_one(): assert False\n"
                    "def test_two(): assert False\n"
                    "def test_three(): assert True\n"
                    "def test_four(): assert True\n"
                    "def test_five(): assert True\n"
                ),
            },
        }), config=cfg,
    )
    assert not result.is_error, result.error
    proposal = next(p for p in list_proposals(cfg.paths)
                    if p.id == result.content[0].data["proposal_id"])
    report = json.loads((proposal.path / "validation_report.json").read_text())
    assert any(issue["code"] == "syntax_error" for issue in report["blockers"])
    return cfg, proposal, report


def validate(cfg, proposal, entrypoint="native"):
    if entrypoint != "native":
        client = SimpleNamespace(strategy=StrategyAPI(cfg, None))
        if entrypoint == "sdk":
            return client.strategy.validate(proposal_id=proposal.id)
        handler = next(h for method, path, h in routes()
                       if (method, path) == ("POST", "/strategies/runtime/validate"))
        return handler(client, {"proposal_id": proposal.id})
    return strategy_runtime.strategy_validate_handler(
        ToolCall(name="strategy_validate", arguments={"proposal_id": proposal.id}),
        config=cfg,
    )


@pytest.mark.parametrize("entrypoint", ["sdk", "native"])
@pytest.mark.parametrize("during_validation", [False, True])
def test_saved_artifacts_do_not_change_source_identity(tmp_path, monkeypatch, entrypoint, during_validation):
    from nerya.strategies import validation_receipt
    from nerya.strategies.workflow_service import source_files
    from nerya.strategies.verification import source_revision
    from nerya.strategies.workflow_graph import package_revision
    cfg, proposal, _ = draft(tmp_path)
    set_state(cfg.paths, proposal.id, "pending_review")
    sealed = (proposal.path / "validation_report.json").read_bytes()
    root = proposal.path / "after/strategies/receipt_fixture"
    (root / "main.py").write_text(VALID_MAIN)
    helper = root / "helper.py"
    helper.write_text("VALUE = 1\n")
    (root / 'workflow.json').write_text('{"version":1,"nodes":{},"edges":[]}\n')
    artifacts = root / "backtests/saved"
    artifacts.mkdir(parents=True)
    for name in ("metrics.json", "run.json", "report.md"):
        (artifacts / name).write_text("{}\n")
    real_validate = validation_receipt.validate_proposal_files
    seen = {}

    def structural_only(**kwargs):
        seen.update(kwargs["files"])
        # Run output can also arrive while validation is in flight.
        if during_validation:
            (artifacts / "later.json").write_text("{}\n")
        return real_validate(**kwargs, smoke_test=False)

    monkeypatch.setattr(validation_receipt, "validate_proposal_files", structural_only)
    result = validate(cfg, proposal, entrypoint)
    if entrypoint == "native":
        assert not result.is_error, result.error
    data = result.content[0].data if entrypoint == "native" else result
    assert data["ok"]
    view = view_workflow(cfg.paths, "receipt_fixture", proposal.id)
    assert view["validation_receipt"]["status"] == "current"
    files = source_files(cfg.paths, "receipt_fixture", proposal.id)[0]
    assert data["source_revision"] == source_revision(files)
    assert data["source_revision"] != package_revision(files)
    if entrypoint == 'native':
        from nerya.strategies.tool_verification import source_binding
        assert source_binding(cfg, ToolCall(name='strategy_backtest', arguments={
            'proposal_id': proposal.id}))['revision'] == data['source_revision']
    assert seen == files
    package_hash = data['package_hash']
    (root / 'workflow.json').write_text('{"version":1,"nodes":{"script:main.py":{"position":{"x":1,"y":2}}},"edges":[]}\n')
    after_layout = view_workflow(cfg.paths, "receipt_fixture", proposal.id)
    assert after_layout['validation_receipt']['status'] == 'current'
    assert after_layout['validation_receipt']['report']['package_hash'] == package_hash
    assert "helper.py" in seen
    runs = root / "runs"
    runs.mkdir()
    (runs / "output.json").write_text("{}\n")
    assert view_workflow(cfg.paths, "receipt_fixture", proposal.id)["validation_receipt"]["status"] == "current"
    helper.write_text("VALUE = 2\n")
    assert view_workflow(cfg.paths, "receipt_fixture", proposal.id)["validation_receipt"]["status"] == "stale"
    assert (proposal.path / "validation_report.json").read_bytes() == sealed


@pytest.mark.parametrize("kind", ["oversized", "binary", "symlink"])
@pytest.mark.parametrize("during_validation", [False, True])
def test_omitted_source_never_saves_receipt(tmp_path, monkeypatch, kind, during_validation):
    from nerya.core.errors import NeryaError
    from nerya.strategies import validation_receipt
    from nerya.strategies.workflow_graph import MAX_FILE_BYTES
    cfg, proposal, _ = draft(tmp_path)
    sealed = (proposal.path / "validation_report.json").read_bytes()
    root = proposal.path / "after/strategies/receipt_fixture"

    def add_omitted():
        path = root / "helper.py"
        if kind == "symlink":
            path.symlink_to(root / "main.py")
        else:
            path.write_bytes(b"#" * (MAX_FILE_BYTES + 1) if kind == "oversized" else b"\xff")

    real_validate = validation_receipt.validate_proposal_files

    def structural_only(**kwargs):
        if not during_validation:
            pytest.fail("Incomplete source must not be validated")
        report = real_validate(**kwargs, smoke_test=False)
        add_omitted()
        return report

    monkeypatch.setattr(validation_receipt, "validate_proposal_files", structural_only)
    if not during_validation:
        add_omitted()
    with pytest.raises(NeryaError, match="revision_conflict|incomplete_source"):
        validate(cfg, proposal, "sdk")
    assert (proposal.path / "validation_report.json").read_bytes() == sealed


@pytest.mark.parametrize("identity", ["metadata", "target", "directory", "unsafe", "ambiguous", "symlink"])
def test_proposal_identity_uses_bounded_safe_lookup(tmp_path, monkeypatch, identity):
    from nerya.core.errors import NeryaError
    from nerya.strategies import validation_receipt
    cfg, proposal, _ = draft(tmp_path)
    sealed = (proposal.path / "validation_report.json").read_bytes()
    proposal.metadata = {}
    proposal.target = None
    after = proposal.path / "after/strategies"
    if identity == "metadata":
        proposal.metadata["strategy_id"] = "receipt_fixture"
    elif identity == "target":
        proposal.target = "strategies/receipt_fixture"
    elif identity == "unsafe":
        proposal.metadata["strategy_id"] = "../receipt_fixture"
    elif identity == "ambiguous":
        (after / "other").mkdir()
    elif identity == "symlink":
        (after / "alias").symlink_to(after / "receipt_fixture", target_is_directory=True)
    monkeypatch.setattr(validation_receipt, "list_proposals", lambda paths: [proposal])
    if identity in {"unsafe", "ambiguous", "symlink"}:
        with pytest.raises(NeryaError, match="Invalid|Ambiguous|Symlink"):
            validate(cfg, proposal, "sdk")
        assert (proposal.path / "validation_report.json").read_bytes() == sealed
    else:
        result = validate(cfg, proposal, "sdk")
        assert result["strategy_id"] == "receipt_fixture"
        assert view_workflow(cfg.paths, "receipt_fixture", proposal.id)["validation_receipt"]["status"] == "current"


@pytest.mark.parametrize("state", ["draft", "pending_review", "approved"])
def test_ui_route_persists_current_receipt(tmp_path, state):
    cfg, proposal, original = draft(tmp_path)
    set_state(cfg.paths, proposal.id, state)
    meta = (proposal.path / "proposal.yml").read_bytes()
    sealed = (proposal.path / "validation_report.json").read_bytes()
    report_path = proposal.path / ("validation_report.json" if state == "draft" else "latest_validation_receipt.json")
    main = proposal.path / "after/strategies/receipt_fixture/main.py"
    main.write_text("def run(ctx)\n    return 'edited'\n")
    client = SimpleNamespace(strategy=StrategyAPI(cfg, None))
    handler = next(h for method, path, h in routes()
                   if (method, path) == ("POST", "/strategies/runtime/validate"))
    result = handler(client, {"proposal_id": proposal.id})
    saved = json.loads(report_path.read_text())
    assert saved["package_hash"] == result["package_hash"] != original["package_hash"]
    assert saved == result
    assert view_workflow(cfg.paths, "receipt_fixture", proposal.id)["validation_receipt"]["status"] == "current"
    native = validate(cfg, proposal).content[0].data
    refreshed = json.loads(report_path.read_text())
    assert all(native[key] == value for key, value in refreshed.items())
    assert {k: v for k, v in refreshed.items() if k != "checked_at"} == {
        k: v for k, v in saved.items() if k != "checked_at"}
    assert (proposal.path / "proposal.yml").read_bytes() == meta
    if state != "draft":
        assert (proposal.path / "validation_report.json").read_bytes() == sealed
    main.write_text(VALID_MAIN)
    assert view_workflow(cfg.paths, "receipt_fixture", proposal.id)["validation_receipt"]["status"] == "stale"


@pytest.mark.parametrize("entrypoint", ["native", "sdk", "route"])
def test_receipt_redacts_diagnostics_but_preserves_source_hash(tmp_path, monkeypatch, entrypoint):
    from nerya.strategies import validation_receipt
    cfg, proposal, _ = draft(tmp_path)
    real_validate = validation_receipt.validate_proposal_files
    secret = "sk-" + "TestCredentialOnly0123456789" * 2

    def diagnostic(**kwargs):
        report = real_validate(**kwargs)
        report.issues[0].message = secret
        report.behavioral["failures"] = [{"message": secret, "api_key": secret}]
        return report

    monkeypatch.setattr(validation_receipt, "validate_proposal_files", diagnostic)
    result = validate(cfg, proposal, entrypoint)
    data = result.content[0].data if entrypoint == "native" else result
    saved = (proposal.path / "validation_report.json").read_text()
    assert secret not in saved
    assert secret not in json.dumps(data)
    assert "REDACTED" in saved
    assert len(data["package_hash"]) == len(data["source_revision"]) == 64
    assert view_workflow(cfg.paths, "receipt_fixture", proposal.id)["validation_receipt"]["status"] == "current"


def test_installed_validation_keeps_existing_contract_without_receipt_writes(tmp_path):
    from nerya.strategies.validator import validate_strategy_package
    cfg, proposal, _ = draft(tmp_path)
    installed = cfg.paths.strategy("receipt_fixture")
    shutil.copytree(proposal.path / "after/strategies/receipt_fixture", installed)
    before = {p.relative_to(installed): p.read_bytes() for p in installed.rglob("*") if p.is_file()}
    expected = validate_strategy_package(cfg.paths, "receipt_fixture").asdict()
    assert StrategyAPI(cfg, None).validate("receipt_fixture") == expected
    native = strategy_runtime.strategy_validate_handler(
        ToolCall(name="strategy_validate", arguments={"strategy_id": "receipt_fixture"}), config=cfg,
    ).content[0].data
    assert all(native[key] == value for key, value in expected.items())
    assert {p.relative_to(installed): p.read_bytes() for p in installed.rglob("*") if p.is_file()} == before
    assert not (installed / "validation_report.json").exists()


@pytest.mark.skipif(sys.platform != "darwin", reason="real isolated candidate runner")
@pytest.mark.parametrize("entrypoint", ["native", "sdk", "route"])
def test_revalidate_replaces_syntax_receipt_with_failed_behavior(tmp_path, entrypoint):
    cfg, proposal, original = draft(tmp_path)
    meta = (proposal.path / "proposal.yml").read_bytes()
    main = proposal.path / "after/strategies/receipt_fixture/main.py"
    main.write_text(VALID_MAIN)
    result = validate(cfg, proposal, entrypoint)
    data = result.content[0].data if entrypoint == "native" else result
    assert data["structural"]["status"] == "passed"
    assert data["behavioral"]["summary"]["failed"] == 2
    assert data["behavioral"]["summary"]["passed"] == 3
    saved = json.loads((proposal.path / "validation_report.json").read_text())
    assert saved["package_hash"] == data["package_hash"] != original["package_hash"]
    assert saved["structural"]["status"] == "passed"
    assert saved["behavioral"] == data["behavioral"]
    assert saved["ok"] is False
    assert saved["source_revision"] and saved["checked_at"]
    assert (proposal.path / "proposal.yml").read_bytes() == meta
    assert next(p for p in list_proposals(cfg.paths) if p.id == proposal.id).state == "draft"
    assert not cfg.paths.strategy("receipt_fixture").exists()
    # A fresh report cannot reseal or authorize the edited candidate.
    bundle = yaml_io.loads(meta.decode())["candidate_bundle"]
    check = verify_candidate_bundle(cfg.paths.root, proposal.path, bundle)
    assert not check["ok"]
    assert "validation_report_digest" in check["mismatches"]
    current = view_workflow(cfg.paths, "receipt_fixture", proposal.id)["validation_receipt"]
    assert current["status"] == "current"
    assert current["report"]["behavioral"]["summary"]["failed"] == 2
    main.write_text(VALID_MAIN + "\n# later edit\n")
    stale = view_workflow(cfg.paths, "receipt_fixture", proposal.id)["validation_receipt"]
    assert stale["status"] == "stale"
    assert stale["report"] is None


@pytest.mark.parametrize("entrypoint", ["native", "sdk", "route"])
@pytest.mark.parametrize("state", ["draft", "pending_review"])
def test_concurrent_edit_does_not_replace_receipt_or_claim_success(tmp_path, monkeypatch, entrypoint, state):
    cfg, proposal, _ = draft(tmp_path)
    report_path = proposal.path / "validation_report.json"
    original = report_path.read_bytes()
    set_state(cfg.paths, proposal.id, state)
    meta = (proposal.path / "proposal.yml").read_bytes()
    main = proposal.path / "after/strategies/receipt_fixture/main.py"
    main.write_text(VALID_MAIN)
    from nerya.strategies import validation_receipt
    real_validate = validation_receipt.validate_proposal_files

    def edit_during_validation(**kwargs):
        report = real_validate(**kwargs, smoke_test=False)
        assert report.ok
        main.write_text(VALID_MAIN + "\n# concurrent edit\n")
        return report

    monkeypatch.setattr(validation_receipt, "validate_proposal_files", edit_during_validation)
    if entrypoint == "sdk":
        from nerya.core.errors import NeryaError
        with pytest.raises(NeryaError, match="revision_conflict"):
            validate(cfg, proposal, entrypoint)
    else:
        result = validate(cfg, proposal, entrypoint)
        if entrypoint == "native":
            assert result.is_error
            assert "revision_conflict" in result.error.message
            assert result.semantic_success is False
        else:
            assert result["ok"] is False
            assert "revision_conflict" in result["error"]
    assert report_path.read_bytes() == original
    assert (proposal.path / "proposal.yml").read_bytes() == meta
    assert not (proposal.path / "latest_validation_receipt.json").exists()


@pytest.mark.skipif(sys.platform != "darwin", reason="real isolated candidate runner")
@pytest.mark.parametrize("state", ["pending_review", "approved"])
@pytest.mark.parametrize("entrypoint", ["native", "sdk", "route"])
def test_repeated_validation_preserves_review_seal_and_apply(tmp_path, state, entrypoint):
    cfg = Config(paths=WorkspacePaths(tmp_path), data={})
    generated = StrategyCodeGenerator(cfg.paths).generate(
        StrategyGenerationRequest(
            strategy_id="receipt_fixture", markets=("mock:BTC/USDT",),
            accounts=("paper_main",), create_tuning=False,
            files={"main.py": VALID_MAIN,
                   "tests/test_contract.py": "def test_contract(): assert True\n"},
        ), initial_state="pending_review", require_valid=True,
    )
    proposal = generated.proposal
    if state == "approved":
        set_state(cfg.paths, proposal.id, state)
    before = {name: (proposal.path / name).read_bytes()
              for name in ("proposal.yml", "validation_report.json")}
    bundle = yaml_io.loads(before["proposal.yml"].decode())["candidate_bundle"]
    assert verify_candidate_bundle(cfg.paths.root, proposal.path, bundle)["ok"]
    for _ in range(2):
        result = validate(cfg, proposal, entrypoint)
        if entrypoint == "native":
            assert not result.is_error, result.error
            result = result.content[0].data
        assert result["ok"]
        assert {name: (proposal.path / name).read_bytes() for name in before} == before
        assert verify_candidate_bundle(cfg.paths.root, proposal.path, bundle)["ok"]
        receipt = view_workflow(cfg.paths, "receipt_fixture", proposal.id)["validation_receipt"]
        assert receipt["status"] == "current" and receipt["report"]["ok"]
        assert receipt["report"]["checked_at"]
    assert json.loads((proposal.path / "latest_validation_receipt.json").read_text())["ok"]
    set_state(cfg.paths, proposal.id, "approved")
    proposal = next(p for p in list_proposals(cfg.paths) if p.id == proposal.id)
    assert proposal_action_gates(cfg.paths, proposal)["can_apply"]
    applied = apply_proposal(cfg.paths, proposal.id)
    assert applied["ok"], applied
    assert (cfg.paths.strategy("receipt_fixture") / "main.py").read_text() == VALID_MAIN
