import hermetic  # Clear credentials and block sockets before importing the client.
import contextlib
import copy
import io
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch
from urllib.parse import urlencode, parse_qs, urlsplit

from pco_seat_audit.billing import extract_billing
from pco_seat_audit.cli import (API_BASE, IMPORT_METRICS, InputError, api_records,
                                audit, main, import_plan, format_report, NoRedirect)
from pco_seat_audit.pricing import apply_ladder, parse_ladder
from test_audit import NOW, ROOT

SUBSCRIPTIONS = ROOT / 'examples/subscriptions-page-quirks.txt'
LADDER = ROOT / 'examples/change-plans-page-quirks.txt'


def imported_plan():
    return apply_ladder(import_plan(extract_billing(SUBSCRIPTIONS.read_text(), subscriptions=True),
                                   {'products': []}), parse_ladder(LADDER.read_text()))


def collection(name, rows, total=None, next_params=None):
    return {'data': rows, 'meta': {'total_count': len(rows) if total is None else total,
                                 'can_query_by': ['created_at']},
            'links': {'next': API_BASE + IMPORT_METRICS[name]['api']['path'] + '?' + urlencode(next_params)
                      if next_params else None}}


def activity(person='PERSON-PLACEHOLDER-1', date=None):
    return {'attributes': {'created_at': date or NOW.isoformat()},
            'relationships': {'person': {'data': {'id': person}}}}


class PageQuirkTests(unittest.TestCase):
    def test_all_products_and_dash_variants(self):
        original = SUBSCRIPTIONS.read_text()
        for dash in ('‐', '‑', '‒', '–', '—', '−', '﹘', '﹣', '－', '-'):
            with self.subTest(dash=dash):
                entries = extract_billing(original.replace('‑', dash), subscriptions=True)['products']
                self.assertEqual(len(entries), 8)
                check = next(row for row in entries if row['product'] == 'check_ins')
                self.assertEqual((check['paid'], check['limit'], check['monthly_price']), (False, 18, 0))
        text = original.replace('Child & Volunteer Check‑In', 'cHeCk  –  iNs')
        self.assertIn('check_ins', [row['product'] for row in extract_billing(text, True)['products']])

    def test_trailing_status_and_invoice_do_not_change_services(self):
        text = SUBSCRIPTIONS.read_text()
        for source in (text, '\n'.join('<p>' + line + '</p>' for line in text.splitlines())):
            result = extract_billing(source, subscriptions=True)
            service = next(row for row in result['products'] if row['product'] == 'services')
            self.assertEqual((service['paid'], service['limit'], service['monthly_price'], service['storage']),
                             (True, 240, 61, '17GB Storage'))
            self.assertEqual(len(result['review']['services']['paid']['candidates']), 1)
        no_storage = text.replace('17GB Storage\n', '')
        self.assertTrue(next(row for row in extract_billing(no_storage, True)['products']
                             if row['product'] == 'services')['paid'])

        self.assertFalse(extract_billing('Services\nNot Subscribed', True)['products'][0]['paid'])

    def test_ladder_singular_plural_storage_and_dash(self):
        ladder = parse_ladder(LADDER.read_text().replace(' - ', ' — '))
        self.assertEqual([row['limit'] for row in ladder['calendar']], [1, 2, 9])
        self.assertEqual(ladder['services'][1]['storage'], '3GB')
        self.assertIsNone(ladder['services'][-1]['limit'])
        self.assertIn('check_ins', ladder)
        with self.assertRaises(InputError):
            parse_ladder('Calendar\n2 Donations - $9')

    def test_next_tier_cannot_fit_then_fits_after_seats_freed(self):
        spec = next(row for row in imported_plan()['products'] if row['product'] == 'services')
        for used, expected in ((84, 'CURRENT TIER NEEDED'), (70, 'LOW USAGE')):
            roots = {'services': {'data': {'type': 'Organization', 'attributes': {
                'people_allowed': 240, 'people_remaining': 240 - used}}}}
            result = audit({'products': [spec]}, {}, 90, now=NOW, account_roots=roots)[0]
            self.assertEqual(result['finding'], expected)
            if used == 84:
                self.assertNotIn('recommendation', result)
                self.assertNotIn('possible_monthly_saving', result)
                self.assertIn('Free 14 Team Members', result['next_tier_requirement'])
                self.assertIn('38.00 per month', result['next_tier_requirement'])
                self.assertNotIn('LOW USAGE', format_report([result], 90, 'Synthetic sample'))
            else:
                self.assertEqual(result['possible_monthly_saving'], 38)
                self.assertIn('70 Team Members', result['recommendation'])


class WindowUsageTests(unittest.TestCase):
    def spec(self, name):
        return next(row for row in imported_plan()['products'] if row['product'] == name)

    def test_giving_uses_filtered_total_for_every_window(self):
        spec = self.spec('giving')
        for days, total in ((30, 33), (60, 66), (90, 99)):
            payload = collection('giving', [activity()], total=total,
                                 next_params={'offset': '1'})
            with patch('pco_seat_audit.cli.api_get', return_value=payload) as get:
                result = audit({'products': [spec]}, {}, days, now=NOW, live=True)[0]
            self.assertEqual(result['used'], total)
            self.assertNotEqual(result['finding'], 'NOT ASSESSED')
            get.assert_called_once()
            params = get.call_args.args[1]
            self.assertEqual(params['per_page'], '1')
            self.assertIn('where[created_at][gte]', params)
            self.assertEqual(params['where[created_at][lte]'], NOW.isoformat())
            self.assertNotIn('PERSON-PLACEHOLDER-1', format_report([result], days, 'Synthetic sample'))

    def test_check_ins_pages_then_deduplicates_in_each_window(self):
        spec = self.spec('check_ins')
        for days in (30, 60, 90):
            from datetime import timedelta
            params = {'per_page': '100', 'offset': '2',
                      'where[created_at][gte]': (NOW - timedelta(days=days)).isoformat(),
                      'where[created_at][lte]': NOW.isoformat()}
            pages = [collection('check_ins', [activity(), activity('PERSON-PLACEHOLDER-2')], 4, params),
                     collection('check_ins', [activity(), activity('PERSON-PLACEHOLDER-3')], 4)]
            with patch('pco_seat_audit.cli.api_get', side_effect=pages) as get:
                rows = api_records('check_ins', spec, days, NOW)
            self.assertIsNone(rows.incomplete_reason)
            with patch('pco_seat_audit.cli.api_records', return_value=rows), patch('pco_seat_audit.cli.api_get',
                    return_value={'data': {'type': 'Organization', 'attributes': {}}}):
                result = audit({'products': [spec]}, {}, days, now=NOW, live=True)[0]
            self.assertEqual(result['used'], 3)
            self.assertEqual(get.call_count, 2)
            self.assertEqual(get.call_args.args[1], params)

    def test_unfiltered_totals_or_out_of_window_rows_never_support_savings(self):
        for name in ('giving', 'check_ins'):
            payload = collection(name, [activity()], total=999)
            payload['meta'].pop('can_query_by')
            with patch('pco_seat_audit.cli.api_get', return_value=payload):
                rows = api_records(name, self.spec(name), 90, NOW)
            self.assertIsNotNone(rows.incomplete_reason)
            with patch('pco_seat_audit.cli.api_get', return_value=collection(name, [activity(date='2000-01-01')])):
                with self.assertRaisesRegex(InputError, 'outside the requested window'):
                    api_records(name, self.spec(name), 90, NOW)

    def test_page_bound_repeats_changed_totals_and_missing_people_are_incomplete(self):
        spec = self.spec('check_ins')
        from datetime import timedelta
        params = {'offset': '1', 'where[created_at][gte]': (NOW-timedelta(days=90)).isoformat(),
                  'where[created_at][lte]': NOW.isoformat()}
        payload = collection('check_ins', [activity()], 3, params)
        with patch('pco_seat_audit.cli.MAX_PAGES', 1), patch('pco_seat_audit.cli.api_get', return_value=payload):
            self.assertIn('limit reached', api_records('check_ins', spec, 90, NOW).incomplete_reason)
        with patch('pco_seat_audit.cli.api_get', return_value=payload) as get:
            self.assertIsNotNone(api_records('check_ins', spec, 90, NOW).incomplete_reason)
            self.assertEqual(get.call_count, 2)
        with patch('pco_seat_audit.cli.api_get', side_effect=[payload, collection('check_ins', [activity()], 4)]):
            self.assertIn('changed', api_records('check_ins', spec, 90, NOW).incomplete_reason)
        missing = activity(); missing['relationships'] = {}
        root = {'data': {'type': 'Organization', 'attributes': {}}}
        with patch('pco_seat_audit.cli.api_get', side_effect=[root, collection('check_ins', [missing])]):
            paid = dict(spec, paid=True, monthly_price=39)
            result = audit({'products': [paid]}, {}, 90, now=NOW, live=True)[0]
        self.assertEqual(result['finding'], 'NOT ASSESSED')
        self.assertNotIn('possible_monthly_saving', result)
        self.assertNotIn('next_tier_requirement', result)

    def test_unsafe_or_filter_losing_next_links_fail_before_second_request(self):
        spec = self.spec('check_ins')
        path = IMPORT_METRICS['check_ins']['api']['path']
        for link in ('//NETWORK-PLACEHOLDER' + path, API_BASE.replace('https:', 'http:') + path,
                     API_BASE + '/giving/v2/donations', API_BASE + path + '?offset=2'):
            payload = collection('check_ins', [activity()], 3)
            payload['links']['next'] = link
            with patch('pco_seat_audit.cli.api_get', return_value=payload) as get:
                with self.assertRaises(InputError):
                    api_records('check_ins', spec, 90, NOW)
            get.assert_called_once()


class FetchTests(unittest.TestCase):
    def test_readme_fetch_command_get_only_from_environment(self):
        class Response:
            def __init__(self, payload): self.payload = payload
            def __enter__(self): return self
            def __exit__(self, *args): pass
            def read(self): return json.dumps(self.payload).encode()
        requests = []
        def open_request(request, timeout):
            requests.append(request)
            parts = urlsplit(request.full_url)
            self.assertEqual(parts.netloc, urlsplit(API_BASE).netloc)
            self.assertEqual(request.get_method(), 'GET')
            self.assertIsNone(request.data)
            self.assertNotIn('SYNTHETIC-TOKEN', request.full_url)
            if parts.path == '/services/v2/':
                payload = {'data': {'type': 'Organization', 'attributes': {'people_allowed': 240, 'people_remaining': 156}}}
            elif parts.path == '/check-ins/v2/':
                payload = {'data': {'type': 'Organization', 'attributes': {'daily_check_ins': 18}}}
            elif parts.path == '/giving/v2/donations':
                self.assertIn('where[created_at][gte]', parse_qs(parts.query))
                payload = collection('giving', [activity()], 99)
            else:
                payload = {'data': [], 'meta': {'total_count': 0, 'can_query_by': ['created_at']}}
            return Response(payload)
        with tempfile.TemporaryDirectory() as folder:
            destination = Path(folder)/'report.txt'
            args = ['fetch', '--subscriptions', str(SUBSCRIPTIONS), '--change-plans', str(LADDER), '--output', str(destination), '--as-of', NOW.isoformat()]
            stdout, stderr = io.StringIO(), io.StringIO()
            with patch.dict('os.environ', {'PCO_APP_ID': 'SYNTHETIC-ID', 'PCO_SECRET': 'SYNTHETIC-TOKEN'}, clear=True), \
                    patch('pco_seat_audit.cli.urlopen', side_effect=open_request), patch('pco_seat_audit.cli.time.sleep') as delay, \
                    contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
                self.assertEqual(main(args), 0)
            self.assertEqual(stderr.getvalue(), '')
            self.assertEqual(stdout.getvalue(), destination.read_text())
            self.assertIn('Services: CURRENT TIER NEEDED', stdout.getvalue())
            self.assertIn('Check Ins: NOT PAID', stdout.getvalue())
            self.assertNotIn('SYNTHETIC-TOKEN', stdout.getvalue())
            self.assertNotIn('SYNTHETIC-ID', stdout.getvalue())
            self.assertNotIn('PERSON-PLACEHOLDER-1', stdout.getvalue())
            self.assertEqual(len(requests), 6)
            self.assertGreaterEqual(delay.call_count, 5)
            self.assertIsNone(NoRedirect().redirect_request(None, None, 302, '', {}, API_BASE))

    def test_fetch_bad_response_is_one_line_error_without_report(self):
        with tempfile.TemporaryDirectory() as folder:
            out = Path(folder)/'report'
            stdout, stderr = io.StringIO(), io.StringIO()
            with patch('pco_seat_audit.cli.api_get', return_value={'data': 'bad'}), \
                    contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
                code = main(['fetch', '--subscriptions', str(SUBSCRIPTIONS), '--change-plans', str(LADDER), '--output', str(out)])
            self.assertEqual(code, 2)
            self.assertFalse(out.exists())
            self.assertEqual(len(stderr.getvalue().splitlines()), 1)
            self.assertNotIn('Traceback', stderr.getvalue())
