import contextlib
import importlib.util
import io
import os
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import patch

SOURCE = Path(__file__).resolve().parents[1] / '.github/actions/gradle-workflow/workflow.py'
spec = importlib.util.spec_from_file_location('workflow', SOURCE)
workflow = importlib.util.module_from_spec(spec)
spec.loader.exec_module(workflow)


class Versions(unittest.TestCase):
    def test_version_transitions(self):
        for old, new in [('1.0.0-beta', '1.1.0-alpha'), ('1.0.0', '2.0.0-alpha'), ('1.0.0-alpha', '1.0.0-beta'), ('1.0.0-beta', '1.0.0')]:
            with self.subTest(old=old, new=new):
                workflow.validate_version(old, new, False)

    def test_invalid_or_regressing_versions_fail(self):
        for old, new in [('1.0.0', '1.0.0'), ('1.0.0', '1.0.0-beta'), ('1.0.0-beta', '1.0.0-alpha'), ('2.0.0', '1.9.9'), ('1.0.0', 'custom')]:
            with self.subTest(old=old, new=new), self.assertRaises(ValueError):
                workflow.validate_version(old, new, False)

    def test_disable_checks_bypasses_every_version_check(self):
        for old, new in [('1.0.0', 'custom'), ('2.0.0', '1.0.0'), ('custom', 'custom'), ('1.0.0', '')]:
            workflow.validate_version(old, new, True)


class Release(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.previous = Path.cwd()
        self.root = Path(self.temp.name)
        self.remote = self.root / 'origin.git'
        self.repo = self.root / 'mod'
        self.repo.mkdir()
        self.git_call('init', '--bare', str(self.remote), cwd=self.root)
        self.git_call('init', '-b', '26.2', cwd=self.repo)
        self.git_call('config', 'user.name', 'Test', cwd=self.repo)
        self.git_call('config', 'user.email', 'test@example.invalid', cwd=self.repo)
        (self.repo / 'gradle.properties').write_bytes(b'mod_version = 1.0.0\r\njava_version = 25\r\n')
        (self.repo / '.gitignore').write_text('build/\n', encoding='utf-8')
        self.git_call('add', '.', cwd=self.repo)
        self.git_call('commit', '-m', 'Initial', cwd=self.repo)
        self.git_call('remote', 'add', 'origin', str(self.remote), cwd=self.repo)
        self.git_call('push', 'origin', 'HEAD:refs/heads/26.2', cwd=self.repo)
        self.original = self.git_call('rev-parse', 'HEAD', cwd=self.repo)
        os.chdir(self.repo)
        self.addCleanup(os.chdir, self.previous)
        env = {'CN_JAVA_VERSION': '25', 'CN_VERSION': '1.1.0', 'CN_DISABLE_CHECKS': 'false', 'CN_DRY_RUN': 'true', 'GITHUB_REF': 'refs/heads/26.2', 'GITHUB_REF_NAME': '26.2', 'GITHUB_EVENT_NAME': 'workflow_dispatch'}
        env.update({'CN_LOADER_' + key.upper(): 'false' for key in workflow.LOADERS})
        env.update({'CN_PLATFORM_' + key.upper(): 'false' for key in workflow.PLATFORMS})
        self.environ = patch.dict(os.environ, env)
        self.environ.start()
        self.addCleanup(self.environ.stop)
        self.calls = []
        self.silent = contextlib.redirect_stdout(io.StringIO())
        self.silent.__enter__()
        self.addCleanup(self.silent.__exit__, None, None, None)

    @staticmethod
    def git_call(*args, cwd):
        return subprocess.run(['git', *args], cwd=cwd, check=True, capture_output=True, text=True, encoding='utf-8').stdout.strip()

    def remote_sha(self):
        return self.git_call('rev-parse', 'refs/heads/26.2', cwd=self.remote)

    def gradle(self, *args):
        self.calls.append(args)
        if args[0] == 'build':
            archive = Path('fabric/build/libs/mod.jar')
            archive.parent.mkdir(parents=True, exist_ok=True)
            archive.write_bytes(Path('gradle.properties').read_bytes())
        if args[0].startswith(('publish', ':')) and '--dry-run' not in args:
            self.assertEqual(self.remote_sha(), self.git_call('rev-parse', 'HEAD', cwd=self.repo))

    def test_dry_run_builds_new_version_without_pushing_or_publishing(self):
        workflow.execute('release', self.gradle)
        self.assertEqual(self.remote_sha(), self.original)
        self.assertIn(b'mod_version = 1.1.0\r\n', Path('fabric/build/libs/mod.jar').read_bytes())
        self.assertEqual([args[0] for args in self.calls], ['build', 'sonar', 'publishAll'])
        self.assertIn('--dry-run', self.calls[-1])
        self.assertIn('-Dsonar.qualitygate.wait=true', self.calls[1])

    def test_live_release_builds_once_then_pushes_then_publishes(self):
        os.environ['CN_DRY_RUN'] = 'false'
        workflow.execute('release', self.gradle)
        self.assertNotEqual(self.remote_sha(), self.original)
        self.assertEqual([args[0] for args in self.calls], ['build', 'sonar', 'publishAll', 'publishAll'])
        self.assertNotIn('--dry-run', self.calls[-1])

    def test_build_and_sonar_failures_never_push_or_retry(self):
        for failed_task in ['build', 'sonar']:
            with self.subTest(task=failed_task):
                self.calls.clear()
                os.environ['CN_DISABLE_CHECKS'] = 'true'
                os.environ['CN_DRY_RUN'] = 'false'
                def failed(*args):
                    self.gradle(*args)
                    if args[0] == failed_task:
                        raise subprocess.CalledProcessError(1, args)
                with self.assertRaises(subprocess.CalledProcessError):
                    workflow.execute('release', failed)
                self.assertEqual(self.remote_sha(), self.original)
                self.assertEqual(sum(args[0] == failed_task for args in self.calls), 1)
                self.assertFalse(any(args[0] == 'publishAll' for args in self.calls))

    def test_branch_race_stops_before_publication(self):
        os.environ['CN_DRY_RUN'] = 'false'
        def racing(*args):
            self.gradle(*args)
            if args[0] == 'sonar':
                competing = self.git_call('commit-tree', 'HEAD^{tree}', '-p', self.original, '-m', 'Competing change', cwd=self.repo)
                self.git_call('push', 'origin', competing + ':refs/heads/26.2', cwd=self.repo)
        with self.assertRaisesRegex(ValueError, 'Branch changed'):
            workflow.execute('release', racing)
        self.assertTrue(all('--dry-run' in args for args in self.calls if args[0] == 'publishAll'))

    def test_publishing_configuration_failure_never_pushes(self):
        os.environ['CN_DRY_RUN'] = 'false'
        def failed(*args):
            self.gradle(*args)
            if args[0] == 'publishAll':
                raise subprocess.CalledProcessError(1, args)
        with self.assertRaises(subprocess.CalledProcessError):
            workflow.execute('release', failed)
        self.assertEqual(self.remote_sha(), self.original)

    def test_changed_jar_stops_before_push(self):
        os.environ['CN_DRY_RUN'] = 'false'
        def tampered(*args):
            self.gradle(*args)
            if args[0] == 'sonar':
                Path('fabric/build/libs/mod.jar').write_bytes(b'changed')
        with self.assertRaisesRegex(ValueError, 'artifacts changed'):
            workflow.execute('release', tampered)
        self.assertEqual(self.remote_sha(), self.original)

    def test_single_release_combines_selected_tasks_in_one_invocation(self):
        os.environ.update(CN_LOADER_FABRIC='true', CN_LOADER_NEOFORGE='true', CN_PLATFORM_MAVEN='true', CN_PLATFORM_MODRINTH='true')
        workflow.execute('single-release', self.gradle)
        self.assertEqual(self.remote_sha(), self.original)
        self.assertEqual(self.calls[-1][:4], (':fabric:publish', ':fabric:publishModrinth', ':neoforge:publish', ':neoforge:publishModrinth'))

    def test_no_selection_fails_before_build(self):
        for overrides in [{}, {'CN_LOADER_FABRIC': 'true'}, {'CN_PLATFORM_MAVEN': 'true'}]:
            with self.subTest(overrides=overrides), patch.dict(os.environ, overrides), self.assertRaises(ValueError):
                workflow.execute('single-release', self.gradle)
        self.assertEqual(self.calls, [])

    def test_common_without_maven_fails_before_build(self):
        os.environ.update(CN_LOADER_COMMON='true', CN_PLATFORM_GITHUB='true')
        with self.assertRaisesRegex(ValueError, 'Common alone requires'):
            workflow.execute('single-release', self.gradle)
        self.assertEqual(self.calls, [])

    def test_custom_version_is_data_and_crlf_is_preserved(self):
        requested = 'custom-"; $(touch SHOULD_NOT_EXIST) & / \\ test'
        workflow.prepare_version(requested)
        text, match = workflow.property_text()
        self.assertEqual(workflow.unescape(match[2]), requested)
        self.assertEqual(text.count('\r\n'), 2)
        self.assertFalse(Path('SHOULD_NOT_EXIST').exists())

    def test_pr_analysis_uses_scanner_pr_detection(self):
        os.environ['GITHUB_EVENT_NAME'] = 'pull_request'
        workflow.execute('analyze', self.gradle)
        self.assertFalse(any(arg.startswith('-Dsonar.branch.name=') for arg in self.calls[-1]))

    def test_missing_java_fails_without_a_fallback(self):
        os.environ['CN_JAVA_VERSION'] = ''
        with self.assertRaisesRegex(ValueError, 'java-version is required'):
            workflow.execute('release', self.gradle)


if __name__ == '__main__':
    unittest.main()
