"""Dated monthly USD tiers; general pricing observed on 2026-10-13, not account data."""

import copy
import re

from .billing import BOUNDARIES, NUMBER, PRODUCTS, TextLines, normalize_dashes

LADDER_DATE = '2026-10-13'
PRICES = [0, 15, 32, 69, 115, 179, 239]
LIMITS = {
    'services': [5, 20, 50, 150, 400, 1000, None],
    'check_ins': [10, 30, 75, 200, 500, 1000, None],
    'giving': [10, 75, 200, 500, 1000, 1500, None],
    'groups': [15, 75, 200, 500, 1000, 1500, None],
    'registrations': [5, 20, 50, 150, 400, 1000, 5000],
    'calendar': [1, 5, 15, 30, 50, 75, None],
}
UNITS = {'services': 'Team Members', 'check_ins': 'Daily Check-Ins', 'giving': 'Monthly Donations',
         'groups': 'Group Members', 'registrations': 'Attendees', 'calendar': 'Rooms'}
STORAGE = ['100MB', '2GB', '4GB', '10GB', '30GB', '60GB', '200GB']
DEFAULT_LADDER = {
    name: [{'tier': f'{limit if limit is not None else "Unlimited"} {UNITS[name]}',
            'limit': limit, 'monthly_price': price,
            **({'storage': STORAGE[index]} if name == 'services' else {})}
           for index, (limit, price) in enumerate(zip(limits, PRICES))]
    for name, limits in LIMITS.items()
}
DEFAULT_LADDER['publishing'] = [{'tier': name, 'limit': None, 'monthly_price': price}
                                 for name, price in zip(('Essential', 'Plus', 'Premium'), (0, 15, 32))]
DEFAULT_LADDER['people'] = [{'tier': 'Unlimited', 'limit': None, 'monthly_price': 0}]


def parse_ladder(text):
    """Read '<limit> <unit> - $<price>' rows below a recognised product heading."""
    from .cli import InputError

    parser = TextLines(structured=bool(re.search(r'</?[A-Za-z][^>]*>', text)))
    parser.feed(normalize_dashes(text))
    parser.close()
    parser.flush()
    result = {}
    current = None
    for source in parser.lines:
        line = source['text']
        heading = BOUNDARIES.match(line)
        if heading:
            current = next((name for name, pattern in PRODUCTS.items() if re.fullmatch(pattern, heading[1], re.I)), None)
            continue
        match = re.fullmatch(r'(.+?)\s+[-–]\s+(?:USD\s*|\$)(' + NUMBER + r'(?:\.\d{2})?)', line, re.I)
        if not current or not match:
            continue
        tier, price = match[1], float(match[2].replace(',', ''))
        size = re.match(r'^(' + NUMBER + r')\s+(.+)', tier)
        named = current == 'publishing' and tier.lower() in {'essential', 'plus', 'premium'}
        unlimited = tier.lower().startswith('unlimited')
        if not (size or named or unlimited):
            continue
        unit = re.sub(r'\s*\([^)]*\)\s*$', '', size[2]).strip() if size else None
        if size and (current not in UNITS or unit.lower().rstrip('s') != UNITS[current].lower().rstrip('s')):
            raise InputError(f'{current}: pasted ladder unit does not match {UNITS.get(current, "a known unit")}')
        row = {'tier': tier, 'limit': int(size[1].replace(',', '')) if size else None,
               'monthly_price': price, 'source': source}
        storage = re.search(r'\((\d+(?:\.\d+)?\s*(?:MB|GB|TB)) Storage\)', tier, re.I)
        if current == 'services' and storage:
            row['storage'] = storage[1]
        if any(item['tier'] == tier or (size and item['limit'] == row['limit']) for item in result.get(current, [])):
            raise InputError(f'{current}: ambiguous duplicate tier in pasted ladder')
        result.setdefault(current, []).append(row)
    if not result:
        raise InputError('change-plans file contains no recognized tier rows; use product headings and limit/unit - price lines')
    return result


def apply_ladder(plan, pasted=None, pasted_date=None):
    """Attach a catalog for suggestions without replacing current subscription prices."""
    result = copy.deepcopy(plan)
    ladder = copy.deepcopy(DEFAULT_LADDER)
    if pasted:
        ladder.update(pasted)
    result['price_ladder'] = {'default_date': LADDER_DATE, 'pasted_date': pasted_date or 'not supplied',
                             'pasted_products': sorted(pasted or {}), 'currency': 'USD', 'period': 'month',
                             'before_tax': True}
    for spec in result['products']:
        name = spec['product']
        if name in ladder and spec.get("currency", "USD") in {"USD", "$"}:
            spec['price_ladder'] = ladder[name]
            spec['ladder_source'] = ('pasted Change plans, date ' + (pasted_date or 'not supplied')
                                     if pasted and name in pasted else 'built-in, date ' + LADDER_DATE)
    return result
