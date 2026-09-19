"""Output stream merging is not a filesystem-write exception."""
import pytest
from nerya.tools.native.shell import _shell_may_mutate_files, _command_heads, _absolute_path_escape

pytestmark = pytest.mark.smoke

@pytest.mark.parametrize('command',[
 'python -m pytest tests/test_contract.py -q 2>&1 | tail -20',
 'cd /workspace && python -m pytest tests/test_contract.py -q 2>&1 | tail -20',
 'PYTHONDONTWRITEBYTECODE=1 python -m pytest -p no:cacheprovider /workspace/tests/test_contract.py -q 2>&1',
 'npm run test 2>&1',
])
def test_test_output_merge_preserves_project_cwd(command):
    assert not _shell_may_mutate_files(command, _command_heads(command))

@pytest.mark.parametrize('command',[
 'python -m pytest tests/test_contract.py > /elsewhere/output.txt',
 'python -m pytest tests/test_contract.py 2>&1 | tee /elsewhere/output.txt',
 'python -m pytest tests/test_contract.py 2>&1 && rm -rf /elsewhere',
 'python -m pytest tests/test_contract.py 2>&1; echo x > /elsewhere/output.txt',
 'python -m pytest tests/test_contract.py 2>&12',
])
def test_real_writes_remain_writes(command):
    assert _shell_may_mutate_files(command, _command_heads(command))


def test_output_merge_does_not_weaken_workspace_path_check(tmp_path):
    command='python -m pytest /outside/tests/test_contract.py -q 2>&1'
    assert _absolute_path_escape(command,root=tmp_path)


@pytest.mark.parametrize('args, expected',[
    ({'timeout_sec':120},120),
    ({'timeout_s':60},60),
    ({'timeout':20},20),
    ({'timeout_sec':120,'timeout_s':3},120),
    ({'timeout_sec':999999},300),
])
def test_public_timeout_schema_reaches_executor(args,expected,tmp_path,monkeypatch):
    from types import SimpleNamespace
    from nerya.tools.native import shell
    from nerya.tools.types import ToolCall
    received={}
    def execute(command,**options):
        received.update(options)
        return SimpleNamespace(returncode=0,stdout='fixture passed',stderr='',elapsed_ms=1)
    monkeypatch.setattr(shell,'sandbox_exec',execute)
    result=shell.run_shell_handler(ToolCall(name='run_shell',arguments={'command':'python -m pytest tests/test_contract.py',**args}),root=tmp_path,session_id='fixture')
    assert not result.is_error
    assert received['timeout']==expected
