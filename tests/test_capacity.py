import hermetic  # Block live connections and discard inherited credentials.
import copy
import io
import json
import unittest
from unittest.mock import patch
from urllib.parse import parse_qs, urlsplit

from pco_seat_audit.billing import extract_billing
from pco_seat_audit.cli import InputError, NoRedirect, account_capacity, api_get, audit, format_report, import_plan, services_capacity
from test_audit import NOW, PLAN, ROOT, USAGE

ROOTS = json.loads((ROOT / 'examples/account-roots-synthetic.json').read_text())
CAPACITY = ROOTS["services"]


class CapacityTests(unittest.TestCase):
    def test_documented_fixture_is_used_in_report_without_mixing_units(self):
        results = audit(PLAN, USAGE, 90, now=NOW, account_roots=ROOTS)
        services = next(item for item in results if item['product'] == 'services')
        self.assertEqual(services['account_capacity'], {'people_allowed': 80, 'people_remaining': 73, 'seats_used': 7, 'file_storage_size': 6000, 'file_storage_size_used': 800})
        self.assertEqual((services['used'], services['limit'], services['unit']), (7, 80, 'Team Members (current seats, not look-back activity)'))
        report = format_report(results, 90, 'Synthetic organization')
        self.assertIn('offline API-root fixture', report)
        self.assertIn('people_allowed=80', report)
        self.assertIn('Services seats are a current snapshot', report)
        self.assertIn('Services storage: 800 used / 6000 allowed (raw account values; unit unknown to this tool)', report)
        self.assertNotIn("manual plan's unit", report)

    def test_manual_services_activity_is_explicitly_not_seat_usage(self):
        results = audit(PLAN, USAGE, 90, now=NOW)
        services = next(item for item in results if item['product'] == 'services')
        self.assertEqual((services['used'], services['limit'], services['unit']), (9, 100, 'service plans'))
        report = format_report(results, 90, 'Synthetic organization')
        self.assertIn('9 / 100 service plans', report)
        self.assertIn("90-day activity count in the manual plan's unit (service plans)", report)
        self.assertIn('does not measure occupied seats or seat/licence usage', report)
        self.assertNotIn('Services seats are a current snapshot', report)
        self.assertIn('this is not a daily usage figure', report)
        empty_root = {'services': {'data': {'type': 'Organization', 'attributes': {}}}}
        report = format_report(audit(PLAN, USAGE, 90, now=NOW, account_roots=empty_root), 90, 'Synthetic organization')
        self.assertIn('9 / 100 service plans', report)
        self.assertIn('does not measure occupied seats or seat/licence usage', report)
        self.assertNotIn('Services seats are a current snapshot', report)

    def test_imported_services_uses_same_seat_unit_offline_and_live(self):
        plan = import_plan(extract_billing((ROOT / 'examples/subscriptions-synthetic.txt').read_text(), subscriptions=True), {'products': []})
        plan['products'] = [item for item in plan['products'] if item['product'] == 'services']
        offline = audit(plan, USAGE, 90, now=NOW, account_roots=ROOTS)
        with patch('pco_seat_audit.cli.api_get', return_value=CAPACITY) as transport:
            live = audit(plan, {}, 90, live=True, now=NOW)
        self.assertEqual(transport.call_count, 1)
        for results in (offline, live):
            self.assertEqual((results[0]['used'], results[0]['limit']), (7, 80))
            report = format_report(results, 90, 'Synthetic organization')
            self.assertIn('7 / 80 Team Members (current seats, not look-back activity)', report)
            self.assertIn('current snapshot of occupied seats', report)
            self.assertNotIn("manual plan's unit", report)

    def test_missing_and_invalid_capacity_are_not_invented(self):
        empty = {'data': {'type': 'Organization', 'attributes': {}}}
        self.assertEqual(services_capacity(empty), {'people_allowed': None, 'people_remaining': None, 'file_storage_size': None, 'file_storage_size_used': None})
        for value in (-1, True, '75'):
            fixture = copy.deepcopy(CAPACITY)
            fixture['data']['attributes']['people_allowed'] = value
            with self.assertRaisesRegex(InputError, 'non-negative integer'):
                services_capacity(fixture)
        for payload in ({}, {'data': []}, {'data': {'type': 'Person', 'attributes': {}}}):
            with self.assertRaisesRegex(InputError, 'Organization'):
                services_capacity(payload)

    def test_redirects_never_forward_credentials(self):
        self.assertIsNone(NoRedirect().redirect_request(None, None, 302, 'redirect', {}, 'https://example.invalid/'))

    def test_check_ins_root_request_and_reader_with_no_network(self):
        with patch.dict('os.environ', {'PCO_APP_ID': 'SYNTHETIC-APP', 'PCO_SECRET': 'SYNTHETIC-SECRET'}, clear=True):
            with patch('pco_seat_audit.cli.urlopen', return_value=io.BytesIO(json.dumps(ROOTS['check_ins']).encode())) as transport:
                payload = api_get('/check-ins/v2/', {'fields[Organization]': 'daily_check_ins'}, 'check_ins')
        request = transport.call_args.args[0]
        self.assertEqual(request.get_method(), 'GET')
        self.assertEqual(urlsplit(request.full_url).path, '/check-ins/v2/')
        self.assertIsNone(request.data)
        self.assertEqual(account_capacity('check_ins', payload), {'daily_check_ins': 360})

    def test_get_request_construction_with_offline_response_no_network(self):
        # Only the transport boundary is replaced with fixture bytes; no live CLI run or socket.
        with patch.dict('os.environ', {'PCO_APP_ID': 'SYNTHETIC-APP', 'PCO_SECRET': 'SYNTHETIC-SECRET'}, clear=True):
            with patch('pco_seat_audit.cli.urlopen', return_value=io.BytesIO(json.dumps(CAPACITY).encode())) as transport:
                payload = api_get('/services/v2', {'fields[Organization]': 'people_allowed,people_remaining'}, 'services')
        request = transport.call_args.args[0]
        self.assertEqual(request.get_method(), 'GET')
        self.assertIsNone(request.data)
        url = urlsplit(request.full_url)
        self.assertEqual(url.path, '/services/v2')
        self.assertEqual(parse_qs(url.query), {'fields[Organization]': ['people_allowed,people_remaining']})
        self.assertEqual(services_capacity(payload)['people_allowed'], 80)
