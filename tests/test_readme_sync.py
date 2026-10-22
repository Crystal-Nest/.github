import contextlib
import importlib.util
import io
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock, patch
from urllib.error import HTTPError


SOURCE = Path(__file__).resolve().parents[1] / '.github/actions/sync-readme/sync_readme.py'
spec = importlib.util.spec_from_file_location('readme_sync', SOURCE)
sync = importlib.util.module_from_spec(spec)
spec.loader.exec_module(sync)


class ReadmeSync(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)
        self.properties = self.root / 'gradle.properties'
        self.properties.write_text('mod_id_kebab = cobweb\ncurseforge_id = 968456\n', encoding='utf-8')
        self.readme = self.root / 'README.md'
        self.body = '# Cobweb\n\n[Dependency](https://modrinth.com/mod/prometheus-api)\n\n🕸️ `code`\n'
        self.readme.write_text(self.body, encoding='utf-8')
        env = {'CN_MODRINTH_PROJECT_ID': '', 'CN_CURSEFORGE_PROJECT_ID': '',
               'CN_DRY_RUN': 'false', 'MODRINTH_TOKEN': 'test-token',
               'CN_README_SHA': 'source-sha', 'GITHUB_STEP_SUMMARY': str(self.root / 'summary.md')}
        environment = patch.dict(os.environ, env)
        environment.start()
        self.addCleanup(environment.stop)
        output = contextlib.redirect_stdout(io.StringIO())
        output.__enter__()
        self.addCleanup(output.__exit__, None, None, None)

    def test_live_update_preserves_readme_and_sends_correct_destination(self):
        request = Mock(side_effect=[{'body': 'old description'}, None])
        before = self.readme.read_bytes()
        self.assertEqual(sync.execute(self.root, request), 'updated')
        self.assertEqual(request.call_args_list[0].args, ('cobweb', 'test-token'))
        self.assertEqual(request.call_args_list[1].args, ('cobweb', 'test-token', self.body))
        self.assertEqual(self.readme.read_bytes(), before)
        self.assertIn('source-sha', (self.root / 'summary.md').read_text(encoding='utf-8'))

    def test_identical_body_skips_patch(self):
        request = Mock(return_value={'body': self.body})
        self.assertEqual(sync.execute(self.root, request), 'unchanged')
        request.assert_called_once_with('cobweb', 'test-token')

    def test_override_precedence_for_both_platforms(self):
        self.properties.write_text('mod_id_kebab: prometheus\nmodrinth_id = prometheus-api\ncurseforge_id = 1365195\n', encoding='utf-8')
        properties = sync.read_properties(self.properties)
        self.assertEqual(sync.project_ids(properties), ('prometheus-api', '1365195'))
        with patch.dict(os.environ, CN_MODRINTH_PROJECT_ID='RQ6INv2n', CN_CURSEFORGE_PROJECT_ID='1173643'):
            self.assertEqual(sync.project_ids(properties), ('RQ6INv2n', '1173643'))
            request = Mock(return_value={'body': self.body})
            sync.execute(self.root, request)
            request.assert_called_once_with('RQ6INv2n', 'test-token')

    def test_curseforge_configuration_is_optional_and_validated(self):
        self.assertEqual(sync.project_ids({'mod_id_kebab': 'cobweb'}), ('cobweb', ''))
        for value in ['not-numeric', '-1', '0']:
            with self.subTest(value=value), patch.dict(os.environ, CN_CURSEFORGE_PROJECT_ID=value), self.assertRaises(ValueError):
                sync.project_ids({'mod_id_kebab': 'cobweb'})

    def test_dry_run_needs_no_token_or_network_and_previews_escaped_content(self):
        os.environ['CN_DRY_RUN'] = 'true'
        os.environ['MODRINTH_TOKEN'] = ''
        self.readme.write_text(self.body + '<video controls>\n', encoding='utf-8')
        request = Mock()
        self.assertEqual(sync.execute(self.root, request), 'dry run')
        request.assert_not_called()
        summary = (self.root / 'summary.md').read_text(encoding='utf-8')
        self.assertIn('&lt;video controls&gt;', summary)
        self.assertIn('No Modrinth API calls were made.', summary)

    def test_fer_video_replacement_is_exact_and_does_not_edit_source(self):
        self.properties.write_text('mod_id_kebab = fancy-entity-renderer\ncurseforge_id = 1173643\n', encoding='utf-8')
        self.readme.write_text('Before\n\n' + sync.FER_ATTACHMENT + '\n\nAfter\n', encoding='utf-8')
        before = self.readme.read_bytes()
        request = Mock(side_effect=[{'body': ''}, None])
        with patch.dict(os.environ, CN_MODRINTH_PROJECT_ID='RQ6INv2n'):
            sync.execute(self.root, request)
        self.assertEqual(request.call_args_list[1].args, ('RQ6INv2n', 'test-token', 'Before\n\n' + sync.FER_VIDEO + '\n\nAfter\n'))
        self.assertEqual(self.readme.read_bytes(), before)

    def test_unexpected_fer_attachment_fails_before_any_request(self):
        self.properties.write_text('mod_id_kebab = fancy-entity-renderer\n', encoding='utf-8')
        for body in ['New attachment URL\n', (sync.FER_ATTACHMENT + '\n') * 2]:
            with self.subTest(body=body):
                self.readme.write_text(body, encoding='utf-8')
                request = Mock()
                with self.assertRaisesRegex(ValueError, 'exactly one'):
                    sync.execute(self.root, request)
                request.assert_not_called()

    def test_other_mods_are_not_subject_to_fer_replacement(self):
        self.assertEqual(sync.prepare_body(sync.FER_ATTACHMENT, {'mod_id_kebab': 'cobweb'}), sync.FER_ATTACHMENT)

    def test_missing_token_and_empty_readme_fail_without_requests(self):
        request = Mock()
        with patch.dict(os.environ, MODRINTH_TOKEN=''), self.assertRaisesRegex(ValueError, 'PROJECT_WRITE'):
            sync.execute(self.root, request)
        self.readme.write_text(' \n', encoding='utf-8')
        with self.assertRaisesRegex(ValueError, 'empty'):
            sync.execute(self.root, request)
        request.assert_not_called()

    def test_invalid_properties_fail_before_any_request(self):
        for properties in ['curseforge_id = 968456\n', 'mod_id_kebab = cobweb\nmod_id_kebab = other\n']:
            with self.subTest(properties=properties):
                self.properties.write_text(properties, encoding='utf-8')
                request = Mock()
                with self.assertRaises(ValueError):
                    sync.execute(self.root, request)
                request.assert_not_called()

    def test_invalid_response_does_not_overwrite_project(self):
        for current in [{}, {'body': None}, []]:
            with self.subTest(current=current):
                request = Mock(return_value=current)
                with self.assertRaisesRegex(ValueError, 'invalid project body'):
                    sync.execute(self.root, request)
                self.assertEqual(request.call_count, 1)

    def test_api_failure_propagates_without_modifying_readme(self):
        request = Mock(side_effect=HTTPError('https://api.modrinth.com', 401, 'Unauthorized', {}, None))
        before = self.readme.read_bytes()
        with self.assertRaises(HTTPError):
            sync.execute(self.root, request)
        self.assertEqual(self.readme.read_bytes(), before)
        self.assertFalse((self.root / 'summary.md').exists())

    def test_api_uses_authorization_and_only_patches_body(self):
        with patch.object(sync, 'urlopen', return_value=io.StringIO('{"body":"old"}')) as network:
            self.assertEqual(sync.api_request('test slug', 'test-token'), {'body': 'old'})
            request = network.call_args.args[0]
            self.assertEqual(request.get_method(), 'GET')
            self.assertEqual(request.full_url, 'https://api.modrinth.com/v2/project/test%20slug')
            self.assertEqual(request.get_header('Authorization'), 'test-token')
        with patch.object(sync, 'urlopen', return_value=io.StringIO('')) as network:
            sync.api_request('cobweb', 'test-token', self.body)
            request = network.call_args.args[0]
            self.assertEqual(request.get_method(), 'PATCH')
            self.assertEqual(json.loads(request.data), {'body': self.body})
            self.assertEqual(network.call_args.kwargs['timeout'], 30)


if __name__ == '__main__':
    unittest.main()
