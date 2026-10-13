import hermetic  # Install the environment and network guard before other tests.
import contextlib
import copy
import io
import json
import os
import socket
import sys
import tempfile
import unittest
from http.client import BadStatusLine, IncompleteRead
from pathlib import Path
from unittest.mock import patch
from urllib.parse import urlsplit

from pco_seat_audit.billing import extract_billing
from pco_seat_audit.cli import API_BASE, InputError, api_get, audit, import_plan, main, format_report
from test_audit import NOW, PLAN, ROOT, USAGE


class ReworkTests(unittest.TestCase):
    def run_cli(self, argv):
        stdout, stderr = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
            code = main(argv)
        return code, stdout.getvalue(), stderr.getvalue()

    def test_live_large_total_or_next_page_never_suggests_savings(self):
        for name in ('check_ins', 'giving', 'registrations'):
            original = next(row for row in PLAN['products'] if row['product'] == name)
            plan = {'products': [dict(original, limit=900, lower_tier={'tier': 'Smaller', 'limit': 200})]}
            rows = [{'attributes': {'created_at': NOW.isoformat(), 'person_id': str(index)}} for index in range(100)]
            for pagination in ({'meta': {'total_count': 24000}}, {'links': {'next': API_BASE + original['api']['path'] + '?offset=100'}}, {}):
                with self.subTest(product=name, pagination=pagination):
                    payload = dict(pagination, data=rows)
                    root = {'data': {'type': 'Organization', 'attributes': {}}}
                    def response(path, *args):
                        return root if path.endswith('/v2/') else payload
                    with patch('pco_seat_audit.cli.api_get', side_effect=response):
                        result = audit(plan, {}, 90, live=True, now=NOW)[0]
                    self.assertEqual(result['finding'], 'NOT ASSESSED')
                    self.assertNotIn('recommendation', result)
                    self.assertNotIn('possible_monthly_saving', result)
                    report = format_report([result], 90, 'Account')
                    self.assertIn('usage may be incomplete', report)
                    self.assertNotIn('Consider', report)
                    self.assertNotIn('possible saving', report)

    def test_old_first_page_is_not_reported_unused(self):
        plan = {'products': [PLAN['products'][1]]}
        payload = {'data': [{'attributes': {'created_at': '2020-01-01'}}], 'meta': {'total_count': 24000}}
        with patch('pco_seat_audit.cli.api_get', return_value=payload):
            result = audit(plan, {}, 90, live=True, now=NOW)[0]
        self.assertEqual(result['finding'], 'NOT ASSESSED')

    def test_complete_page_can_be_assessed_and_unknown_pagination_cannot(self):
        plan = {'products': [PLAN['products'][1]]}
        rows = USAGE['registrations']
        for info, expected in (({'meta': {'total_count': len(rows)}}, 'LOW USAGE'),
                               ({'links': {'next': None}}, 'LOW USAGE'), ({}, 'NOT ASSESSED')):
            with patch('pco_seat_audit.cli.api_get', return_value=dict(info, data=rows)):
                result = audit(plan, {}, 90, live=True, now=NOW)[0]
            self.assertEqual(result['finding'], expected)

    def test_inherited_credentials_cleared_and_network_blocked(self):
        self.assertNotIn('PCO_APP_ID', os.environ)
        self.assertNotIn('PCO_SECRET', os.environ)
        with socket.socket() as connection:
            with self.assertRaisesRegex(AssertionError, 'must not use the network'):
                connection.connect(('NETWORK-PLACEHOLDER', 1))

    def test_missing_usage_is_plain_error_for_both_plan_routes(self):
        with tempfile.TemporaryDirectory() as folder:
            output = Path(folder) / 'report'
            for argv in (['--plan', str(ROOT / 'examples/plan.json')],
                         ['--subscriptions', str(ROOT / 'examples/subscriptions-synthetic.txt')]):
                code, stdout, stderr = self.run_cli(argv + ['--output', str(output)])
                self.assertEqual(code, 2)
                self.assertEqual(len(stderr.splitlines()), 1)
                self.assertIn('supply --usage', stderr)
                self.assertIn('--live', stderr)
                self.assertEqual(stdout, '')
                self.assertFalse(output.exists())

    def test_report_heading_and_both_sources_are_truthful(self):
        with tempfile.TemporaryDirectory() as folder:
            saved = Path(folder) / 'subscriptions.txt'
            saved.write_text('Donation Management\n$23\n180 Monthly Donations')
            output = Path(folder) / 'report'
            args = ['--subscriptions', str(saved), '--as-of', NOW.isoformat(), '--output', str(output)]
            for live in (False, True):
                extra = ['--live'] if live else ['--usage', str(ROOT / 'examples/usage.json')]
                payload = {'data': [], 'meta': {'total_count': 0}}
                with patch('pco_seat_audit.cli.api_get', return_value=payload):
                    code, stdout, stderr = self.run_cli(args + extra)
                self.assertEqual((code, stderr), (0, ''))
                self.assertTrue(stdout.startswith('Planning Center usage audit — Account usage review'))
                self.assertNotIn('Synthetic sample organization', stdout)
                self.assertIn('Plan source: pasted Subscriptions file ' + str(saved), stdout)
                self.assertIn('Usage source: ' + ('live API' if live else 'usage file'), stdout)
                self.assertEqual(stdout, output.read_text())
            code, stdout, stderr = self.run_cli(['--plan', str(ROOT / 'examples/plan.json'),
                '--usage', str(ROOT / 'examples/usage.json'), '--output', str(output)])
            self.assertEqual(code, 0)
            self.assertIn('Synthetic sample organization', stdout)

    def test_full_paste_completes_with_unmeasured_products(self):
        text = (ROOT / 'examples/subscriptions-synthetic.txt').read_text()
        text += '\nMembership Management\n$0\nUnlimited\nEvents & Facility Management\n$8\n1 Room\nGroups Management\n$7\n120 Group Members\n'
        with tempfile.TemporaryDirectory() as folder:
            saved = Path(folder) / 'subscriptions.txt'
            saved.write_text(text)
            code, stdout, stderr = self.run_cli(['--subscriptions', str(saved), '--usage', str(ROOT / 'examples/usage.json'),
                '--account-roots', str(ROOT / 'examples/account-roots-synthetic.json'), '--as-of', NOW.isoformat(),
                '--output', str(Path(folder) / 'report')])
            self.assertEqual((code, stderr), (0, ''))
            for name in ('People', 'Calendar', 'Groups'):
                self.assertIn(name + ': NOT ASSESSED — Billing imported, usage not measured', stdout)
            self.assertIn('Check Ins: LOW USAGE', stdout)

    def assert_bad_input(self, kind, value, expected):
        with tempfile.TemporaryDirectory() as folder:
            bad = Path(folder) / (kind + '.json')
            bad.write_text(json.dumps(value))
            output = Path(folder) / 'report'
            argv = ['--plan', str(ROOT / 'examples/plan.json'), '--usage', str(ROOT / 'examples/usage.json'),
                    '--as-of', NOW.isoformat(), '--output', str(output)]
            option = {'plan': '--plan', 'usage': '--usage', 'roots': '--account-roots'}[kind]
            code, stdout, stderr = self.run_cli(argv + [option, str(bad)])
            self.assertEqual(code, 2)
            self.assertEqual(len(stderr.splitlines()), 1)
            self.assertIn(str(bad), stderr)
            self.assertIn(expected, stderr)
            self.assertNotIn('Traceback', stdout + stderr)
            self.assertFalse(output.exists())

    def test_wrong_shaped_plan_usage_and_roots_name_the_file(self):
        for value in ([], None, 42):
            with self.subTest(kind='usage', value=value):
                self.assert_bad_input('usage', value, 'usage must be an object')
            with self.subTest(kind='plan', value=value):
                self.assert_bad_input('plan', value, 'products list')
            with self.subTest(kind='roots', value=value):
                self.assert_bad_input('roots', value, 'account-roots must be an object')
        for field, value, expected in (('product', [], 'supported product name string'),
                                       ('api', None, 'api must be an object'), ('api', 'bad', 'api must be an object'),
                                       ('lower_tier', {'limit': 200}, 'tier name')):
            plan = copy.deepcopy(PLAN)
            plan['products'][0][field] = value
            with self.subTest(field=field, value=value):
                self.assert_bad_input('plan', plan, expected)
        for relationships in (None, {'person': None}):
            usage = copy.deepcopy(USAGE)
            usage['check_ins'] = [{'attributes': {'created_at': NOW.isoformat()}, 'relationships': relationships}]
            self.assert_bad_input('usage', usage, 'relationship')
        for roots in ({'services': None}, {'check_ins': []}, {'services': {'data': []}}):
            self.assert_bad_input('roots', roots, 'Organization')

    def test_api_failures_are_truthful_and_do_not_write_partial_reports(self):
        failures = [OSError('SYNTHETIC-SECRET'), BadStatusLine('SYNTHETIC-SECRET'),
                    IncompleteRead(b'SYNTHETIC-SECRET', 100), RecursionError('SYNTHETIC-SECRET')]
        with tempfile.TemporaryDirectory() as folder:
            output = Path(folder) / 'report'
            for failure in failures:
                with self.subTest(failure=type(failure).__name__):
                    with patch.dict('os.environ', {'PCO_APP_ID': 'SYNTHETIC-APP', 'PCO_SECRET': 'SYNTHETIC-SECRET'}):
                        with patch('pco_seat_audit.cli.urlopen', side_effect=failure):
                            code, stdout, stderr = self.run_cli(['--live', '--plan', str(ROOT / 'examples/plan.json'), '--output', str(output)])
                    self.assertEqual(code, 2)
                    self.assertEqual(len(stderr.splitlines()), 1)
                    self.assertIn('API request failed', stderr)
                    self.assertNotIn('cannot write', stderr)
                    self.assertNotIn('SYNTHETIC-SECRET', stdout + stderr)
                    self.assertNotIn('SYNTHETIC-APP', stdout + stderr)
                    self.assertFalse(output.exists())
                    self.assertNotIn('Planning Center usage audit', stdout)

    def test_malformed_response_bodies_and_shapes_are_plain_api_errors(self):
        for body in (b'not JSON', b'\xff', b'[' * (sys.getrecursionlimit() + 100) + b']' * (sys.getrecursionlimit() + 100)):
            with patch('pco_seat_audit.cli.urlopen', return_value=io.BytesIO(body)):
                with self.assertRaisesRegex(InputError, 'API request failed'):
                    api_get('/giving/v2/donations', {}, 'giving', ('SYNTHETIC-APP', 'SYNTHETIC-SECRET'))
        for payload in ([], {'data': None}, {'data': [], 'meta': None}, {'data': [], 'meta': {'total_count': 'bad'}}):
            with patch('pco_seat_audit.cli.api_get', return_value=payload):
                with self.assertRaisesRegex(InputError, 'API response'):
                    audit({'products': [PLAN['products'][1]]}, {}, 90, live=True, now=NOW)

    def test_invalid_api_paths_are_rejected_before_transport(self):
        for path in ('/bad path', '/bad\tpath', '/bad\npath', '/badé', '//elsewhere', '/bad/../path', '/bad?query'):
            plan = copy.deepcopy(PLAN)
            plan['products'][0]['api']['path'] = path
            with patch('pco_seat_audit.cli.urlopen') as transport:
                self.assert_bad_input('plan', plan, 'api.path')
                transport.assert_not_called()

    def test_hard_link_output_cannot_overwrite_any_input(self):
        with tempfile.TemporaryDirectory() as folder:
            for option, example in (('--plan', 'plan.json'), ('--usage', 'usage.json'),
                                    ('--subscriptions', 'subscriptions-synthetic.txt'),
                                    ('--account-roots', 'account-roots-synthetic.json'), ('--change-plans', 'change-plans-synthetic.txt')):
                original = Path(folder) / 'input'
                output = Path(folder) / 'output'
                content = (ROOT / 'examples' / example).read_bytes()
                original.write_bytes(content)
                os.link(original, output)
                code, stdout, stderr = self.run_cli(['--plan', str(ROOT / 'examples/plan.json'),
                    '--usage', str(ROOT / 'examples/usage.json'), option, str(original), '--output', str(output)])
                self.assertEqual(code, 2)
                self.assertIn('output path must differ', stderr)
                self.assertEqual(original.read_bytes(), content)
                output.unlink()
                original.unlink()

    def test_distinct_missing_identifier_is_unassessed_not_unused(self):
        usage = copy.deepcopy(USAGE)
        usage['check_ins'] = [{'attributes': {'created_at': NOW.isoformat()}}]
        result = audit(PLAN, usage, 90, now=NOW)[0]
        self.assertEqual(result['finding'], 'NOT ASSESSED')
        self.assertIn('no person identifier', result['incomplete_reason'])
        self.assertNotIn('possible_monthly_saving', result)

    def test_api_capacity_label_matches_limit_and_explains_difference(self):
        roots = {'check_ins': {'data': {'type': 'Organization', 'attributes': {'daily_check_ins': 900}}}}
        result = audit(PLAN, USAGE, 90, now=NOW, account_roots=roots)[0]
        self.assertEqual((result['tier'], result['limit']), ('API capacity 900', 900))
        self.assertIn('API limit 900 is used', result['capacity_difference'])

    def test_unlimited_wording_explains_manual_feature_review(self):
        plan = import_plan(extract_billing('Donation Management\n$23\nUnlimited Monthly Donations', subscriptions=True), {'products': []})
        result = audit(plan, {'giving': [{'attributes': {'created_at': NOW.isoformat()}}]}, 90, now=NOW)[0]
        self.assertIn('Unlimited tier; compare cheaper tiers', result['finding'])
        self.assertNotIn('limit not found', result['finding'])

    def test_parser_navigation_bom_plain_angle_text_and_price_forms(self):
        self.assertEqual(extract_billing('People Calendar Check-Ins Giving\n$23')['products'], [])
        for text in ('\ufeffDonation Management\n$23/mo\n180 Monthly Donations',
                     '<note>\nDonation Management\n$23/mo\n180 Monthly Donations'):
            entry = extract_billing(text, subscriptions=True)['products'][0]
            self.assertEqual((entry['product'], entry['paid'], entry['monthly_price'], entry['limit']), ('giving', True, 23, 180))
        entry = extract_billing('Events & Facility Management\n$8/mo\n1 Room', subscriptions=True)['products'][0]
        self.assertEqual(entry['limit'], 1)
        for text in ('Giving\nUnknown Product\n$99\nFree', 'Giving\nTotal\n$99'):
            entry = extract_billing(text, subscriptions=True)['products'][0]
            self.assertNotIn('paid', entry)
            self.assertNotIn('monthly_price', entry)

    def test_long_price_line_keeps_conflicts_without_repeated_line_scans(self):
        text = 'Giving\n' + ' '.join('$23/mo $24/mo' for _ in range(2000))
        imported = extract_billing(text, subscriptions=True)
        self.assertEqual(len(imported['review']['giving']['price_shown']['candidates']), 4000)
        self.assertNotIn('monthly_price', imported['products'][0])

    def test_bom_json_inputs_are_supported(self):
        with tempfile.TemporaryDirectory() as folder:
            plan = Path(folder) / 'plan.json'
            plan.write_text('\ufeff' + json.dumps(PLAN))
            code, stdout, stderr = self.run_cli(['--plan', str(plan), '--usage', str(ROOT / 'examples/usage.json'),
                '--output', str(Path(folder) / 'report')])
            self.assertEqual((code, stderr), (0, ''))

    def test_live_transport_keeps_get_no_body_and_constant_host(self):
        with patch('pco_seat_audit.cli.urlopen', return_value=io.BytesIO(b'{}')) as transport:
            api_get('/giving/v2/donations', {'per_page': '100'}, 'giving', ('SYNTHETIC-APP', 'SYNTHETIC-SECRET'))
        request = transport.call_args.args[0]
        self.assertEqual(request.get_method(), 'GET')
        self.assertIsNone(request.data)
        self.assertEqual(urlsplit(request.full_url).netloc, urlsplit(API_BASE).netloc)
        self.assertNotIn('SYNTHETIC', request.full_url)
