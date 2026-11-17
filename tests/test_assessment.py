import contextlib
import io
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'lib'))
import sapper
import assessment

CONTEXT = dict(objective='Review synthetic tenant isolation', scope='local fixture API and org.example.app',
    exclusions='All external targets', environment='fixture build 1; no device', window='local test run',
    identities='owner A, reader B, tenants X/Y', access='synthetic sessions only',
    methods='local deterministic observations', constraints='no network targets; redact credentials')


class AssessmentTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.store = sapper.Store(self.root / '.sapper')
        self.store.init('test', 'Bounded assessment')

    def tearDown(self):
        self.tmp.cleanup()

    def pentest(self):
        self.store.configure_profile('pentest', CONTEXT)
        return self.store.load_project()['engagement']

    def test_profile_selection_is_explicit_repeatable_and_preserves_history(self):
        self.assertNotIn('profile', self.store.load_project())
        context = self.pentest()
        before = self.store.project_file.read_bytes()
        self.pentest()
        self.assertEqual(before, self.store.project_file.read_bytes())
        self.store.configure_profile(None)
        self.assertEqual(self.store.load_project()['profile'], 'pentest')
        self.store.configure_profile('ctf')
        self.assertEqual(self.store.get_fact(context)['data'], CONTEXT)
        self.assertNotIn('engagement', self.store.load_project())
        self.assertEqual(sapper.validate_graph(self.store), [])

    def test_context_validation_and_immutability(self):
        with self.assertRaises(sapper.FGError):
            self.store.configure_profile('pentest', {'scope': 'missing'})
        self.assertEqual(len(self.store.list_facts()), 1)
        context = self.pentest()
        with self.assertRaisesRegex(sapper.FGError, 'immutable'):
            self.store.update_doc(context, 'changed')
        self.store.configure_profile('pentest', dict(CONTEXT, environment='build 2'))
        self.assertNotEqual(self.store.load_project()['engagement'], context)

    def test_setup_preflights_context_settings_and_ownership(self):
        project = self.root / 'new'
        project.mkdir()
        context = project / 'context.json'
        context.write_text(json.dumps(CONTEXT))
        with mock.patch('os.getcwd', return_value=str(project)), contextlib.redirect_stdout(io.StringIO()):
            args = ['--store', str(project / '.sapper'), 'setup', '--backend', 'pi', '--goal', 'check', '--profile', 'pentest']
            self.assertEqual(sapper.main(args), 1)
            self.assertFalse((project / '.sapper').exists())
            (project / '.pi').mkdir()
            owner = project / '.pi/sapper-integration.json'
            owner.write_text('{}')
            self.assertEqual(sapper.main(args + ['--engagement', str(context)]), 1)
            self.assertFalse((project / '.sapper').exists())
            owner.unlink()
            self.assertEqual(sapper.main(args + ['--engagement', str(context)]), 0)
            self.assertEqual(sapper.main(args), 0)
            selected = sapper.Store(project / '.sapper').load_project()
            self.assertEqual(sapper.main(['--store', str(project / '.sapper'), 'teardown', '--backend', 'pi']), 0)
            self.assertEqual(sapper.Store(project / '.sapper').load_project(), selected)

    def result_data(self, outcome):
        data = dict(outcome=outcome, asset='fixture /objects/1',
            conditions='build 1; local test; authenticated reader B tenant Y',
            limitations='Only object 1 and these two identities')
        if outcome in assessment.TESTED_OUTCOMES:
            data.update(method='Replay request with controlled identities', expected='No cross-tenant object',
                observed='No object returned; authorized owner read succeeds',
                evidence='redacted transcript in doc', basis='dynamic')
        data.update({key: 'synthetic ' + key for key in assessment.OUTCOME_FIELDS[outcome]})
        if outcome == 'vulnerability':
            data.update(observed='Reader B tenant Y receives owner A tenant X object contents',
                comparison='Owner read matches the cross-tenant response; unrelated object request is denied',
                impact='Cross-tenant disclosure of the synthetic object',
                root_cause='Fixture object lookup omits tenant predicate for object 1',
                remediation='Enforce tenant and object permission before retrieval')
        return data

    def check(self, dependencies=None):
        context = self.pentest()
        return self.store.add_intent(dependencies or [context], 'Check tenant boundary', 'test')

    def test_all_outcomes_are_typed_immutable_and_linked(self):
        for outcome in assessment.OUTCOME_FIELDS:
            intent = self.check()
            fact, closed = self.store.conclude_with_new_fact(intent['id'], outcome, 'test',
                kind='assessment', data=self.result_data(outcome), doc='Synthetic transcript: ' + self.result_data(outcome).get('observed', 'No test performed'))
            self.assertEqual(closed['to'], fact['id'])
            with self.assertRaisesRegex(sapper.FGError, 'immutable'):
                self.store.update_doc(fact['id'], 'erase evidence')
        self.assertEqual(sapper.validate_graph(self.store), [])
        report = sapper.assessment_report(self.store)
        self.assertEqual(report['outcome_counts'], {outcome: 1 for outcome in assessment.OUTCOME_FIELDS})
        self.assertIn('tenant predicate', report['checks'][0]['result']['data']['root_cause'])

    def test_invalid_claims_fail_all_writes_without_allocating_ids(self):
        intent = self.check()
        before = self.store.project_file.read_bytes()
        for outcome in ('secure', 'passed', 'vulnerability', 'validated_defense'):
            data = dict(outcome=outcome, asset='fixture', conditions='local', limitations='bounded')
            for write in (
                lambda: self.store.add_fact('invalid', 'test', intent['id'], kind='assessment', data=data),
                lambda: self.store.conclude_with_new_fact(intent['id'], 'invalid', 'test', kind='assessment', data=data),
            ):
                with self.assertRaises(sapper.FGError):
                    write()
        with self.assertRaises(sapper.FGError):
            self.store.conclude_with_new_fact(intent['id'], 'failed exploit', 'test', kind='note')
        self.assertEqual(self.store.project_file.read_bytes(), before)
        self.assertIsNone(self.store.get_intent(intent['id'])['to'])
        note = self.store.add_fact('legacy note', 'test')
        for target in (note['id'], 'goal', intent['from'][0]):
            with self.assertRaises(sapper.FGError):
                self.store.conclude_intent(intent['id'], target, 'test')
        with self.assertRaises(sapper.FGError):
            self.store.add_fact('orphan', 'test', kind='assessment', data=self.result_data('blocked'))

    def test_context_pinning_and_retest_preserve_prior_evidence(self):
        intent = self.check()
        old_context = intent['from'][0]
        first, _ = self.store.conclude_with_new_fact(intent['id'], 'bounded negative', 'test',
            kind='assessment', data=self.result_data('not_observed'), doc='original snapshot')
        original = self.store.get_fact(first['id'])
        self.store.configure_profile('pentest', dict(CONTEXT, environment='build 2'))
        new_context = self.store.load_project()['engagement']
        retest = self.store.add_intent([first['id']], 'Retest same boundary', 'test')
        self.assertEqual(retest['from'], [first['id'], new_context])
        self.store.conclude_with_new_fact(retest['id'], 'blocked retest', 'test',
            kind='assessment', data=self.result_data('blocked'))
        self.assertEqual(self.store.get_fact(first['id']), original)
        self.assertEqual(self.store.get_intent(intent['id'])['from'], [old_context])
        with self.assertRaises(sapper.FGError):
            self.store.add_intent([old_context, new_context], 'ambiguous', 'test')
        with self.assertRaises(sapper.FGError):
            self.store.update_doc(intent['id'], 'rewrite hypothesis')
        self.assertEqual(sapper.validate_graph(self.store), [])

    def test_corrupt_evidence_and_context_are_audited(self):
        intent = self.check()
        fact, _ = self.store.conclude_with_new_fact(intent['id'], 'defense', 'test',
            kind='assessment', data=self.result_data('validated_defense'), doc='snapshot')
        fact['data']['basis'] = 'static'
        sapper.atomic_write_json(self.store.facts_dir / (fact['id'] + '.json'), fact)
        self.assertTrue(any('dynamic' in error for error in sapper.validate_graph(self.store)))

    def test_all_dispatch_prompts_and_raw_auto_use_pentest_contract(self):
        intent = self.check()
        for role in ('reason', intent['id'], 'blindspot', 'brainstorm'):
            out = io.StringIO()
            with contextlib.redirect_stdout(out):
                self.assertEqual(sapper.main(['--store', str(self.store.root), 'dispatch', role, '--dry-run']), 0)
            prompt = out.getvalue()
            self.assertIn('Sapper pentest mode', prompt)
            self.assertIn(CONTEXT['access'], prompt)
            for forbidden in ('Capture flags immediately', 'harvest credentials/loot', 'flag submitted', 'arena challenges'):
                self.assertNotIn(forbidden, prompt)
        payload = dict(accepted=True, data=dict(title='Bounded result', description='No violation observed',
            kind='assessment', data=self.result_data('not_observed'), doc='FULL_EVIDENCE_SENTINEL'))
        with mock.patch.object(sapper, 'run_agent', return_value={'result': json.dumps(payload)}) as run:
            sapper._run_explore_raw(self.store, ROOT / 'templates', intent, 'test', 10, False, self.root)
            self.assertIn('Sapper pentest mode', run.call_args.kwargs['append_system'])
        intent = self.store.get_intent(intent['id'])
        with mock.patch.object(sapper, 'run_agent', return_value={'result': '{"verified":true,"issues":""}'}) as run:
            self.assertEqual(sapper._run_verify_raw(self.store, ROOT / 'templates', intent, 'test', 10, False, self.root), (True, ''))
            self.assertIn('FULL_EVIDENCE_SENTINEL', run.call_args.args[0])
            self.assertIn(CONTEXT['identities'], run.call_args.args[0])
        with mock.patch.object(sapper, 'run_agent', return_value={'result': '{"accepted":true,"data":{}}'}) as run:
            sapper._run_reason_raw(self.store, ROOT / 'templates', 'test', 10, False, self.root)
            self.assertIn('Sapper pentest mode', run.call_args.args[0])
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            self.assertEqual(sapper.main(['--store', str(self.store.root), 'dispatch', 'verify', '--intent', intent['id'], '--dry-run']), 0)
        self.assertIn('FULL_EVIDENCE_SENTINEL', out.getvalue())

    def test_custom_verifier_keeps_complete_pinned_evidence(self):
        context = self.pentest()
        intent = self.store.add_intent([context], 'Check tenant boundary', 'test', doc='CHECK_DOCUMENT_SENTINEL')
        self.store.conclude_with_new_fact(intent['id'], 'Bounded defense', 'test',
            kind='assessment', data=self.result_data('validated_defense'), doc='CUSTOM_VERIFIER_SNAPSHOT')
        intent = self.store.get_intent(intent['id'])
        self.store.configure_profile('pentest', dict(CONTEXT, environment='New active build, not the tested build'))
        custom = self.root / 'templates/pentest'
        custom.mkdir(parents=True)
        summary = 'Review {intent_description} against {result_description}'
        for template in (summary, summary + '\n{assessment_evidence}'):
            (custom / 'verify.md').write_text(template)
            with self.subTest(template=template):
                output = io.StringIO()
                with contextlib.redirect_stdout(output):
                    self.assertEqual(sapper.main(['--store', str(self.store.root), 'dispatch', 'verify',
                        '--intent', intent['id'], '--templates', str(custom.parent), '--dry-run']), 0)
                with mock.patch.object(sapper, 'run_agent', return_value={'result': '{"verified":true,"issues":""}'}) as run:
                    self.assertEqual(sapper._run_verify_raw(self.store, custom.parent, intent,
                        'test', 10, False, self.root), (True, ''))
                for prompt in (output.getvalue(), run.call_args.args[0]):
                    for evidence in ('CHECK_DOCUMENT_SENTINEL', 'CUSTOM_VERIFIER_SNAPSHOT',
                                     CONTEXT['environment'], self.result_data('validated_defense')['comparison']):
                        self.assertEqual(prompt.count(evidence), 1, evidence)
        self.store.configure_profile('ctf')
        result = self.store.get_fact(intent['to'])
        self.assertEqual(sapper.render_verify_prompt(self.store, summary, intent, result),
            'Review Check tenant boundary against Bounded defense')

    def test_default_ctf_templates_and_override_keep_mode_instructions(self):
        for role in ('reason', 'explore', 'verify', 'blindspot', 'brainstorm'):
            self.assertEqual(sapper._load_template(ROOT / 'templates', role + '.md', self.store),
                (ROOT / 'templates' / (role + '.md')).read_text())
        self.pentest()
        custom = self.root / 'templates/pentest'
        custom.mkdir(parents=True)
        (custom / 'reason.md').write_text('CUSTOM_TEMPLATE')
        prompt = sapper._load_template(custom.parent, 'reason.md', self.store)
        self.assertIn('CUSTOM_TEMPLATE', prompt)
        self.assertIn('Sapper pentest mode', prompt)

    def test_model_malformed_result_cannot_downgrade_to_a_note(self):
        intent = self.check()
        for payload in ({'description': 'bad', 'kind': 42, 'data': []},
                        {'description': 'bad', 'kind': 'assessment', 'data': {'outcome': 'secure'}}):
            with mock.patch.object(sapper, 'run_agent', return_value={'result': json.dumps(payload)}):
                with self.assertRaises(sapper.FGError):
                    sapper._run_explore_raw(self.store, ROOT / 'templates', intent, 'test', 10, False, self.root)
        self.assertIsNone(self.store.get_intent(intent['id'])['to'])

    def test_zero_vulnerability_cli_report_keeps_negatives_gaps_and_history(self):
        seed = self.store.add_fact('legacy seed', 'test')
        self.store.add_intent([seed['id']], 'legacy CTF work', 'test')
        original = None
        for outcome in ('validated_defense', 'not_observed', 'inconclusive', 'blocked', 'not_tested', 'not_applicable'):
            intent = self.check([original['id']] if original else None)
            result, _ = self.store.conclude_with_new_fact(intent['id'], outcome, 'test',
                kind='assessment', data=self.result_data(outcome), doc='bounded snapshot')
            if original is None:
                original = result
        pending = self.check()
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            self.assertEqual(sapper.main(['--store', str(self.store.root), 'report', '--format', 'json']), 0)
        report = json.loads(out.getvalue())
        self.assertEqual(report['outcome_counts']['vulnerability'], 0)
        self.assertEqual(report['outcome_counts']['not_tested'], 2)
        self.assertEqual(report['outcome_counts']['not_applicable'], 1)
        self.assertEqual(report['excluded_legacy_intents'], 1)
        self.assertEqual(report['checks'][-1]['check']['id'], pending['id'])
        self.assertIsNone(report['checks'][-1]['result'])
        self.assertIn(original['id'], report['checks'][1]['prior_assessments'])
        self.assertIn('not unique risks', report['limitations'])
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            self.assertEqual(sapper.main(['--store', str(self.store.root), 'report']), 0)
        self.assertIn('vulnerability: 0', out.getvalue())
        self.assertIn('bounded snapshot', out.getvalue())
        self.assertIn('No result evidence', out.getvalue())

    def test_report_keeps_blindspot_and_failed_verification_advisories(self):
        intent = self.check()
        blindspot = dict(accepted=True, data=dict(blindspots=[dict(area='mobile backend', risk='high',
            question='MISSING_BACKEND_EVIDENCE', suggestion='Obtain the authorized mobile session')]))
        with mock.patch.object(sapper, 'run_agent', return_value={'result': json.dumps(blindspot)}), \
                contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(sapper.main(['--store', str(self.store.root), 'dispatch', 'blindspot', '--backend', 'pi']), 0)
        responses = [dict(accepted=True, data={}), dict(accepted=True, data=dict(
            description='Bounded defense', kind='assessment', data=self.result_data('validated_defense'), doc='Snapshot')),
            dict(verified=False, issues='BASELINE_IDENTITY_UNCONFIRMED')]
        with mock.patch.object(sapper, 'run_agent', side_effect=[{'result': json.dumps(r)} for r in responses]), \
                contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(sapper.main(['--store', str(self.store.root), 'auto', '--backend', 'pi',
                '--max-steps', '1', '--max-explore', '1', '--verify']), 0)
        hints = self.store.list_hints()
        self.assertEqual({hint['creator'] for hint in hints}, {'blindspot', 'verify'})
        self.assertIn(intent['id'], hints[1]['content'])
        for format in ('json', 'text'):
            with self.subTest(format=format):
                output = io.StringIO()
                with contextlib.redirect_stdout(output):
                    self.assertEqual(sapper.main(['--store', str(self.store.root), 'report', '--format', format]), 0)
                text = output.getvalue()
                if format == 'json':
                    report = json.loads(text)
                    self.assertEqual(report.get('advisories'), hints)
                    self.assertEqual(report['outcome_counts']['validated_defense'], 1)
                    self.assertEqual(sum(report['outcome_counts'].values()), 1)
                else:
                    self.assertIn('not tested coverage', text)
                    for hint in hints:
                        for field in ('id', 'creator', 'created_at', 'content'):
                            self.assertIn(hint[field], text)
                self.assertIn('MISSING_BACKEND_EVIDENCE', text)
                self.assertIn('BASELINE_IDENTITY_UNCONFIRMED', text)

    def test_report_rejects_corrupt_records_rather_than_hiding_coverage(self):
        self.check()
        (self.store.facts_dir / 'f999.json').write_text('{bad')
        with self.assertRaisesRegex(sapper.FGError, 'invalid graph'):
            sapper.assessment_report(self.store)

    def test_context_keeps_doctrine_separate_and_scope_outside_frontier_budget(self):
        self.store.configure_profile('pentest', dict(CONTEXT, scope='scope ' * 2000 + 'SCOPE_TAIL'))
        self.store.add_fact('observation ' * 2000, 'test')
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            sapper.main(['--store', str(self.store.root), 'context'])
        self.assertIn('SCOPE_TAIL', out.getvalue())
        self.assertIn('UNTRUSTED DATA', out.getvalue())
        instructions = io.StringIO()
        with contextlib.redirect_stdout(instructions):
            sapper.main(['--store', str(self.store.root), 'context', '--instructions'])
        self.assertIn('Sapper pentest mode', instructions.getvalue())
        self.assertNotIn('SCOPE_TAIL', instructions.getvalue())
        self.assertNotIn('observation observation', instructions.getvalue())

    def test_installed_claude_setup_skill_selects_assessment_without_arena(self):
        checkout = self.root / 'checkout'
        checkout.mkdir()
        shutil.copy2(ROOT / 'install.sh', checkout / 'install.sh')
        for directory in ('lib', 'skill', 'templates', 'docs'):
            shutil.copytree(ROOT / directory, checkout / directory)
        home = self.root / 'home'
        env = dict(os.environ, HOME=str(home))
        subprocess.run(['bash', str(checkout / 'install.sh')], env=env,
            check=True, capture_output=True, text=True)
        installed = home / '.claude/skills/sapper-setup'
        self.assertEqual(installed.resolve(), (checkout / 'skill').resolve())
        text = (installed / 'SKILL.md').read_text()
        self.assertTrue((installed.resolve() / '../docs/pentest.md').is_file())
        self.assertIn('Setup without `--profile` preserves the current selection', text)
        self.assertIn('sapper context --instructions', text)
        self.assertIn('sapper report', text)
        self.assertIn('sapper setup --profile pentest --engagement engagement.json', text)
        self.assertIn('sapper setup --profile ctf', text)
        self.assertIn('Only for explicitly selected CTF competitions', text)
        self.assertNotIn('For penetration\nengagements, pair it with', text)
        for outcome in assessment.OUTCOME_FIELDS:
            self.assertIn(outcome, text)
        project = self.root / 'installed-project'
        project.mkdir()
        (project / 'engagement.json').write_text(json.dumps(CONTEXT))
        cli = str(home / '.local/bin/sapper')
        for args in (['setup', '--profile', 'pentest', '--engagement', 'engagement.json',
                      '--goal', 'Review authorized scope', '--agents'], ['setup']):
            result = subprocess.run([cli, *args], cwd=project, env=env,
                capture_output=True, text=True)
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        result = subprocess.run([cli, 'context', '--instructions'], cwd=project, env=env,
            check=True, capture_output=True, text=True)
        self.assertIn('Sapper pentest mode', result.stdout)
        self.assertEqual(sapper.Store(project / '.sapper').load_project()['profile'], 'pentest')

    def test_claude_session_hook_uses_pentest_doctrine(self):
        import importlib.util
        spec = importlib.util.spec_from_file_location('assessment_hook', ROOT / 'lib/sapper-hook.py')
        hook = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(hook)
        self.pentest()
        out = io.StringIO()
        with mock.patch.object(hook, '_event_cwd', return_value=self.root), contextlib.redirect_stdout(out):
            hook.sessionstart()
        text = json.loads(out.getvalue())['hookSpecificOutput']['additionalContext']
        self.assertIn('Sapper pentest mode', text)
        self.assertNotIn('capture flags immediately', text)

    def test_pentest_auto_executes_ready_check_after_commander_noop(self):
        intent = self.check()
        args = sapper.build_parser().parse_args(['--store', str(self.store.root), 'auto',
            '--backend', 'pi', '--max-steps', '1', '--max-explore', '1'])
        args.templates = str(ROOT / 'templates')
        with mock.patch.object(sapper, '_run_reason_raw', return_value=('noop', 'covered')), \
                mock.patch.object(sapper, '_run_explore_raw') as explore, contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(sapper.cmd_auto(args), 0)
            self.assertEqual(explore.call_args.args[2]['id'], intent['id'])
            self.store.configure_profile('ctf')
            explore.reset_mock()
            self.assertEqual(sapper.cmd_auto(args), 0)
            explore.assert_not_called()

    def test_metadata_and_intent_write_boundaries_reject_invalid_profile_and_repin(self):
        intent = self.check()
        old = self.store.project_file.read_bytes()
        project = self.store.load_project()
        for invalid in (dict(project, profile='unknown'), dict(project, engagement='goal'),
                        dict(project, profile='ctf')):
            with self.assertRaises(sapper.FGError):
                self.store.save_project(invalid)
        self.assertEqual(self.store.project_file.read_bytes(), old)
        self.store.configure_profile('pentest', dict(CONTEXT, environment='build 2'))
        intent['from'] = [self.store.load_project()['engagement']]
        with self.assertRaisesRegex(sapper.FGError, 'immutable'):
            self.store.save_intent(intent)
        with self.assertRaises(sapper.FGError):
            self.store.complete_project([*intent['from'], self.store.get_intent(intent['id'])['from'][0]],
                'ambiguous closure', 'test')
        self.assertEqual(sapper.validate_graph(self.store), [])

    def test_brainstorm_unselected_options_are_not_tested(self):
        self.pentest()
        context = self.store.load_project()['engagement']
        sapper._apply_brainstorm(self.store, dict(options=[
            dict(title='first', description='first hypothesis', **{'from': [context]}),
            dict(title='second', description='second hypothesis', **{'from': [context]})]))
        selected = self.store.list_intents()[0]
        with contextlib.redirect_stdout(io.StringIO()):
            sapper.cmd_brainstorm_pick(type('Args', (), dict(store=str(self.store.root), id=selected['id']))())
        report = sapper.assessment_report(self.store)
        self.assertEqual(report['outcome_counts']['not_tested'], 2)
        self.assertTrue(any(row['result'] and row['result']['data']['reason'].startswith('Option abandoned')
            for row in report['checks']))

    def test_cli_fact_from_intent_and_invalid_evidence_contract(self):
        intent = self.check()
        data = self.result_data('not_observed')
        argv = ['--store', str(self.store.root), 'fact', 'Bounded negative', '--from-intent', intent['id'],
            '-k', 'assessment', '--doc', 'Redacted synthetic response comparison']
        for key, value in data.items():
            argv.extend(['-d', key + '=' + value])
        with contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(sapper.main(argv), 0)
        result = self.store.get_fact(self.store.get_intent(intent['id'])['to'])
        self.assertEqual(result['data']['outcome'], 'not_observed')
        for update in ({'basis': 'scanner'}, {'severity': 'urgent'}, {'confidence': 'confirmed'},
                       {'unexpected': 'field'}, {'evidence': ''}, {'conditions': False}):
            check = self.check()
            with self.assertRaises(sapper.FGError):
                self.store.conclude_with_new_fact(check['id'], 'invalid', 'test',
                    kind='assessment', data=dict(data, **update), doc='snapshot')
        self.assertEqual(sapper.validate_graph(self.store), [])


if __name__ == '__main__':
    unittest.main()
