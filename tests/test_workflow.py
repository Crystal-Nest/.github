import contextlib
import importlib.util
import io
import json
import os
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import patch
from urllib.error import URLError

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


class Sonar(unittest.TestCase):
    def test_gate_lookup_uses_the_reported_task_and_exact_analysis(self):
        with tempfile.TemporaryDirectory() as temp:
            metadata = Path(temp) / 'report-task.txt'
            metadata.write_text('serverUrl=https://sonarcloud.io\nceTaskId=task-123\n', encoding='utf-8')
            responses = [io.StringIO(json.dumps({'task': {'status': 'SUCCESS', 'analysisId': 'analysis-456'}})), io.StringIO(json.dumps({'projectStatus': {'status': 'NONE'}}))]
            with patch.dict(os.environ, SONAR_TOKEN='test-token'), patch.object(workflow, 'urlopen', side_effect=responses) as request:
                self.assertEqual(workflow.sonar_gate_status(metadata), 'NONE')
            urls = [call.args[0].full_url for call in request.call_args_list]
            self.assertEqual(urls, ['https://sonarcloud.io/api/ce/task?id=task-123', 'https://sonarcloud.io/api/qualitygates/project_status?analysisId=analysis-456'])
            self.assertEqual(request.call_args_list[0].args[0].get_header('Authorization'), 'Bearer test-token')

    def test_status_lookup_errors_and_unfinished_tasks_do_not_allow_retry(self):
        with tempfile.TemporaryDirectory() as temp:
            metadata = Path(temp) / 'report-task.txt'
            with contextlib.redirect_stdout(io.StringIO()):
                self.assertIsNone(workflow.sonar_gate_status(metadata))
                metadata.write_text('serverUrl=https://sonarcloud.io\nceTaskId=task-123\n', encoding='utf-8')
                for response in [{'task': {'status': 'PENDING'}}, {'task': {'status': 'FAILED'}}, {'task': {'status': 'SUCCESS'}}, {}]:
                    with self.subTest(response=response), patch.dict(os.environ, SONAR_TOKEN='test-token'), patch.object(workflow, 'urlopen', return_value=io.StringIO(json.dumps(response))) as request:
                        self.assertIsNone(workflow.sonar_gate_status(metadata))
                        self.assertEqual(request.call_count, 1)
                with patch.dict(os.environ, SONAR_TOKEN='test-token'), patch.object(workflow, 'urlopen', side_effect=URLError('unavailable')):
                    self.assertIsNone(workflow.sonar_gate_status(metadata))

    def test_recorded_command_output_is_visible_and_attached_to_failure(self):
        with contextlib.redirect_stdout(io.StringIO()) as output:
            with self.assertRaises(subprocess.CalledProcessError) as error:
                workflow.run([os.sys.executable, '-c', 'print("QUALITY GATE STATUS: FAILED", flush=True); raise SystemExit(1)'], record=True)
        self.assertIn('QUALITY GATE STATUS: FAILED', output.getvalue())
        self.assertEqual(error.exception.output.strip(), 'QUALITY GATE STATUS: FAILED')


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

    def sonar_failure(self, *args):
        self.gradle(*args)
        if args[0] == 'sonar':
            metadata = Path(next(arg.split('=', 1)[1] for arg in args if arg.startswith('-Dsonar.scanner.metadataFilePath=')))
            self.assertFalse(metadata.exists(), 'Each scan must start without stale metadata')
            metadata.write_text('serverUrl=https://sonarcloud.io\nceTaskId=this-scan\n', encoding='utf-8')
            raise subprocess.CalledProcessError(1, args, output='QUALITY GATE STATUS: FAILED - View details on https://sonarcloud.io/dashboard')

    def test_none_retries_sonar_only_then_publishes_after_a_passing_gate(self):
        os.environ['CN_DRY_RUN'] = 'false'
        def first_none(*args):
            if args[0] == 'sonar' and not any(call[0] == 'sonar' for call in self.calls):
                self.sonar_failure(*args)
            else:
                self.gradle(*args)
        with patch.object(workflow, 'sonar_gate_status', return_value='NONE') as status:
            workflow.execute('release', first_none)
        self.assertEqual(status.call_count, 1)
        self.assertEqual([args[0] for args in self.calls], ['build', 'sonar', 'sonar', 'publishAll', 'publishAll'])
        self.assertNotEqual(self.remote_sha(), self.original)
        self.assertEqual(self.calls[1], self.calls[2])

    def test_none_retry_keeps_dry_run_without_pushing_or_publishing(self):
        def first_none(*args):
            if args[0] == 'sonar' and not any(call[0] == 'sonar' for call in self.calls):
                self.sonar_failure(*args)
            else:
                self.gradle(*args)
        with patch.object(workflow, 'sonar_gate_status', return_value='NONE'):
            workflow.execute('release', first_none)
        self.assertEqual(self.remote_sha(), self.original)
        self.assertEqual([args[0] for args in self.calls], ['build', 'sonar', 'sonar', 'publishAll'])
        self.assertIn('--dry-run', self.calls[-1])

    def test_second_sonar_failure_is_final_even_if_none(self):
        os.environ['CN_DRY_RUN'] = 'false'
        with patch.object(workflow, 'sonar_gate_status', return_value='NONE') as status, self.assertRaises(subprocess.CalledProcessError):
            workflow.execute('release', self.sonar_failure)
        self.assertEqual(status.call_count, 1)
        self.assertEqual([args[0] for args in self.calls], ['build', 'sonar', 'sonar'])
        self.assertEqual(self.remote_sha(), self.original)

    def test_failed_or_unknown_gate_never_retries_or_publishes(self):
        os.environ['CN_DRY_RUN'] = 'false'
        for gate in ['ERROR', 'OK', None]:
            self.calls.clear()
            os.environ['CN_DISABLE_CHECKS'] = 'true'
            with self.subTest(gate=gate), patch.object(workflow, 'sonar_gate_status', return_value=gate), self.assertRaises(subprocess.CalledProcessError):
                workflow.execute('release', self.sonar_failure)
            self.assertEqual([args[0] for args in self.calls], ['build', 'sonar'])
            self.assertEqual(self.remote_sha(), self.original)

    def test_scanner_errors_and_timeouts_never_check_for_retry(self):
        for output in ['Scanner failed', 'Quality Gate check timeout exceeded']:
            self.calls.clear()
            def failed(*args):
                self.gradle(*args)
                raise subprocess.CalledProcessError(1, args, output=output)
            with self.subTest(output=output), patch.object(workflow, 'sonar_gate_status', return_value='NONE') as status, self.assertRaises(subprocess.CalledProcessError):
                workflow.analyze(failed)
            status.assert_not_called()
            self.assertEqual(len(self.calls), 1)

    def test_standalone_pr_analysis_retries_without_building_twice(self):
        os.environ['GITHUB_EVENT_NAME'] = 'pull_request'
        def first_none(*args):
            if args[0] == 'sonar' and not any(call[0] == 'sonar' for call in self.calls):
                self.sonar_failure(*args)
            else:
                self.gradle(*args)
        with patch.object(workflow, 'sonar_gate_status', return_value='NONE'):
            workflow.execute('analyze', first_none)
        self.assertEqual([args[0] for args in self.calls], ['build', 'sonar', 'sonar'])
        self.assertFalse(any(arg.startswith('-Dsonar.branch.name=') for arg in self.calls[-1]))

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

    def test_dirty_checkout_is_checked_after_analysis_before_publication(self):
        os.environ['CN_DRY_RUN'] = 'false'
        Path('.gitignore').write_text('build/\n# changed during setup\n', encoding='utf-8')
        with self.assertRaises(subprocess.CalledProcessError):
            workflow.execute('release', self.gradle)
        self.assertEqual([args[0] for args in self.calls], ['build', 'sonar'])
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
