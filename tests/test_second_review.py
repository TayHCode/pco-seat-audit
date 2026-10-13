"""Synthetic reproductions, numbered to match the second review's eight findings."""
import hermetic
import contextlib
import copy
import io
import json
import re
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch
from urllib.request import Request

from pco_seat_audit.billing import extract_billing
from pco_seat_audit.cli import API_BASE, InputError, audit, format_report, import_plan, main, urlopen
from test_audit import NOW, PLAN, ROOT, USAGE
from test_real_page_rework import activity, collection, imported_plan


class SecondReviewTests(unittest.TestCase):
    def run_cli(self, args):
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = main(args)
        return code, out.getvalue(), err.getvalue()

    def test_1_real_sized_collections_are_assessed_beyond_first_100(self):
        for name in ('check_ins', 'registrations', 'giving', 'publishing'):
            with self.subTest(product=name):
                spec = next(row for row in imported_plan()['products'] if row['product'] == name)
                rows = [activity(str(i)) for i in range(101)]
                if name == 'publishing':
                    for row in rows:
                        row['attributes']['published_to_library_at'] = NOW.isoformat()
                from datetime import timedelta
                params = {'per_page': '100', 'offset': '100'}
                if name == 'check_ins':
                    params.update({'where[created_at][gte]': (NOW-timedelta(days=90)).isoformat(),
                                   'where[created_at][lte]': NOW.isoformat()})
                pages = [collection(name, rows[:100], 101, params), collection(name, rows[100:], 101)]
                if name == 'giving':
                    pages = [collection(name, rows[:1], 101)]
                def response(path, *args):
                    if path.endswith('/v2/'):
                        return {'data': {'type': 'Organization', 'attributes': {}}}
                    return pages.pop(0)
                with patch('pco_seat_audit.cli.api_get', side_effect=response):
                    result = audit({'products': [spec]}, {}, 90, now=NOW, live=True)[0]
                self.assertEqual(result['used'], 101)
                self.assertNotEqual(result['finding'], 'NOT ASSESSED')
                self.assertEqual(pages, [])

    def test_2_interstitial_lines_preserve_price_and_tier(self):
        for text in ('Giving\nActive\n$32\n200 Monthly Donations',
                     'Giving\n$32\nChange plan\n200 Monthly Donations',
                     'Giving\nManage donations for your church\n$32\n200 Monthly Donations',
                     'Giving\n$32\nBilled monthly\n200 Monthly Donations'):
            for source in (text, '\n'.join('<p>'+line+'</p>' for line in text.splitlines())):
                with self.subTest(text=source):
                    entry = extract_billing(source, subscriptions=True)['products'][0]
                    self.assertEqual((entry.get('paid'), entry.get('monthly_price'), entry.get('limit')),
                                     (True, 32, 200))
        for boundary in ('Unknown Product', 'Total', 'Invoice', 'Music Stand'):
            entry = extract_billing('Giving\n' + boundary + '\n$99\nFree', True)['products'][0]
            self.assertNotIn('monthly_price', entry)

    def test_3_incomplete_counts_do_not_appear_as_used_over_limit(self):
        spec = next(row for row in PLAN['products'] if row['product'] == 'registrations')
        for date in (NOW.isoformat(), '2000-01-01'):
            payload = {'data': [{'attributes': {'created_at': date}} for _ in range(100)],
                       'meta': {'total_count': 900}}
            with patch('pco_seat_audit.cli.api_get', return_value=payload):
                result = audit({'products': [spec]}, {}, 90, now=NOW, live=True)[0]
            report = format_report([result], 90, 'Account')
            self.assertIn('NOT ASSESSED', report)
            self.assertNotIn(' / ', report)
            self.assertIn('usage count incomplete', report)
            self.assertNotIn('possible saving', report)

    def test_4_real_usage_requires_explicit_plan_before_transport(self):
        with tempfile.TemporaryDirectory() as folder:
            destination = Path(folder) / 'report'
            for args in (['--live'], ['--usage', str(ROOT / 'examples/usage.json')]):
                with patch('pco_seat_audit.cli.urlopen') as transport:
                    code, out, err = self.run_cli(args + ['--output', str(destination)])
                self.assertEqual(code, 2)
                self.assertIn('supply --plan', err)
                self.assertIn('--subscriptions', err)
                self.assertEqual(len(err.splitlines()), 1)
                self.assertFalse(destination.exists())
                transport.assert_not_called()

    def test_5_bad_ladders_and_extreme_dates_give_named_errors(self):
        with tempfile.TemporaryDirectory() as folder:
            plan_file, usage_file, output = [Path(folder)/name for name in ('plan.json', 'usage.json', 'report')]
            spec = copy.deepcopy(PLAN['products'][1])
            spec.pop('lower_tier', None)
            for ladder in ('bad', [1], [{}], [{'tier': 'Small', 'limit': 5}],
                           [{'tier': 'Small', 'monthly_price': 0}],
                           [{'tier': 'Small', 'limit': -1, 'monthly_price': 0}],
                           [{'tier': 'Small', 'limit': 5, 'monthly_price': 'bad'}]):
                with self.subTest(ladder=ladder):
                    plan_file.write_text(json.dumps({'products': [dict(spec, price_ladder=ladder)]}))
                    code, out, err = self.run_cli(['--plan', str(plan_file), '--usage', str(ROOT/'examples/usage.json'), '--output', str(output)])
                    self.assertEqual(code, 2)
                    self.assertIn(str(plan_file), err)
                    self.assertIn('price_ladder', err)
                    self.assertEqual(len(err.splitlines()), 1)
                    self.assertNotIn('Traceback', out+err)
                    self.assertFalse(output.exists())
            plan_file.write_text(json.dumps({'products': [spec]}))
            for stamp in ('0001-01-01', '9999-12-31T23:59:59-23:00', '0001-01-01T00:00:00+23:00'):
                code, out, err = self.run_cli(['--plan', str(plan_file), '--usage', str(ROOT/'examples/usage.json'), '--as-of', stamp, '--output', str(output)])
                self.assertEqual(code, 2)
                self.assertNotIn('Traceback', out+err)
                self.assertEqual(len(err.splitlines()), 1)
            for stamp in ('9999-12-31T23:59:59-23:00', '0001-01-01T00:00:00+23:00'):
                usage_file.write_text(json.dumps({'registrations': [{'attributes': {'created_at': stamp}}]}))
                code, out, err = self.run_cli(['--plan', str(plan_file), '--usage', str(usage_file), '--output', str(output)])
                self.assertEqual(code, 2)
                self.assertIn(str(usage_file), err)
                self.assertEqual(len(err.splitlines()), 1)

    def test_6_bare_free_measured_products_do_not_block_other_products(self):
        for heading, name in (('Event Signups', 'registrations'), ('Check-In', 'check_ins'),
                              ('Service Planning & Scheduling', 'services'), ('Web & App Content Management', 'publishing')):
            for free in ('$0', '$0\nFree', 'Free'):
                with self.subTest(product=name, text=free):
                    plan = import_plan(extract_billing('Giving\n$32\n200 Monthly Donations\n' + heading + '\n' + free, True), {'products': []})
                    results = audit(plan, {'giving': []}, 90, now=NOW)
                    result = next(row for row in results if row['product'] == name)
                    self.assertEqual(result['finding'], 'NOT PAID')
                    self.assertNotIn('recommendation', result)
                    self.assertIn('Giving: PAID BUT UNUSED', format_report(results, 90, 'Account'))

    def test_7_errors_name_the_input_or_remedy_that_needs_attention(self):
        with tempfile.TemporaryDirectory() as folder:
            saved, base, output = [Path(folder)/name for name in ('subscriptions.txt', 'plan.json', 'report')]
            saved.write_text((ROOT/'examples/subscriptions-synthetic.txt').read_text())
            code, out, err = self.run_cli(['--subscriptions', str(saved), '--usage', str(ROOT/'examples/usage.json'), '--output', str(output)])
            self.assertEqual(code, 2)
            self.assertIn('--account-roots', err)
            self.assertNotIn(str(ROOT/'examples/usage.json'), err)
            base.write_text('{"products": []}')
            saved.write_text('Giving\n$32')
            code, out, err = self.run_cli(['--plan', str(base), '--subscriptions', str(saved), '--usage', str(ROOT/'examples/usage.json'), '--output', str(output)])
            self.assertEqual(code, 2)
            self.assertIn(str(saved), err)
            self.assertNotIn(str(base), err)
            saved.write_text('Services\n$32\n50 Team Members')
            root = {'data': {'type': 'Organization', 'attributes': {'people_allowed': None, 'people_remaining': None}}}
            with patch('pco_seat_audit.cli.api_get', return_value=root):
                code, out, err = self.run_cli(['--subscriptions', str(saved), '--live', '--output', str(output)])
            self.assertEqual(code, 2)
            self.assertIn('did not supply', err)
            self.assertIn('people_allowed', err)
            self.assertNotIn('need --live', err)

    def test_8_named_unlimited_periods_and_sources_have_plain_wording(self):
        for text, usage in (('Publishing\n$15\nPlus', {'publishing': [{'attributes': {'published_to_library_at': NOW.isoformat()}}]}),
                            ('Giving\n$32\nUnlimited Monthly Donations', {'giving': []})):
            results = audit(import_plan(extract_billing(text, True), {'products': []}), usage, 90, now=NOW)
            report = format_report(results, 90, 'Account')
            self.assertNotIn('not found', report)
            self.assertNotIn('enter it', report)
            self.assertIn('no numeric limit', report.lower())
        plan = import_plan(extract_billing('Giving\n$32\n200 Monthly Donations', True), {'products': []})
        report = format_report(audit(plan, {'giving': []}, 90, now=NOW), 90, 'Account')
        self.assertIn('90-day total', report)
        self.assertIn('monthly', report)
        self.assertIn('not a monthly usage figure', report)
        with tempfile.TemporaryDirectory() as folder:
            saved, output = Path(folder)/'subscriptions.txt', Path(folder)/'report'
            saved.write_text('Giving\n$32\n200 Monthly Donations')
            with patch('pco_seat_audit.cli.api_get', return_value=collection('giving', [], 0)):
                code, out, err = self.run_cli(['--subscriptions', str(saved), '--live', '--output', str(output)])
            self.assertEqual((code, err), (0, ''))
            self.assertNotIn('live API plan limits', out)
        readme = (ROOT/'README.md').read_text()
        self.assertIn('PowerShell', readme)
        self.assertIn('Open a terminal', readme)
        self.assertIn('Extract', readme)
        self.assertIn('"products": [', readme)
        self.assertIn('date not supplied', readme)

    def test_8_readme_correction_example_runs_with_incomplete_paste(self):
        correction = json.loads(re.search(r'```json\n(.*?)\n```', (ROOT/'README.md').read_text(), re.S)[1])
        with tempfile.TemporaryDirectory() as folder:
            saved, plan, usage, output = [Path(folder)/name for name in ('subscriptions.txt', 'corrections.json', 'usage.json', 'report')]
            saved.write_text('Giving')
            plan.write_text(json.dumps(correction))
            usage.write_text('{"giving": []}')
            code, out, err = self.run_cli(['--subscriptions', str(saved), '--plan', str(plan), '--usage', str(usage), '--output', str(output)])
            self.assertEqual((code, err), (0, ''))
            self.assertIn('Giving: PAID BUT UNUSED', out)
            self.assertEqual(output.read_text(), out)

    def test_8_full_account_fixture_retains_unmeasured_products_and_undated_ladder(self):
        with tempfile.TemporaryDirectory() as folder:
            output = Path(folder)/'report'
            code, out, err = self.run_cli(['--subscriptions', str(ROOT/'examples/subscriptions-page-quirks.txt'),
                '--change-plans', str(ROOT/'examples/change-plans-page-quirks.txt'),
                '--account-roots', str(ROOT/'examples/account-roots-synthetic.json'),
                '--usage', str(ROOT/'examples/usage.json'), '--as-of', NOW.isoformat(), '--output', str(output)])
            self.assertEqual((code, err), (0, ''))
            for name in ('People', 'Calendar', 'Groups'):
                self.assertIn(name+': NOT ASSESSED — Billing imported, usage not measured', out)
            self.assertIn('pasted date: not supplied', out)
            self.assertIn('not a monthly usage figure', out)
            self.assertIn('no numeric limit for this tier', out)

    def test_read_only_transport_rejects_every_non_get_method_and_any_body(self):
        for method, data in [(method, None) for method in ('POST', 'PUT', 'PATCH', 'DELETE', 'HEAD', 'OPTIONS')] + [('GET', b'fixture')]:
            with self.subTest(method=method, body=data):
                with patch('pco_seat_audit.cli.build_opener') as opener:
                    with self.assertRaisesRegex(InputError, 'only GET'):
                        urlopen(Request(API_BASE+'/services/v2/', method=method, data=data), 20)
                opener.assert_not_called()
