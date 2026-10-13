"""Conservative, layout-independent import of administrator-saved billing text."""

import copy
import json
import re
from html.parser import HTMLParser

PRODUCTS = {
    "check_ins": r"(?:check[\s-]*ins?|Child & Volunteer Check[\s-]*ins?)",
    "registrations": r"(?:registrations|Event Signups)",
    "giving": r"(?:giving|Donation Management)",
    "services": r"(?:services|Service Planning & Scheduling)",
    "publishing": r"(?:publishing|Web & App Content Management)",
    "people": r"(?:people|Membership Management)",
    "calendar": r"(?:calendar|Events & Facility Management)",
    "groups": r"(?:groups|Groups Management)",
}
BOUNDARIES = re.compile(r"^(?:product\s*:\s*)?(" + "|".join(PRODUCTS.values()) + r"|music stand)\b", re.I)
PLAN_UNITS = {"services": "Team Members", "check_ins": "Daily Check-Ins", "giving": "Monthly Donations",
              "registrations": "Attendees", "calendar": "Rooms", "groups": "Group Members"}
NUMBER = r"(?:\d{1,3}(?:,\d{3})+|\d+)(?![\d,])"
FIELDS = ("paid", "tier", "limit", "storage", "price_shown", "monthly_price")


def section_boundary(line):
    """Separate short unknown headings from ordinary text inside a billing block."""
    if re.fullmatch(r"(?:active|inactive|subscribed|not subscribed|not paid|no subscription|change plans?|manage plan|billed monthly|billing period.*)", line, re.I):
        return False
    if re.fullmatch(r"(?:product\s*:.*|.*\bproduct|(?:total|invoice|balance|tax)(?:\s.*)?)", line, re.I):
        return True
    # Plain copied text has no heading tags. A short title with no recognised
    # billing terms is treated conservatively as a new, unsupported section.
    words = line.split()
    return (len(words) <= 4 and all(word[:1].isupper() for word in words)
            and not re.search(r"[$€£]|\b(?:USD|CAD|AUD|EUR|GBP|paid|plan|package|tier|size|limit|capacity|price|status|free|essential|plus|premium|unlimited|storage|subscription)\b|^\d", line, re.I))


def normalize_dashes(text):
    """Accept typographic hyphens in copied headings and plan units."""
    return text.translate(str.maketrans({char: "-" for char in "‐‑‒–—−﹘﹣－"}))


class TextLines(HTMLParser):
    """Keep visible text and physical source line numbers; never load linked content."""

    BLOCKS = {"p", "div", "section", "article", "tr", "li", "br", "hr", "h1", "h2", "h3", "h4", "pre"}

    def __init__(self, structured=False):
        super().__init__(convert_charrefs=True)
        self.lines = []
        self.parts = []
        self.ignored = 0
        self.structured = structured
        self.preformatted = False

    def flush(self):
        text = " ".join(part[1] for part in self.parts).strip()
        if text:
            self.lines.append({"line": self.parts[0][0], "end_line": self.parts[-1][0], "text": text})
        self.parts = []

    def handle_starttag(self, tag, attrs):
        if tag in {"script", "style", "title"}:
            self.ignored += 1
        if tag == "pre":
            self.preformatted = not self.preformatted
        if tag in self.BLOCKS:
            self.flush()
        if tag in {"td", "th"} and self.parts:
            self.parts.append((self.getpos()[0], "|"))

    def handle_endtag(self, tag):
        if tag in {"script", "style", "title"}:
            self.ignored = max(0, self.ignored - 1)
        if tag == "pre":
            self.preformatted = not self.preformatted
        if tag in self.BLOCKS:
            self.flush()

    def handle_data(self, data):
        if self.ignored:
            return
        for offset, part in enumerate(data.splitlines()):
            if offset and (not self.structured or self.preformatted):
                self.flush()
            part = " ".join(part.split())
            if part:
                self.parts.append((self.getpos()[0] + offset, part))


def extract_billing(text, subscriptions=False):
    """Return partial plan products and review candidates; infer no absent values."""
    text = normalize_dashes(text.lstrip("\ufeff"))
    structured = bool(re.search(r"</?(?:html|body|div|p|section|article|table|tr|td|th|h[1-6]|li|ul|br|pre|script|style)\b[^>]*>", text, re.I))
    parser = TextLines(structured=structured)
    if structured:
        parser.feed(text)
        parser.close()
        parser.flush()
    else:
        parser.lines = [{"line": index, "end_line": index, "text": " ".join(line.split())}
                        for index, line in enumerate(text.splitlines(), 1) if line.strip()]
    candidates = {name: {field: [] for field in FIELDS} for name in PRODUCTS}
    found = set()
    unsupported = set()
    current = None
    distance = 0
    plan_complete = False
    for source in parser.lines:
        line = source["text"]
        # Simple table rows may put a label and value in adjacent cells.
        line = re.sub(r"\b(paid|plan|package|tier|size|limit|capacity|price)\s*\|\s*", r"\1: ", line, flags=re.I)
        # Reject navigation before registering any product.
        if sum(bool(re.search(r"\b(?:" + pattern + r")\b", line, re.I)) for pattern in PRODUCTS.values()) > 1:
            current = None
            continue
        heading = BOUNDARIES.match(line)
        if heading:
            label = heading[1]
            current = next((name for name, pattern in PRODUCTS.items()
                            if re.fullmatch(pattern, label, re.I)), None)
            if current:
                found.add(current)
            else:
                unsupported.add(label)
            distance = 0
            plan_complete = False
        else:
            distance += 1
            # A complete price/plan block can only have one optional storage line.
            # Explanatory copy and a page-level status belong outside this block.
            if subscriptions and plan_complete and not re.fullmatch(r"\d+(?:\.\d+)?\s*(?:MB|GB|TB) Storage", line, re.I):
                current = None
            # An unknown section or total must not lend its price to the last product.
            if section_boundary(line):
                current = None
        if current is None or distance > 6:
            continue
        # Do not choose an offered alternative, total, trial, or promotional price.
        if re.search(r"\b(lower|upgrade|downgrade|trial|discount|total|tax|invoice|balance)\b", line, re.I):
            current = None
            continue

        def add(field, value):
            candidates[current][field].append({"value": value, "source": source})

        for match in re.finditer(r"\bpaid\s*:\s*(yes|no|true|false)\b", line, re.I):
            add("paid", match[1].lower() in {"yes", "true"})
        if re.search(r"\b(not paid|not subscribed|no subscription|free (?:plan|tier)|(?:status|plan|tier)\s*:\s*free)\b", line, re.I):
            add("paid", False)
        if re.search(r"\b(?:paid subscription|status\s*:\s*paid)\b", line, re.I):
            add("paid", True)
        for match in re.finditer(r"\b(?:plan|package|tier)(?: name)?\s*:\s*([^;|]+)", line, re.I):
            value = match[1].strip()
            if value and not re.search(r"[$€£]|\b(?:limit|size|price|paid)\s*:", value, re.I):
                add("tier", value)
        for match in re.finditer(r"\b(?:package size|size|limit|capacity)\s*:\s*(" + NUMBER + r")(?![\d.,])\b", line, re.I):
            value = int(match[1].replace(",", ""))
            if value > 0 and not re.search(r"\d[\d,]*\s*(?:to|[-–/])\s*\d", line, re.I):
                add("limit", value)
        if subscriptions:
            # Observed Subscriptions rows carry a complete plan line below the price.
            size = re.fullmatch(r"(" + NUMBER + r")\s+(" + "|".join([*PLAN_UNITS.values(), "Room"]) + r")", line, re.I)
            if size and current in PLAN_UNITS and size[2].lower().rstrip("s") == PLAN_UNITS[current].lower().rstrip("s"):
                add("tier", line)
                plan_complete = True
                if int(size[1].replace(",", "")) > 0:
                    add("limit", int(size[1].replace(",", "")))
            if re.fullmatch(r"Free", line, re.I):
                add("paid", False)
            if re.fullmatch(r"(?:Essential|Plus|Premium|Unlimited(?: .+)?)", line, re.I):
                add("tier", line)
                plan_complete = True
            if re.fullmatch(r"\d+(?:\.\d+)?\s*(?:MB|GB|TB) Storage", line, re.I):
                add("storage", line)
                plan_complete = False
                # End the block after storage, without accepting a trailing status.
                distance = 6
        monthly = bool(re.search(r"(?:/\s*(?:mo(?:nth)?)(?:\b)|per\s+month\b|\bmonthly\b)", line, re.I))
        bare_subscription_price = subscriptions and bool(re.fullmatch(r"(?:USD|[$])\s*" + NUMBER + r"(?:\.\d{2})?(?:\s*/\s*mo(?:nth)?)?", line, re.I))
        annual = bool(re.search(r"(?:/\s*(?:yr|year)\b|per\s+year\b|\bannual(?:ly)?\b)", line, re.I))
        for match in re.finditer(r"(?<![-\d])(?P<currency>USD|CAD|AUD|EUR|GBP|[$€£])\s*(?P<amount>" + NUMBER + r"(?:\.\d{2})?)(?![\d.,])", line, re.I):
            amount = float(match["amount"].replace(",", ""))
            price = {"amount": amount, "currency": match["currency"].upper(),
                     "period": "month" if (monthly or bare_subscription_price) and not annual else "not found"}
            add("price_shown", price)
            if (monthly or bare_subscription_price) and not annual:
                add("monthly_price", amount)
            if bare_subscription_price:
                add("paid", amount > 0)

    products = []
    review = {}
    for name, fields in candidates.items():
        entry = {"product": name}
        review[name] = {}
        for field, matches in fields.items():
            unique = {json.dumps(m["value"], sort_keys=True) for m in matches}
            confident = len(unique) == 1
            review[name][field] = {"value": matches[0]["value"] if confident else None,
                                  "candidates": matches,
                                  "status": "extracted" if confident else "not found — enter it"}
            if confident and field != "price_shown":
                entry[field] = matches[0]["value"]
        # Different currencies/periods must never collapse into one numeric monthly price.
        if review[name]["price_shown"]["value"] is None:
            entry.pop("monthly_price", None)
            review[name]["monthly_price"]["value"] = None
            review[name]["monthly_price"]["status"] = "not found — enter it"
        if review[name]["price_shown"]["value"] is not None:
            entry["currency"] = review[name]["price_shown"]["value"]["currency"]
        if name != "services":
            review[name]["storage"]["status"] = "not applicable"
        tier = entry.get("tier", "").lower()
        if "limit" not in entry and (tier.startswith("unlimited") or
                (name == "publishing" and tier in {"essential", "plus", "premium"})):
            review[name]["limit"]["status"] = "not applicable — no numeric limit for this tier"
        if name in found:
            products.append(entry)
    return {"products": products, "review": review, "unsupported": sorted(unsupported),
            "interpretation": "Subscriptions shape observed 2026-10-13: bare USD/$ prices are monthly, and positive/zero prices mean paid/not paid. Confirm discounts and tax." if subscriptions else "Only explicitly labelled values used."}


def merge_plan(imported, base):
    """Explicit plan entries override imported fields; retain every detected product."""
    from .cli import InputError

    if not isinstance(base, dict) or not isinstance(base.get("products"), list):
        raise InputError("plan must contain a products list")
    products = {entry["product"]: copy.deepcopy(entry) for entry in imported["products"]}
    seen = set()
    for entry in base["products"]:
        if not isinstance(entry, dict) or not isinstance(entry.get("product"), str):
            raise InputError("each plan product must be an object with a product key")
        name = entry["product"]
        if name in seen:
            raise InputError(f"duplicate product: {name}")
        seen.add(name)
        products.setdefault(name, {"product": name}).update(copy.deepcopy(entry))
    result = copy.deepcopy(base)
    result["products"] = list(products.values())
    return result


def format_review(imported, base):
    lines = ["Saved billing import review (physical source lines; HTML text normalized).",
             "Review the extracted price, plan and optional storage before changing a subscription.",
             imported["interpretation"]]
    overrides = {entry["product"]: entry for entry in base["products"]}
    for name, fields in imported["review"].items():
        lines.append(f"{name}:")
        for field, item in fields.items():
            value = json.dumps(item["value"], ensure_ascii=False) if item["status"] == "extracted" else item["status"]
            lines.append(f"  {field}: {value}")
            for candidate in item["candidates"]:
                source = candidate["source"]
                span = str(source["line"])
                if source["end_line"] != source["line"]:
                    span += f"-{source['end_line']}"
                lines.append(f"    source line {span}: {source['text']}")
            if field in overrides.get(name, {}):
                lines.append(f"    plan file override: {json.dumps(overrides[name][field], ensure_ascii=False)}")
    if imported["unsupported"]:
        lines.append("Outside this audit's product scope: " + ", ".join(imported["unsupported"]))
    lines.append("Supply missing required billing values in a correction plan file. Optional storage and tiers without numeric limits need manual review; no missing paid status or price is guessed.")
    return "\n".join(lines) + "\n"
