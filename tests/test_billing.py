import hermetic  # Block live connections and discard inherited credentials.
import contextlib
import copy
import io
import json
import re
import tempfile
import unittest
from pathlib import Path

from pco_seat_audit.billing import extract_billing, merge_plan, format_review
from pco_seat_audit.cli import main

ROOT = Path(__file__).resolve().parents[1]
SAMPLE = (ROOT / 'examples/billing-synthetic.html').read_text()
SETTINGS = json.loads((ROOT / 'examples/import-settings.json').read_text())


def products(imported):
    return {item['product']: item for item in imported['products']}


class BillingTests(unittest.TestCase):
    def test_synthetic_sample_extracts_every_product_and_source(self):
        imported = extract_billing(SAMPLE)
        expected = {'check_ins': ('Large', 500, 40), 'registrations': ('Standard', 100, 30),
                    'giving': ('Standard', 100, 25), 'services': ('Standard', 100, 20),
                    'publishing': ('Standard', 20, 12)}
        for name, (tier, limit, price) in expected.items():
            entry = products(imported)[name]
            self.assertEqual((entry['paid'], entry['tier'], entry['limit'], entry['monthly_price']),
                             (True, tier, limit, price))
            for field in ('paid', 'tier', 'limit', 'monthly_price'):
                source = imported['review'][name][field]['candidates'][0]['source']
                self.assertIn(source['text'], SAMPLE.splitlines()[source['line'] - 1])
        self.assertEqual(imported['review']['check_ins']['price_shown']['value']['currency'], 'USD')

    def test_reordered_products_and_missing_product_are_not_guessed(self):
        imported = extract_billing('Publishing | Paid: yes | Plan: Mini | Limit: 20 | $12/month\n'
                                   'Check-in | Paid: no | Package: Free | Size: 50 | $0/month')
        self.assertEqual([p['product'] for p in imported['products']], ['check_ins', 'publishing'])
        self.assertEqual(products(imported)['publishing']['monthly_price'], 12)
        self.assertFalse(products(imported)['check_ins']['paid'])
        self.assertEqual(imported['review']['giving']['paid']['status'], 'not found — enter it')
        self.assertNotIn('giving', products(imported))

    def test_price_without_size_stays_partial(self):
        imported = extract_billing('Giving\nPaid: yes\nPlan: Example\nUSD 25.00 per month')
        self.assertEqual(products(imported)['giving']['monthly_price'], 25)
        self.assertNotIn('limit', products(imported)['giving'])
        self.assertIn('limit: not found — enter it', format_review(imported, {'products': []}))

    def test_thousands_separators_and_currency_symbols(self):
        for currency in ('USD', 'CAD', 'AUD', 'EUR', 'GBP', '$', '€', '£'):
            with self.subTest(currency=currency):
                entry = products(extract_billing(f'Services\nLimit: 1,250 people\n{currency} 1,234.56 /mo'))['services']
                self.assertEqual(entry['limit'], 1250)
                self.assertEqual(entry['monthly_price'], 1234.56)

    def test_explicit_unpaid_and_free_statuses(self):
        for status in ('Paid: no', 'Paid: false', 'Not paid', 'Not subscribed', 'No subscription',
                       'Free plan', 'Status: Free', 'Plan: Free'):
            with self.subTest(status=status):
                self.assertFalse(products(extract_billing('Giving\n' + status))['giving']['paid'])

    def test_markup_stripped_text_matches_html(self):
        plain = re.sub('<[^>]+>', '', SAMPLE)
        self.assertEqual(extract_billing(plain)['products'], extract_billing(SAMPLE)['products'])
        inline = extract_billing('<h2>Giving</h2><p>Plan: <b>Example</b></p><p>Limit: 100</p>')
        self.assertEqual(products(inline)['giving']['tier'], 'Example')

    def test_empty_irrelevant_and_scripts_do_not_extract_values(self):
        for text in ('', 'An unrelated saved document', '<script>Giving\nPaid: yes</script>',
                     '<style>Services</style><p>No billing here</p>'):
            with self.subTest(text=text):
                self.assertEqual(extract_billing(text)['products'], [])

    def test_conflicting_values_and_currency_are_reported(self):
        imported = extract_billing('Giving\nPaid: yes\nPaid: no\nLimit: 100\nLimit: 200\nUSD 25/month\nCAD 25/month')
        entry = products(imported)['giving']
        for field in ('paid', 'limit', 'monthly_price'):
            self.assertNotIn(field, entry)
            self.assertEqual(imported['review']['giving'][field]['status'], 'not found — enter it')
        review = format_review(imported, {'products': []})
        self.assertIn('source line 2: Paid: yes', review)
        self.assertIn('source line 3: Paid: no', review)

    def test_amount_and_active_status_do_not_imply_paid(self):
        entry = products(extract_billing('Giving\nStatus: active\n$25/month'))['giving']
        self.assertNotIn('paid', entry)
        self.assertNotIn('tier', entry)

    def test_annual_or_unknown_period_is_not_a_monthly_price(self):
        for price in ('$120/year', 'GBP 12.00', '$12/month and $120/year'):
            imported = extract_billing('Giving\n' + price)
            self.assertNotIn('monthly_price', products(imported)['giving'])
            self.assertTrue(imported['review']['giving']['price_shown']['candidates'])

    def test_other_products_navigation_and_promotions_do_not_bleed(self):
        imported = extract_billing('Giving\nPlan: Current\nUpgrade: Plan: Larger | Limit: 999 | $90/month\n'
                                   'Groups\nPaid: yes\nLimit: 800\n$25/month')
        self.assertEqual(products(imported)['giving'], {'product': 'giving', 'tier': 'Current'})
        self.assertIn('groups', products(imported))
        self.assertEqual(imported['unsupported'], [])
        self.assertEqual(extract_billing('Giving | Services | Publishing\nPaid: yes\nLimit: 999')['products'], [])

    def test_invalid_sizes_and_distant_values_stay_missing(self):
        for size in ('0', '-4', '1.5', '1,23', 'unlimited', '100 to 200', '100/200'):
            with self.subTest(size=size):
                self.assertNotIn('limit', products(extract_billing('Giving\nLimit: ' + size))['giving'])
        text = 'Giving\n' + 'unrelated line\n' * 7 + 'Limit: 300'
        self.assertNotIn('limit', products(extract_billing(text))['giving'])

    def test_merge_retains_products_metrics_and_explicit_corrections(self):
        imported = extract_billing(SAMPLE)
        base = copy.deepcopy(SETTINGS)
        base['products'][0]['limit'] = 600
        base['products'][0]['monthly_price'] = 50
        merged = merge_plan(imported, base)
        self.assertEqual(merged['products'][0]['limit'], 600)
        self.assertEqual(merged['products'][0]['monthly_price'], 50)
        self.assertEqual(merged['products'][0]['metric'], base['products'][0]['metric'])
        self.assertEqual(merged['products'][0]['lower_tier'], base['products'][0]['lower_tier'])
        self.assertEqual(len(merge_plan(imported, {'products': base['products'][:1]})['products']), 5)
        self.assertEqual(imported['products'][0]['limit'], 500)
        self.assertIn('plan file override: 600', format_review(imported, base))

    def test_simple_html_table_preserves_sources(self):
        imported = extract_billing('<h2>Giving</h2>\n<table>\n<tr><td>Plan</td><td>Example</td></tr>\n'
                                   '<tr><td>Limit</td><td>200</td></tr>\n<tr><td>Paid</td><td>No</td></tr></table>')
        self.assertEqual(products(imported)['giving'], {'product': 'giving', 'paid': False, 'tier': 'Example', 'limit': 200})
        self.assertIn('source line 3: Plan | Example', format_review(imported, {'products': []}))

    def test_negative_price_does_not_become_positive(self):
        for price in ('-$25/month', 'USD -25/month'):
            self.assertNotIn('monthly_price', products(extract_billing('Giving\n' + price))['giving'])

    def test_multiline_html_provenance_includes_source_span(self):
        imported = extract_billing('<h2>Giving</h2>\n<p>Plan: <b>\nExample</b></p>')
        source = imported['review']['giving']['tier']['candidates'][0]['source']
        self.assertEqual((source['line'], source['end_line']), (2, 3))
        self.assertIn('source line 2-3', format_review(imported, {'products': []}))


class BillingCliTests(unittest.TestCase):
    def run_cli(self, args):
        stdout, stderr = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
            code = main(args)
        return code, stdout.getvalue(), stderr.getvalue()

    def test_import_only_writes_partial_plan_with_review_no_sample_defaults(self):
        with tempfile.TemporaryDirectory() as folder:
            output = Path(folder) / 'partial.json'
            code, stdout, stderr = self.run_cli(['--billing-file', str(ROOT / 'examples/billing-synthetic.html'),
                                                '--import-only', '--usage', 'missing-usage.json', '--output', str(output)])
            self.assertEqual((code, stderr), (0, ''))
            plan = json.loads(output.read_text())
            self.assertEqual(len(plan['products']), 5)
            self.assertEqual(plan['products'][0]['metric'], {'type': 'distinct', 'field': 'person_id'})
            self.assertNotIn('lower_tier', plan['products'][0])
            self.assertEqual(plan['price_ladder']['default_date'], '2026-10-13')
            self.assertIn('import_review', plan)
            self.assertIn('source line 9: Paid: yes', stdout)

    def test_import_with_settings_builds_audit_and_overrides_are_visible(self):
        with tempfile.TemporaryDirectory() as folder:
            output = Path(folder) / 'report'
            base = copy.deepcopy(SETTINGS)
            base['products'][0]['monthly_price'] = 50
            base_path = Path(folder) / 'settings.json'
            base_path.write_text(json.dumps(base))
            code, stdout, stderr = self.run_cli(['--billing-file', str(ROOT / 'examples/billing-synthetic.html'),
                                                '--plan', str(base_path), '--usage', str(ROOT / 'examples/usage.json'),
                                                '--as-of', '2026-10-13T12:00:00Z', '--output', str(output)])
            self.assertEqual((code, stderr), (0, ''))
            self.assertEqual(stdout, output.read_text())
            self.assertIn('plan file override: 50', stdout)
            self.assertIn('possible saving 30.00 per month', stdout)
            self.assertIn('Giving: PAID BUT UNUSED', stdout)

    def test_missing_required_values_reviewed_before_clear_failure(self):
        with tempfile.TemporaryDirectory() as folder:
            billing = Path(folder) / 'saved.txt'
            billing.write_text('Giving\nPrice: USD 25/month')
            code, stdout, stderr = self.run_cli(['--billing-file', str(billing), '--usage', str(ROOT / 'examples/usage.json'), '--output', str(Path(folder) / 'report')])
            self.assertEqual(code, 2)
            self.assertIn('paid: not found — enter it', stdout)
            self.assertEqual(stderr.strip(), f'error: {billing}: giving: missing paid')
            self.assertFalse((Path(folder) / 'report').exists())

    def test_bad_files_and_arguments_are_one_line_errors(self):
        with tempfile.TemporaryDirectory() as folder:
            saved = Path(folder) / 'saved.txt'
            saved.write_text('Giving\nPaid: yes')
            empty = Path(folder) / 'empty.txt'
            empty.write_text('')
            irrelevant = Path(folder) / 'irrelevant.txt'
            irrelevant.write_text('No account information here')
            binary = Path(folder) / 'binary.txt'
            binary.write_bytes(b'\xff\xfe')
            bad_plan = Path(folder) / 'bad.json'
            bad_plan.write_text('{"products": [null]}')
            normal = ['--billing-file', str(saved), '--import-only', '--output', str(Path(folder) / 'partial.json')]
            cases = [normal + ['--billing-file', str(p)] for p in (empty, irrelevant, binary, Path(folder) / 'absent')]
            cases += [normal + ['--output', str(saved)], normal + ['--plan', str(bad_plan)],
                      normal + ['--output', str(Path(folder) / 'missing' / 'out')],
                      ['--import-only'], normal + ['--live']]
            for args in cases:
                with self.subTest(args=args):
                    code, _, stderr = self.run_cli(args)
                    self.assertEqual(code, 2)
                    self.assertTrue(stderr.startswith('error: '))
                    self.assertEqual(len(stderr.splitlines()), 1)
                    self.assertNotIn('Traceback', stderr)
            self.assertEqual(saved.read_text(), 'Giving\nPaid: yes')
