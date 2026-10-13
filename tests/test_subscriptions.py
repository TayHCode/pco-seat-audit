import hermetic  # Block live connections and discard inherited credentials.
import contextlib
import copy
import io
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from pco_seat_audit.billing import extract_billing, merge_plan
from pco_seat_audit.cli import InputError, account_capacity, audit, import_plan, main, setup_credentials
from pco_seat_audit.pricing import DEFAULT_LADDER, apply_ladder, parse_ladder
from test_audit import NOW, PLAN, ROOT, USAGE

TEXT = (ROOT / 'examples/subscriptions-synthetic.txt').read_text()
ROOTS = json.loads((ROOT / 'examples/account-roots-synthetic.json').read_text())
CHANGE = (ROOT / 'examples/change-plans-synthetic.txt').read_text()


class SubscriptionsTests(unittest.TestCase):
    def test_observed_names_bare_monthly_prices_plan_and_storage_lines(self):
        imported = extract_billing(TEXT, subscriptions=True)
        entries = {entry['product']: entry for entry in imported['products']}
        self.assertEqual(entries['services']['limit'], 80)
        self.assertEqual(entries['services']['monthly_price'], 45)
        self.assertEqual(entries['services']['storage'], '3GB Storage')
        self.assertEqual(entries['publishing']['tier'], 'Plus')
        self.assertNotIn('limit', entries['publishing'])
        self.assertTrue(all(entry['paid'] for entry in entries.values()))
        source = imported['review']['services']['limit']['candidates'][0]['source']
        self.assertEqual(source['text'], '80 Team Members')
        self.assertIn('positive/zero prices mean paid/not paid', imported['interpretation'])

    def test_all_eight_observed_product_names_and_zero_price(self):
        text = ('Membership Management\n$0\nUnlimited\nEvents & Facility Management\n$8\n3 Rooms\n'
                'Groups Management\n$7\n120 Group Members\n' + TEXT)
        entries = {entry['product']: entry for entry in extract_billing(text, subscriptions=True)['products']}
        self.assertEqual(len(entries), 8)
        self.assertFalse(entries['people']['paid'])
        self.assertEqual(entries['calendar']['limit'], 3)
        self.assertEqual(entries['groups']['limit'], 120)

    def test_missing_price_size_and_invoice_are_not_fabricated(self):
        imported = extract_billing('Donation Management\n$23\nNext invoice $99\nEvent Signups\n90 Attendees', subscriptions=True)
        entries = {entry['product']: entry for entry in imported['products']}
        self.assertNotIn('limit', entries['giving'])
        self.assertEqual(entries['giving']['monthly_price'], 23)
        self.assertNotIn('paid', entries['registrations'])
        self.assertNotIn('monthly_price', entries['registrations'])

    def test_roots_override_pasted_and_manual_limits_and_services_uses_seats(self):
        plan = import_plan(extract_billing(TEXT, subscriptions=True), {'products': []})
        plan['products'][0]['limit'] = 999
        results = audit(plan, USAGE, 90, now=NOW, account_roots=ROOTS)
        by_name = {entry['product']: entry for entry in results}
        self.assertEqual(by_name['check_ins']['limit'], 360)
        self.assertEqual((by_name['services']['used'], by_name['services']['limit']), (7, 80))
        self.assertEqual(by_name['services']['account_capacity']['file_storage_size_used'], 800)
        self.assertEqual(by_name['publishing']['finding'], 'USAGE RECORDED — named Publishing tier; compare features manually')
        usage = copy.deepcopy(USAGE)
        usage['publishing'] = []
        by_name = {entry['product']: entry for entry in audit(plan, usage, 90, now=NOW, account_roots=ROOTS)}
        self.assertEqual(by_name['publishing']['finding'], 'PAID BUT UNUSED')

    def test_invalid_root_and_missing_seat_snapshot_fail_clearly(self):
        fixture = copy.deepcopy(ROOTS['services'])
        fixture['data']['attributes']['people_remaining'] = 90
        with self.assertRaisesRegex(InputError, 'exceeds'):
            account_capacity('services', fixture)
        plan = import_plan(extract_billing(TEXT, subscriptions=True), {'products': []})
        with self.assertRaisesRegex(InputError, 'current seats need'):
            audit(plan, USAGE, 90, now=NOW)

    def test_awkward_subscriptions_order_thousands_and_html(self):
        text = 'Event Signups\n$1,234.56\n1,250 Attendees\nDonation Management\n$0\n10 Monthly Donations'
        imported = extract_billing(text, subscriptions=True)
        entries = {p['product']: p for p in imported['products']}
        self.assertEqual(entries['registrations']['limit'], 1250)
        self.assertEqual(entries['registrations']['monthly_price'], 1234.56)
        self.assertFalse(entries['giving']['paid'])
        html = '\n'.join('<p>' + line + '</p>' for line in text.splitlines())
        self.assertEqual(extract_billing(html, subscriptions=True)['products'], imported['products'])


class PricingTests(unittest.TestCase):
    def test_dated_default_is_general_pricing_and_suggests_a_fitting_lower_tier(self):
        plan = apply_ladder(import_plan(extract_billing(TEXT, subscriptions=True), {'products': []}))
        self.assertEqual(plan['price_ladder']['default_date'], '2026-10-13')
        self.assertEqual(len(DEFAULT_LADDER), 8)
        results = {p['product']: p for p in audit(plan, USAGE, 90, now=NOW, account_roots=ROOTS)}
        self.assertIn('10 Daily Check-Ins', results['check_ins']['recommendation'])
        self.assertEqual(results['check_ins']['possible_monthly_saving'], 44)
        self.assertIn('built-in, date 2026-10-13', results['check_ins']['ladder_source'])

    def test_pasted_ladder_replaces_product_ladder_and_preserves_price_sources(self):
        ladder = parse_ladder(CHANGE)
        plan = apply_ladder(import_plan(extract_billing(TEXT, subscriptions=True), {'products': []}), ladder, '2026-10-13')
        self.assertEqual(len(plan['products'][0]['price_ladder']), 3)
        self.assertEqual(ladder['check_ins'][1]['source']['text'], '45 Daily Check-Ins - $9')
        results = {p['product']: p for p in audit(plan, USAGE, 90, now=NOW, account_roots=ROOTS)}
        self.assertIn('12 Daily Check-Ins', results['check_ins']['recommendation'])
        self.assertIn('pasted Change plans', results['services']['ladder_source'])
        self.assertIn('built-in', results['giving']['ladder_source'])
        self.assertEqual(plan['products'][0]['monthly_price'], 44)

    def test_invalid_ladder_and_duplicate_rows_are_errors(self):
        for text in ('', 'no tiers', 'Giving\n20 Rooms - $9', 'Giving\n20 Monthly Donations - $9\n20 Monthly Donations - $11'):
            with self.subTest(text=text), self.assertRaises(InputError):
                parse_ladder(text)

    def test_non_usd_current_prices_do_not_use_usd_ladder(self):
        plan = {'products': [dict(PLAN['products'][0], currency='GBP')]}
        self.assertNotIn('price_ladder', apply_ladder(plan)['products'][0])

    def test_import_only_retains_pasted_ladder_and_review(self):
        with tempfile.TemporaryDirectory() as folder:
            output = Path(folder) / 'plan.json'
            stdout = io.StringIO()
            with contextlib.redirect_stdout(stdout):
                code = main(['--subscriptions', str(ROOT / 'examples/subscriptions-synthetic.txt'), '--import-only',
                             '--change-plans', str(ROOT / 'examples/change-plans-synthetic.txt'),
                             '--ladder-date', '2026-10-13', '--output', str(output)])
            self.assertEqual(code, 0)
            plan = json.loads(output.read_text())
            self.assertIn('check_ins', plan['price_ladder']['pasted_products'])
            self.assertIn('pasted date: 2026-10-13', stdout.getvalue())
            self.assertIn('import_review', plan)


class SetupTests(unittest.TestCase):
    def test_environment_values_are_never_printed_or_saved(self):
        stdout = io.StringIO()
        with patch.dict('os.environ', {'PCO_APP_ID': 'SYNTHETIC-ID', 'PCO_SECRET': 'SYNTHETIC-TOKEN'}, clear=True):
            with contextlib.redirect_stdout(stdout), patch('pco_seat_audit.cli.api_get') as transport:
                self.assertEqual(main(['setup']), 0)
                transport.assert_not_called()
        self.assertNotIn('SYNTHETIC-ID', stdout.getvalue())
        self.assertNotIn('SYNTHETIC-TOKEN', stdout.getvalue())
        self.assertIn('full access level', stdout.getvalue())
        self.assertIn('deleting the token', stdout.getvalue())

    def test_both_prompt_values_use_hidden_input_and_stay_in_memory(self):
        stdout = io.StringIO()
        with patch.dict('os.environ', {}, clear=True), patch('sys.stdin.isatty', return_value=True):
            with patch('getpass.getpass', side_effect=['SYNTHETIC-ID', 'SYNTHETIC-TOKEN']) as prompt:
                with contextlib.redirect_stdout(stdout):
                    self.assertEqual(setup_credentials(), ('SYNTHETIC-ID', 'SYNTHETIC-TOKEN'))
        self.assertEqual(prompt.call_count, 2)
        self.assertNotIn('SYNTHETIC-TOKEN', stdout.getvalue())

    def test_unattended_setup_is_one_line_error_without_network(self):
        stdout, stderr = io.StringIO(), io.StringIO()
        with patch.dict('os.environ', {}, clear=True), patch('sys.stdin.isatty', return_value=False):
            with contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
                code = main(['setup'])
        self.assertEqual(code, 2)
        self.assertEqual(len(stderr.getvalue().splitlines()), 1)
        self.assertNotIn('Traceback', stderr.getvalue())
