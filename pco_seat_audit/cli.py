"""Offline-first usage audit with an optional read-only Planning Center API client."""

import argparse
import base64
import getpass
import warnings
import json
import math
import os
import sys
import time
from datetime import datetime, timedelta, timezone
from http.client import HTTPException
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.parse import parse_qsl, urlencode, urlsplit
from urllib.request import HTTPRedirectHandler, Request, build_opener

from .billing import PRODUCTS, extract_billing, merge_plan, format_review
from .pricing import apply_ladder, parse_ladder

ALLOWED_DAYS = {30, 60, 90}
KNOWN_PRODUCTS = set(PRODUCTS)
API_BASE = "https://api.planningcenteronline.com"
MAX_PAGES = 100
REQUEST_DELAY = 0.25
_last_request = None
GET_REQUESTS = 0


class InputError(Exception):
    """A user-correctable input or API error."""

    def __init__(self, message, source=None, product=None, field=None):
        super().__init__(message)
        self.source = source
        self.product = product
        self.field = field


def read_text(path):
    try:
        return Path(path).read_text(encoding="utf-8-sig")
    except OSError as exc:
        raise InputError(f"cannot read {path}: {exc.strerror or 'I/O error'}") from None
    except UnicodeDecodeError:
        raise InputError(f"cannot read {path}: use UTF-8 text or HTML") from None


def validate_json_depth(payload):
    pending = [(payload, 0)]
    while pending:
        value, depth = pending.pop()
        if depth > 100:
            raise ValueError("JSON nesting is too deep")
        if isinstance(value, dict):
            pending.extend((child, depth + 1) for child in value.values() if isinstance(child, (dict, list)))
        elif isinstance(value, list):
            pending.extend((child, depth + 1) for child in value if isinstance(child, (dict, list)))
    return payload


def read_json(path):
    try:
        return validate_json_depth(json.loads(read_text(path)))
    except json.JSONDecodeError as exc:
        raise InputError(f"invalid JSON in {path} at line {exc.lineno}") from None
    except (RecursionError, ValueError):
        raise InputError(f"invalid JSON in {path}: nesting or numeric value is too large") from None


def finite_number(value):
    if not isinstance(value, (int, float)) or isinstance(value, bool):
        return False
    try:
        return math.isfinite(value)
    except OverflowError:
        return False


def parse_time(value):
    if not isinstance(value, str) or not value:
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)
        return parsed.astimezone(timezone.utc)
    except (ValueError, OverflowError):
        return None


def window_start(now, days):
    try:
        return now - timedelta(days=days)
    except OverflowError:
        raise InputError("report reference time is too early for the selected window") from None


class UsageRows(list):
    """Rows plus a reason why their count must not support a downgrade."""

    def __init__(self, rows, incomplete_reason=None, exact_count=None):
        super().__init__(rows)
        self.incomplete_reason = incomplete_reason
        self.exact_count = exact_count


def validate_api_path(path, product):
    if (not isinstance(path, str) or not path.startswith("/") or path.startswith("//")
            or ".." in path.split("/") or "\\" in path or "?" in path or "#" in path
            or any(ord(char) < 33 or ord(char) > 126 for char in path)
            or urlsplit(path).netloc):
        raise InputError(f"{product}: api.path must be an absolute API path with ASCII characters and no spaces or query")


def page_info(payload, product):
    records = payload.get("data") if isinstance(payload, dict) else None
    if not isinstance(records, list):
        raise InputError(f"{product}: API response is missing JSON:API data array")
    meta, links = payload.get("meta", {}), payload.get("links", {})
    if not isinstance(meta, dict) or not isinstance(links, dict):
        raise InputError(f"{product}: API response has malformed pagination information")
    next_link = links.get("next")
    if next_link is not None and not isinstance(next_link, str):
        raise InputError(f"{product}: API response has a malformed next-page link")
    total = meta.get("total_count")
    if total is not None and (type(total) is not int or total < len(records)):
        raise InputError(f"{product}: API response has an invalid total count")
    return records, meta, links, total


def next_page(link, path, product):
    try:
        parts = urlsplit(link)
    except ValueError:
        raise InputError(f"{product}: malformed next-page link") from None
    base = urlsplit(API_BASE)
    if (parts.scheme != base.scheme or parts.netloc != base.netloc
            or parts.path != path or parts.fragment):
        raise InputError(f"{product}: next-page link must stay on the fixed API host and collection")
    validate_api_path(parts.path, product)
    return dict(parse_qsl(parts.query, keep_blank_values=True))


def api_records(product, spec, days, now, credentials=None):
    """Use proven date-filtered totals for Giving; page distinct or other metrics."""
    app = spec.get("api")
    if not isinstance(app, dict) or not isinstance(app.get("path"), str):
        raise InputError(f"{product}: missing api.path in plan file")
    path = app["path"]
    cutoff = window_start(now, days)
    filtered = (product in {"giving", "check_ins"} and app.get("date_field") == "created_at"
                and path == IMPORT_METRICS[product]["api"]["path"])
    params = {"per_page": "100"}
    if filtered:
        params.update({"where[created_at][gte]": cutoff.isoformat(),
                       "where[created_at][lte]": now.isoformat()})
    count_total = filtered and product == "giving" and spec["metric"]["type"] == "records"
    if count_total:
        params["per_page"] = "1"
    rows = []
    seen = set()
    reason = "API pagination or request limit reached; usage may be incomplete. No downgrade or saving can be assessed."
    expected_total = None
    for page in range(MAX_PAGES):
        signature = tuple(sorted(params.items()))
        if signature in seen:
            return UsageRows(rows, reason)
        seen.add(signature)
        records, meta, links, total = page_info(api_get(path, params, product, credentials), product)
        link = links.get("next")
        following = next_page(link, path, product) if link else None
        if filtered:
            supported = meta.get("can_query_by")
            if not isinstance(supported, list) or "created_at" not in supported:
                return UsageRows(rows, "API did not confirm support for date filters; usage may be incomplete. No downgrade or saving can be assessed.")
            for row in records:
                attrs = row.get("attributes", {}) if isinstance(row, dict) else {}
                stamp = parse_time(attrs.get("created_at")) if isinstance(attrs, dict) else None
                if stamp is None or not cutoff <= stamp <= now:
                    raise InputError(f"{product}: API date-filtered response contains activity outside the requested window")
        if count_total and total is not None:
            if total > 0 and not records:
                return UsageRows([], reason)
            return UsageRows(records, exact_count=total)
        if page == 0:
            expected_total = total
        elif total != expected_total:
            return UsageRows(rows, "API collection changed during pagination; usage may be incomplete. Run again before considering a downgrade.")
        rows.extend(records)
        if not following:
            complete = ((total is not None and len(rows) == total)
                        or (total is None and "next" in links and len(records) < 100))
            return UsageRows(rows, None if complete else reason)
        if not records:
            return UsageRows(rows, reason)
        if filtered and any(following.get(key) != params[key] for key in
                            ("where[created_at][gte]", "where[created_at][lte]")):
            raise InputError(f"{product}: next-page link lost the requested date filters")
        params = following
    return UsageRows(rows, reason)


class NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        # Never forward a full-access token to a redirect target.
        return None


def urlopen(request, timeout):
    if request.get_method() != "GET" or request.data is not None:
        raise InputError("only GET requests with no body are permitted")
    return build_opener(NoRedirect()).open(request, timeout=timeout)


def api_get(path, params, product, credentials=None):
    """Shared authenticated GET; called only by explicit live mode."""
    client_id, secret = credentials or (os.environ.get("PCO_APP_ID"), os.environ.get("PCO_SECRET"))
    if not client_id or not secret:
        raise InputError("live mode needs PCO_APP_ID and PCO_SECRET environment variables")
    validate_api_path(path, product)
    url = f"{API_BASE}{path}?{urlencode(params)}"
    token = base64.b64encode(f"{client_id}:{secret}".encode()).decode()
    global _last_request, GET_REQUESTS
    if _last_request is not None:
        time.sleep(max(0, REQUEST_DELAY - (time.monotonic() - _last_request)))
    _last_request = time.monotonic()
    GET_REQUESTS += 1
    try:
        request = Request(url, headers={"Authorization": f"Basic {token}", "Accept": "application/json", "User-Agent": "pco-seat-audit local account audit"}, method="GET")
        with urlopen(request, timeout=20) as response:
            payload = validate_json_depth(json.loads(response.read().decode("utf-8")))
    except (HTTPError, URLError, OSError, HTTPException, UnicodeError, json.JSONDecodeError, RecursionError, ValueError) as exc:
        raise InputError(f"{product}: API request failed ({getattr(exc, 'code', 'connection or response error')})") from None
    return payload


ROOT_FIELDS = {
    "services": ("people_allowed", "people_remaining", "file_storage_size", "file_storage_size_used"),
    "check_ins": ("daily_check_ins",),
}
ROOT_PATHS = {"services": "/services/v2/", "check_ins": "/check-ins/v2/"}


def account_capacity(product, payload):
    data = payload.get("data") if isinstance(payload, dict) else None
    attrs = data.get("attributes") if isinstance(data, dict) and data.get("type") == "Organization" else None
    if not isinstance(attrs, dict):
        raise InputError(f"{product}: expected a JSON:API Organization with attributes")
    values = {}
    for field in ROOT_FIELDS[product]:
        value = attrs.get(field)
        if value is not None and (not isinstance(value, int) or isinstance(value, bool) or value < 0):
            raise InputError(f"{product}: {field} must be a non-negative integer")
        values[field] = value
    if product == "services":
        allowed, remaining = values["people_allowed"], values["people_remaining"]
        if allowed is not None and remaining is not None:
            if remaining > allowed:
                raise InputError("services: people_remaining exceeds people_allowed")
            values["seats_used"] = allowed - remaining
    return values


def services_capacity(payload):
    return account_capacity("services", payload)


def setup_credentials():
    print("Create a Personal Access Token at https://api.planningcenteronline.com/oauth/applications")
    print("Use Personal Access Tokens. It has your full access level; this tool sends GET only.")
    print("Revoke it by deleting the token on that same developer-account page.")
    values = []
    for name, label in (("PCO_APP_ID", "Application id (hidden): "), ("PCO_SECRET", "Secret (hidden): ")):
        value = os.environ.get(name)
        if not value:
            if not sys.stdin.isatty():
                raise InputError("setup needs PCO_APP_ID and PCO_SECRET, or an interactive terminal for hidden input")
            try:
                with warnings.catch_warnings():
                    warnings.simplefilter("error", getpass.GetPassWarning)
                    value = getpass.getpass(label)
            except (EOFError, getpass.GetPassWarning):
                raise InputError("hidden input unavailable; set PCO_APP_ID and PCO_SECRET") from None
        if not value:
            raise InputError("both Personal Access Token values are required")
        values.append(value)
    print("Token accepted for this process only; nothing is stored. Add --live to perform the audit.")
    return tuple(values)


def get_usage_rows(product, spec, usage_data, days, now, live, credentials=None):
    if live:
        rows = api_records(product, spec, days, now, credentials)
    else:
        rows = usage_data.get(product)
        if not isinstance(rows, list):
            raise InputError(f"usage input is missing product rows for {product}")
    date_field = spec.get("api", {}).get("date_field", "occurred_at")
    filtered = UsageRows([], getattr(rows, "incomplete_reason", None), getattr(rows, "exact_count", None))
    cutoff = window_start(now, days)
    for row in rows:
        if not isinstance(row, dict):
            raise InputError(f"{product}: every usage row must be an object")
        attrs = row.get("attributes", row)
        if not isinstance(attrs, dict):
            raise InputError(f"{product}: row attributes must be an object")
        if "relationships" in row:
            relationships = row["relationships"]
            if not isinstance(relationships, dict) or any(not isinstance(value, dict) for value in relationships.values()):
                raise InputError(f"{product}: usage row relationships must contain objects")
        if date_field not in attrs:
            raise InputError(f"{product}: row has missing or malformed {date_field}")
        stamp = parse_time(attrs.get(date_field))
        if stamp is None:
            if attrs.get(date_field) is None and spec.get("api", {}).get("null_date_is_unused") is True:
                continue
            raise InputError(f"{product}: row has missing or malformed {date_field}")
        if cutoff <= stamp <= now:
            filtered.append(row)
    return filtered


def measure(rows, spec):
    metric = spec.get("metric")
    if not isinstance(metric, dict) or not isinstance(metric.get("type"), str) or metric.get("type") not in {"records", "distinct", "account_seats"}:
        raise InputError("each product needs a metric type of records or distinct")
    if metric["type"] == "records":
        return rows.exact_count if isinstance(rows, UsageRows) and rows.exact_count is not None else len(rows)
    key = metric.get("field")
    if not isinstance(key, str) or not key:
        raise InputError("distinct metrics need a non-empty metric.field")
    values = set()
    for row in rows:
        attrs = row.get("attributes", row)
        value = attrs.get(key)
        if value is None:
            relation_name = key[:-3] if key.endswith("_id") else key
            relationships = row.get("relationships", {})
            if not isinstance(relationships, dict):
                raise InputError("usage row relationships must be an object")
            relation = relationships.get(relation_name, {})
            if not isinstance(relation, dict):
                raise InputError("usage row person relationship must be an object")
            rel = relation.get("data")
            value = rel.get("id") if isinstance(rel, dict) else None
        if value is not None:
            if not isinstance(value, (str, int, float)) or isinstance(value, bool):
                raise InputError("usage row distinct identifier must be a string or number")
            values.add(str(value))
        elif isinstance(rows, UsageRows):
            rows.incomplete_reason = "Some activity has no person identifier; distinct usage is incomplete. No downgrade or saving can be assessed."
    return len(values)


def validate_plan(plan):
    if not isinstance(plan, dict) or not isinstance(plan.get("products"), list) or not plan["products"]:
        raise InputError("plan must contain a non-empty products list")
    if "organization" in plan and (not isinstance(plan["organization"], str) or not plan["organization"].strip()):
        raise InputError("plan organization must be a non-empty string")
    seen = set()
    for product in plan["products"]:
        if not isinstance(product, dict):
            raise InputError("each plan product must be an object")
        name = product.get("product")
        if not isinstance(name, str) or name not in KNOWN_PRODUCTS:
            raise InputError("unknown product: use a supported product name string")
        if name in seen:
            raise InputError(f"duplicate product: {name}")
        seen.add(name)
        if "api" in product:
            app = product["api"]
            if not isinstance(app, dict):
                raise InputError(f"{name}: api must be an object")
            if "path" in app:
                validate_api_path(app["path"], name)
            if "date_field" in app and (not isinstance(app["date_field"], str) or not app["date_field"]):
                raise InputError(f"{name}: api.date_field must be a non-empty string")
        if "price_ladder" in product:
            ladder = product["price_ladder"]
            if not isinstance(ladder, list):
                raise InputError(f"{name}: price_ladder must be a list of tier objects")
            for row in ladder:
                if (not isinstance(row, dict) or not isinstance(row.get("tier"), str)
                        or not row["tier"].strip() or "limit" not in row
                        or (row["limit"] is not None and (not finite_number(row["limit"]) or row["limit"] <= 0))
                        or not finite_number(row.get("monthly_price")) or row["monthly_price"] < 0):
                    raise InputError(f"{name}: price_ladder tiers need a name, a positive limit (or null), and a non-negative monthly_price")
        if product.get("not_assessed_reason") and "metric" not in product:
            continue
        for field in ("paid", "tier", "limit", "unit", "metric"):
            if field not in product:
                raise InputError(f"{name}: missing {field}", product=name, field=field)
        if not isinstance(product["tier"], str) or not product["tier"].strip():
            raise InputError(f"{name}: tier must be a non-empty string")
        if not isinstance(product["paid"], bool):
            raise InputError(f"{name}: paid must be true or false")
        if product["limit"] is None and product.get("capacity_unknown") is True:
            pass
        elif not finite_number(product["limit"]) or product["limit"] <= 0:
            raise InputError(f"{name}: limit must be a positive number")
        if not isinstance(product["unit"], str) or not product["unit"].strip():
            raise InputError(f"{name}: unit must be a non-empty string")
        metric = product["metric"]
        if not isinstance(metric, dict) or not isinstance(metric.get("type"), str) or metric.get("type") not in {"records", "distinct", "account_seats"}:
            raise InputError(f"{name}: metric type must be records, distinct, or account_seats (Services only)")
        if metric["type"] == "account_seats" and name != "services":
            raise InputError(f"{name}: account_seats is only supported for Services")
        if metric["type"] == "distinct" and (not isinstance(metric.get("field"), str) or not metric["field"]):
            raise InputError(f"{name}: distinct metric needs a field")
        for price_key in ("monthly_price", "lower_monthly_price"):
            value = product.get(price_key)
            if value is not None and (not finite_number(value) or value < 0):
                raise InputError(f"{name}: {price_key} must be zero or greater")
        lower = product.get("lower_tier")
        if lower is not None:
            if not isinstance(lower, dict) or not finite_number(lower.get("limit")) or lower["limit"] <= 0:
                raise InputError(f"{name}: lower_tier must contain a positive limit")
            if not isinstance(lower.get("tier"), str) or not lower["tier"].strip():
                raise InputError(f"{name}: lower_tier needs a non-empty tier name")
            if product["limit"] is not None and lower["limit"] >= product["limit"]:
                raise InputError(f"{name}: lower_tier.limit must be smaller than current limit")


def audit(plan, usage_data, days, live=False, now=None, account_roots=None, credentials=None):
    validate_plan(plan)
    if not live and not isinstance(usage_data, dict):
        raise InputError("usage must be an object keyed by product names")
    now = now or datetime.now(timezone.utc)
    results = []
    for original in plan["products"]:
        spec = dict(original)
        name = spec["product"]
        if spec.get("not_assessed_reason") and "metric" not in spec:
            results.append({"product": name, "finding": "NOT ASSESSED", "reason": spec["not_assessed_reason"]})
            continue
        if not spec["paid"] and spec["limit"] is None and spec["tier"] == "Free":
            results.append({"product": name, "finding": "NOT PAID",
                            "reason": "Free subscription; capacity and usage not measured."})
            continue
        incomplete_reason = None
        capacity = None
        if name in ROOT_FIELDS and (live or (account_roots and name in account_roots)):
            payload = api_get(ROOT_PATHS[name], {"fields[Organization]": ",".join(ROOT_FIELDS[name])}, name, credentials) if live else account_roots[name]
            capacity = account_capacity(name, payload)
            field = "people_allowed" if name == "services" else "daily_check_ins"
            if capacity[field] is not None and capacity[field] > 0:
                spec["limit"] = capacity[field]
        if name == "services" and capacity and "seats_used" in capacity:
            used = capacity["seats_used"]
            spec["unit"] = "Team Members (current seats, not look-back activity)"
        elif spec["metric"]["type"] == "account_seats":
            if live or (account_roots is not None and "services" in account_roots):
                raise InputError("services: account response did not supply people_allowed and people_remaining; check account access or supply a manual activity metric",
                                 source=None if live else "roots")
            raise InputError("services: current seats need --live or --account-roots; supply a manual activity metric otherwise")
        else:
            try:
                rows = get_usage_rows(name, spec, usage_data, days, now, live, credentials)
                used = measure(rows, spec)
            except InputError as exc:
                if not live:
                    exc.source = "usage"
                raise
            incomplete_reason = rows.incomplete_reason
        result = {"product": name, "paid": spec["paid"], "tier": spec["tier"], "used": used,
                  "limit": spec["limit"], "unit": spec["unit"], "monthly_price": spec.get("monthly_price")}
        if name == 'giving' and spec['metric']['type'] == 'records':
            result['comparison_note'] = f"Giving comparison: {days}-day total against a monthly donation allowance; this is not a monthly usage figure. Review each billing month's donations before changing plans."
        elif name == 'check_ins' and spec['metric']['type'] == 'distinct':
            result['comparison_note'] = f"Check-Ins comparison: distinct people across {days} days against a daily check-in allowance; this is not a daily usage figure. Review daily use before changing plans."
        elif name == 'services' and not (capacity and 'seats_used' in capacity):
            result['comparison_note'] = f"Services comparison: {days}-day activity count in the manual plan's unit ({spec['unit']}); this does not measure occupied seats or seat/licence usage."
        if capacity is not None and original["limit"] != spec["limit"]:
            result["tier"] = f"API capacity {spec['limit']}"
            result["capacity_difference"] = f"Pasted/manual tier {original['tier']} has limit {original['limit']}; API limit {spec['limit']} is used. Confirm the pasted price against current billing."
        if incomplete_reason:
            result["finding"] = "NOT ASSESSED"
            result["incomplete_reason"] = incomplete_reason
        elif spec["paid"] and used == 0:
            result["finding"] = "PAID BUT UNUSED"
        elif spec["paid"] and spec["limit"] is None:
            result["finding"] = ("USAGE RECORDED — Unlimited tier; compare cheaper tiers and their features manually"
                                 if spec["tier"].lower().startswith("unlimited") else
                                 "USAGE RECORDED — named Publishing tier; compare features manually"
                                 if name == "publishing" and spec["tier"].lower() in {"essential", "plus", "premium"}
                                 else "USAGE RECORDED — limit not found — enter it")
        else:
            result["finding"] = "OK" if spec["paid"] else "NOT PAID"
        if spec["paid"] and not incomplete_reason:
            current = spec.get("monthly_price")
            options = []
            if spec.get("lower_tier"):
                options.append(dict(spec["lower_tier"], monthly_price=spec.get("lower_monthly_price")))
            else:
                options = spec.get("price_ladder", [])
            cheaper = [row for row in options if row["limit"] is not None
                       and (spec["limit"] is None or row["limit"] < spec["limit"])
                       and current is not None and row.get("monthly_price") is not None
                       and row["monthly_price"] < current]
            fitting = [row for row in cheaper if used <= row["limit"]]
            if fitting:
                lower = min(fitting, key=lambda row: row["monthly_price"])
                result["recommendation"] = f"Consider {lower['tier']} ({lower['limit']} {spec['unit']})"
                result["possible_monthly_saving"] = round(current - lower["monthly_price"], 2)
                if used > 0:
                    result["finding"] = "LOW USAGE" if spec["limit"] is not None and used < spec["limit"] / 2 else "LOWER TIER FITS"
            elif cheaper:
                lower = max(cheaper, key=lambda row: row["limit"])
                result["finding"] = "CURRENT TIER NEEDED"
                result["next_tier_requirement"] = (f"The next tier down, {lower['tier']}, cannot hold current use. "
                    f"Free {used - lower['limit']} {spec['unit']} to reach it; "
                    f"that would save {current - lower['monthly_price']:.2f} per month (confirm current billing).")
        if capacity is not None:
            result["account_capacity"] = capacity
            result["account_capacity_source"] = "GET " + ROOT_PATHS[name] if live else "offline API-root fixture " + ROOT_PATHS[name]
        if "ladder_source" in spec:
            result["ladder_source"] = spec["ladder_source"]
        results.append(result)
    return results


def format_report(results, days, organization, plan_source="supplied plan", usage_source="supplied usage"):
    lines = [f"Planning Center usage audit — {organization}", f"Look-back window: {days} days", f"Plan source: {plan_source}", f"Usage source: {usage_source}", "", "For account administrators. Recommendations only; no account changes are made.", ""]
    for item in results:
        if "reason" in item:
            lines.append(f"{item['product'].replace('_', ' ').title()}: {item['finding']} — {item['reason']}")
            continue
        if item.get("incomplete_reason"):
            amount = "usage count incomplete"
        elif item['limit'] is None:
            no_limit = (item['tier'].lower().startswith('unlimited') or
                        (item['product'] == 'publishing' and item['tier'].lower() in {'essential', 'plus', 'premium'}))
            amount = f"{item['used']} {item['unit']}; " + ('no numeric limit for this tier' if no_limit else 'numeric limit not supplied')
        else:
            amount = f"{item['used']} / {item['limit']} {item['unit']}"
        line = f"{item['product'].replace('_', ' ').title()}: {item['finding']} — {amount} (paid tier: {item['tier']})"
        if item.get("recommendation"):
            line += f"; {item['recommendation']}"
        if item.get("possible_monthly_saving") is not None:
            line += f"; possible saving {item['possible_monthly_saving']:.2f} per month (confirm current billing)"
        lines.append(line)
        if item.get('comparison_note'):
            lines.append('  ' + item['comparison_note'])
        if item.get("next_tier_requirement"):
            lines.append("  " + item["next_tier_requirement"])
        if item.get("incomplete_reason"):
            lines.append("  " + item["incomplete_reason"])
        if item.get("capacity_difference"):
            lines.append("  " + item["capacity_difference"])
        if "account_capacity" in item:
            values = item["account_capacity"]
            source = item["account_capacity_source"]
            lines.append(f"  Account plan capacity ({source}): " + "; ".join(
                f"{key}={value if value is not None else 'not found — enter it'}" for key, value in values.items()))
            if item['product'] == 'services':
                storage_used = values.get('file_storage_size_used')
                storage_allowed = values.get('file_storage_size')
                lines.append(f"  Services storage: {storage_used if storage_used is not None else 'not found'} used / {storage_allowed if storage_allowed is not None else 'not found'} allowed (raw account values; unit unknown to this tool). Confirm storage in Planning Center before changing plans.")
                if 'seats_used' in values:
                    lines.append("  Services seats are a current snapshot of occupied seats, not look-back activity.")
            lines.append("  API-root limits take priority. Paid status, tier and price still need subscription text or manual confirmation.")
        if "ladder_source" in item:
            lines.append(f"  Price ladder: {item['ladder_source']} (monthly USD, before tax); confirm current prices and storage before changing plans.")
    lines.extend(["", "Unofficial tool; not affiliated with or endorsed by Planning Center.", "Plan and price values come from pasted subscriptions or manual corrections; available API-root limits take priority. Confirm all values against current billing."])
    return "\n".join(lines) + "\n"


IMPORT_METRICS = {
    "check_ins": {"unit": "unique people checked in (paid capacity: Daily Check-Ins)", "metric": {"type": "distinct", "field": "person_id"}, "api": {"path": "/check-ins/v2/check_ins", "date_field": "created_at"}},
    "registrations": {"unit": "attendee records", "metric": {"type": "records"}, "api": {"path": "/registrations/v2/attendees", "date_field": "created_at"}},
    "giving": {"unit": "donation records", "metric": {"type": "records"}, "api": {"path": "/giving/v2/donations", "date_field": "created_at"}},
    "services": {"unit": "Team Members", "metric": {"type": "account_seats"}},
    "publishing": {"unit": "published episodes", "metric": {"type": "records"}, "api": {"path": "/publishing/v2/episodes", "date_field": "published_to_library_at", "null_date_is_unused": True}},
}


def import_plan(imported, base):
    defaults = {"products": []}
    for entry in imported["products"]:
        spec = dict(IMPORT_METRICS.get(entry["product"], {}))
        spec.update(entry)
        if entry["product"] not in IMPORT_METRICS:
            spec["not_assessed_reason"] = "Billing imported, usage not measured: no built-in per-unit usage comparison for this product (including free plans)."
        if "limit" not in spec:
            spec.update(limit=None, capacity_unknown=True)
        if spec.get("paid") is False and "tier" not in spec:
            spec["tier"] = "Free"
        defaults["products"].append(spec)
    return merge_plan(defaults, base)


def ladder_review(plan):
    info = plan["price_ladder"]
    return (f"Price ladder: built-in {info['default_date']}; pasted products: {', '.join(info['pasted_products']) or 'none'}; "
            f"pasted date: {info['pasted_date']}. Monthly USD before tax; confirm current prices. "
            "A pasted product replaces that product's whole default ladder; other products retain the dated default.")


def same_file(out, inputs):
    if not out:
        return False
    out_path = Path(out).resolve()
    for path in inputs:
        if not path:
            continue
        if out_path == Path(path).resolve():
            return True
        try:
            if os.path.samefile(out, path):
                return True
        except OSError:
            pass
    return False


def main(argv=None):
    global GET_REQUESTS
    GET_REQUESTS = 0
    argv = list(sys.argv[1:] if argv is None else argv)
    fetch = bool(argv and argv[0] == "fetch")
    if fetch:
        argv.pop(0)
    setup = bool(argv and argv[0] == "setup")
    if setup:
        argv.pop(0)
    parser = argparse.ArgumentParser(description="Compare paid Planning Center products with recent usage.")
    parser.add_argument("--plan", help="your package and metric file; required unless importing Subscriptions")
    parser.add_argument("--subscriptions", "--billing-file", dest="billing_file", help="pasted UTF-8 Subscriptions page text (saved HTML also accepted)")
    parser.add_argument("--change-plans", help="pasted Change plans page text to refresh the dated default price ladder")
    parser.add_argument("--ladder-date", help="date of pasted price ladder, YYYY-MM-DD")
    parser.add_argument("--import-only", action="store_true", help="review billing extraction and write a partial plan JSON; no usage or API calls")
    parser.add_argument("--account-roots", help="offline JSON object with Services and Check-Ins JSON:API root responses")
    parser.add_argument("--usage", help="offline JSON:API-shaped usage file; required unless --live or --import-only")
    parser.add_argument("--days", type=int, default=90, choices=sorted(ALLOWED_DAYS))
    parser.add_argument("--as-of", help="ISO 8601 report reference time; defaults to the current UTC time")
    parser.add_argument("--output", default="report.txt", help="report destination (default: report.txt)")
    parser.add_argument("--live", action="store_true", help="fetch supported activity via read-only API GET requests")
    args = parser.parse_args(argv)
    if fetch:
        args.live = True
    review = ""
    try:
        if fetch and (not args.billing_file or not args.change_plans):
            raise InputError("fetch needs --subscriptions and --change-plans saved text files")
        if args.live and args.usage:
            raise InputError("live mode cannot be combined with --usage")
        credentials = setup_credentials() if setup else None
        if setup and not args.live:
            return 0
        if args.ladder_date and (parse_time(args.ladder_date) is None or len(args.ladder_date) != 10):
            raise InputError("--ladder-date must be YYYY-MM-DD")
        if args.import_only and (not args.billing_file or args.live):
            raise InputError("--import-only needs --billing-file and cannot be combined with --live")
        if args.live and args.account_roots:
            raise InputError("--account-roots is offline only")
        if not args.import_only and not args.live and not args.usage:
            raise InputError("supply --usage your-usage.json or --live to read account usage")
        if not args.plan and not args.billing_file:
            raise InputError("supply --plan your-plan.json or --subscriptions saved-subscriptions.txt; example plans are never used automatically")
        plan_path = args.plan
        if same_file(args.output, [plan_path, args.billing_file, args.change_plans, args.account_roots, None if args.live else args.usage]):
            raise InputError("output path must differ from every input path")
        base = read_json(plan_path) if plan_path else {"products": []}
        if not isinstance(base, dict) or not isinstance(base.get("products"), list):
            raise InputError(f"{plan_path}: plan must contain a products list")
        plan = base
        plan_source = "; ".join(value for value in (f"plan file {plan_path}" if plan_path else None,
                                  f"pasted Subscriptions file {args.billing_file}" if args.billing_file else None,
                                  f"account capacity file {args.account_roots}" if args.account_roots else None) if value)
        usage_source = "live API (GET only)" if args.live else f"usage file {args.usage}"
        review = ""
        if args.billing_file:
            imported = extract_billing(read_text(args.billing_file), subscriptions=True)
            if not imported["products"]:
                raise InputError("billing file contains no recognized product sections; save UTF-8 billing text or enter a plan file")
            try:
                plan = import_plan(imported, base)
                review = format_review(imported, base)
            except InputError as exc:
                raise InputError(f"{plan_path or args.billing_file}: {exc}") from None
            if args.import_only:
                pasted = parse_ladder(read_text(args.change_plans)) if args.change_plans else None
                plan = apply_ladder(plan, pasted, args.ladder_date)
                plan["import_review"] = imported
                Path(args.output).write_text(json.dumps(plan, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
                print(review, end="")
                print(ladder_review(plan))
                print(f"Partial plan written to {args.output}; supply missing values and metrics before auditing.")
                return 0
        try:
            validate_plan(plan)
        except InputError as exc:
            origin = plan_path or args.billing_file
            if args.billing_file and plan_path:
                overrides = next((row for row in base['products'] if isinstance(row, dict) and row.get('product') == exc.product), {})
                origin = (plan_path if exc.field in overrides else args.billing_file) if exc.field else f"combined plan (Subscriptions {args.billing_file}; overrides {plan_path})"
            raise InputError(f"{origin}: {exc}") from None
        usage = {} if args.live else read_json(args.usage)
        if not isinstance(usage, dict):
            raise InputError(f"{args.usage}: usage must be an object keyed by product names")
        reference = parse_time(args.as_of) if args.as_of else datetime.now(timezone.utc)
        if reference is None:
            raise InputError("--as-of must be a valid ISO 8601 date or timestamp")
        window_start(reference, args.days)
        roots = read_json(args.account_roots) if args.account_roots else None
        if args.account_roots:
            if not isinstance(roots, dict):
                raise InputError(f"{args.account_roots}: account-roots must be an object keyed by services and check_ins")
            for product, payload in roots.items():
                if product not in ROOT_FIELDS:
                    raise InputError(f"{args.account_roots}: unsupported account-roots product")
                try:
                    account_capacity(product, payload)
                except InputError as exc:
                    raise InputError(f"{args.account_roots}: {exc}") from None
        if args.billing_file or args.change_plans:
            pasted = parse_ladder(read_text(args.change_plans)) if args.change_plans else None
            plan = apply_ladder(plan, pasted, args.ladder_date)
        try:
            results = audit(plan, usage, args.days, live=args.live, now=reference, account_roots=roots, credentials=credentials)
        except InputError as exc:
            if args.live or exc.source is None:
                raise
            origin = args.usage if exc.source == 'usage' else args.account_roots
            raise InputError(f"{origin}: {exc}") from None
        if "price_ladder" in plan:
            review += ladder_review(plan) + "\n"
        synthetic = (same_file(plan_path, ["examples/plan.json"]) and not args.billing_file and not args.live
                     and same_file(args.usage, ["examples/usage.json"]) and not args.account_roots and not args.change_plans)
        organization = "Synthetic sample organization" if synthetic else plan.get("organization", "Account usage review")
        if not synthetic and organization == "Synthetic sample organization":
            organization = "Account usage review"
        report = format_report(results, args.days, organization, plan_source, usage_source) + review
        report += f"Report reference time: {reference.isoformat()}\n"
        if args.live:
            report += f"API requests this process: {GET_REQUESTS}; methods: GET only.\n"
        try:
            Path(args.output).write_text(report, encoding="utf-8")
        except OSError as exc:
            raise InputError(f"cannot write {args.output}: {exc.strerror or 'I/O error'}") from None
        print(report, end="")
        return 0
    except OSError as exc:
        print(f"error: cannot write {args.output}: {exc.strerror or 'I/O error'}", file=sys.stderr)
        return 2
    except InputError as exc:
        if review:
            print("Billing import review only; audit failed and no complete report was written.")
            print(review, end="")
        print("error: " + " ".join(str(exc).splitlines()), file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())
