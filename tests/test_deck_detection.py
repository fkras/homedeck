"""Telling whether deck.py is already running.

server.py won't start a second deck.py, and reports the deck stopped when it
thinks none runs. It used to look for "deck.py" anywhere in any process's
command line: a leftover `python -c` whose code mentioned deck.py in a
comment made a fresh server refuse to start the deck, and say it was
stopped, for as long as that process lived.
"""

import os
import sys

import psutil

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.realpath(__file__))))

import server  # noqa: E402
from server import is_deck_process, script_of  # noqa: E402

REPO = os.path.dirname(server.SCRIPT_PATH)
VENV_PYTHON = '/opt/homedeck/.venv/bin/python'


class TestScriptOf:
    def test_the_server_launch(self):
        assert script_of([VENV_PYTHON, '-u', '/opt/homedeck/deck.py']) == '/opt/homedeck/deck.py'

    def test_a_relative_script(self):
        assert script_of(['python3', 'deck.py']) == 'deck.py'

    def test_options_with_values(self):
        assert script_of(['python3.11', '-X', 'dev', '-W', 'ignore', 'deck.py']) == 'deck.py'
        assert script_of(['python3', '-Xdev', 'deck.py']) == 'deck.py'

    def test_code_and_modules_are_not_scripts(self):
        assert script_of(['python3', '-c', 'import deck']) is None
        assert script_of(['python3', '-uc', 'print("deck.py")']) is None
        assert script_of(['python3', '-m', 'deck']) is None

    def test_arguments_after_the_script_are_its_own(self):
        assert script_of(['python3', 'other.py', 'deck.py']) == 'other.py'

    def test_other_programs(self):
        assert script_of(['vim', 'deck.py']) is None
        assert script_of(['/usr/bin/less', '/opt/homedeck/deck.py']) is None
        assert script_of([]) is None

    def test_macos_framework_python(self):
        python = '/Library/Frameworks/Python.framework/Versions/3.9/Resources/Python.app/Contents/MacOS/Python'
        assert script_of([python, 'deck.py']) == 'deck.py'


class TestIsDeckProcess:
    def test_this_repos_deck_by_absolute_path(self):
        assert is_deck_process([VENV_PYTHON, '-u', server.SCRIPT_PATH], cwd='/')

    def test_this_repos_deck_run_from_the_repo(self):
        assert is_deck_process(['python3', 'deck.py'], cwd=REPO)

    def test_another_projects_deck(self, tmp_path):
        assert not is_deck_process(['python3', str(tmp_path / 'deck.py')], cwd='/')
        assert not is_deck_process(['python3', 'deck.py'], cwd=str(tmp_path))

    def test_through_a_symlink(self, tmp_path):
        link = tmp_path / 'homedeck'
        link.symlink_to(REPO)

        assert is_deck_process(['python3', str(link / 'deck.py')], cwd='/')

    def test_a_deck_whose_directory_cant_be_seen_counts(self):
        # Another user's process: better not to start a second deck
        assert is_deck_process(['python3', 'deck.py'], cwd=None)

    def test_the_python_c_that_caused_the_false_positive(self):
        code = (
            "\nimport sys,os; sys.path.insert(0,'src'); sys.path.insert(0,'.')\n"
            "os.environ['MDNS_SERVICE_ID']='test-deck'\nimport server, uvicorn\n"
            "# don't spawn deck.py\n"
        )
        python = '/Library/Developer/CommandLineTools/Library/Frameworks/Python3.framework/Versions/3.9/Resources/Python.app/Contents/MacOS/Python'

        assert not is_deck_process([python, '-c', code], cwd=REPO)

    def test_an_editor_with_deck_py_open(self):
        assert not is_deck_process(['vim', 'deck.py'], cwd=REPO)


class FakeProcess:
    def __init__(self, cmdline, cwd=REPO, error=None):
        self._info = {'pid': 1, 'cmdline': cmdline, 'cwd': cwd}
        self._error = error

    @property
    def info(self):
        if self._error:
            raise self._error
        return self._info


class TestIsScriptRunning:
    def test_finds_a_running_deck(self, monkeypatch):
        processes = [FakeProcess(['bash']), FakeProcess([VENV_PYTHON, '-u', server.SCRIPT_PATH])]
        monkeypatch.setattr(psutil, 'process_iter', lambda attrs=None: iter(processes))

        assert server.is_script_running()

    def test_ignores_processes_that_only_mention_it(self, monkeypatch):
        processes = [
            FakeProcess(['python3', '-c', '# deck.py']),
            FakeProcess(['vim', 'deck.py']),
            FakeProcess(['python3', '/somewhere/else/deck.py']),
        ]
        monkeypatch.setattr(psutil, 'process_iter', lambda attrs=None: iter(processes))

        assert not server.is_script_running()

    def test_skips_processes_that_vanish(self, monkeypatch):
        processes = [FakeProcess([], error=psutil.NoSuchProcess(1)), FakeProcess(['python3', 'deck.py'])]
        monkeypatch.setattr(psutil, 'process_iter', lambda attrs=None: iter(processes))

        assert server.is_script_running()
