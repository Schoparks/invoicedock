"""Exercise updater behavior against an isolated local Git remote, without pip or network."""
from contextlib import redirect_stdout
from io import StringIO
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import patch

import update


class UpdateTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.origin = self.root / 'origin'
        self.checkout = self.root / 'checkout'
        self.command('init', '-b', 'main', str(self.origin))
        self.command('-C', str(self.origin), 'config', 'user.name', 'Fixture')
        self.command('-C', str(self.origin), 'config', 'user.email', 'fixture@example.test')
        self.commit({'requirements.lock.txt': 'fixture==1\n', '.gitignore': '.env\ndata/\n'})
        self.command('clone', str(self.origin), str(self.checkout))
        (self.checkout / '.env').write_text('synthetic private config')
        (self.checkout / 'data').mkdir()
        (self.checkout / 'data/fixture.txt').write_text('synthetic private data')

    def command(self, *args):
        return subprocess.check_output(['git', *args], stderr=subprocess.STDOUT, text=True).strip()

    def commit(self, files):
        for name, content in files.items():
            (self.origin / name).write_text(content)
        self.command('-C', str(self.origin), 'add', '.')
        self.command('-C', str(self.origin), 'commit', '-m', 'Fixture change')

    def execute(self):
        original_run = subprocess.run
        installations = []
        def run(args, **kwargs):
            if args[1:4] == ['-m', 'pip', 'install']:
                installations.append(args)
                return subprocess.CompletedProcess(args, 0)
            if args[0] == 'git' and 'pull' in args:
                kwargs.setdefault('stdout', subprocess.PIPE)
                kwargs.setdefault('stderr', subprocess.PIPE)
            return original_run(args, **kwargs)
        with patch.object(update, 'dependencies_match', return_value=True), patch.object(update.subprocess, 'run', side_effect=run), redirect_stdout(StringIO()):
            update.update(self.checkout)
        return installations

    def test_fast_forward_preserves_private_files_and_skips_unchanged_dependencies(self):
        self.commit({'feature.txt': 'new source'})
        self.assertEqual(self.execute(), [])
        self.assertEqual((self.checkout / 'feature.txt').read_text(), 'new source')
        self.assertEqual((self.checkout / '.env').read_text(), 'synthetic private config')
        self.assertEqual((self.checkout / 'data/fixture.txt').read_text(), 'synthetic private data')

    def test_changed_lock_installs_with_running_interpreter(self):
        self.commit({'requirements.lock.txt': 'fixture==2\n'})
        installations = self.execute()
        self.assertEqual(len(installations), 1)
        self.assertEqual(installations[0][0], update.sys.executable)
        self.assertEqual(installations[0][-1], str((self.checkout / 'requirements.lock.txt').resolve()))

    def test_dirty_checkout_is_rejected_before_pulling(self):
        self.commit({'feature.txt': 'remote change'})
        (self.checkout / 'local.txt').write_text('keep local work')
        previous = self.command('-C', str(self.checkout), 'rev-parse', 'HEAD')
        with self.assertRaisesRegex(RuntimeError, '未提交'):
            self.execute()
        self.assertEqual(self.command('-C', str(self.checkout), 'rev-parse', 'HEAD'), previous)
        self.assertEqual((self.checkout / 'local.txt').read_text(), 'keep local work')

    def test_diverged_history_is_rejected_without_resetting(self):
        self.command('-C', str(self.checkout), 'config', 'user.name', 'Fixture')
        self.command('-C', str(self.checkout), 'config', 'user.email', 'fixture@example.test')
        (self.checkout / 'local.txt').write_text('committed local work')
        self.command('-C', str(self.checkout), 'add', '.')
        self.command('-C', str(self.checkout), 'commit', '-m', 'Local fixture')
        previous = self.command('-C', str(self.checkout), 'rev-parse', 'HEAD')
        self.commit({'feature.txt': 'remote change'})
        with self.assertRaises(subprocess.CalledProcessError):
            self.execute()
        self.assertEqual(self.command('-C', str(self.checkout), 'rev-parse', 'HEAD'), previous)
        self.assertEqual((self.checkout / 'local.txt').read_text(), 'committed local work')
