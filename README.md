# PCO Seat Audit

This small tool compares your church's Planning Center plans with usage and writes a report for an administrator. It suggests a cheaper tier only when the measured use fits. It never changes an account or subscription.

This is an unofficial tool, not affiliated with or endorsed by Planning Center. Planning Center's name belongs to its owner.

You need **Python 3.10 or later**; there are no extra packages to install. Download the tool's ZIP from the repository's Code menu, then Extract it into a folder. Open a terminal in that extracted folder (on Windows, right-click the folder and choose **Open in Terminal**). If needed, type `cd` followed by the folder path in quotes. On Linux and macOS the commands below use `python3`; on Windows use `py -3` in its place. Acceptance tests were run on Linux with Python 3.14; older supported versions, Windows and macOS have not been tested here.

## Quickstart

Try this invented example first. It needs no account, token, network or interactive input. It writes `report.txt` and prints the same report. Its manual metrics count Services **service plans in the past 90 days** and Giving **unique donors**, not occupied Services seats or monthly donation totals:

```sh
python3 -m pco_seat_audit --plan examples/plan.json --usage examples/usage.json --as-of 2026-10-13T12:00:00Z --output report.txt
```

Next, try an invented Subscriptions paste and API-root responses. Here Services measures **occupied seats now**, labelled **Team Members (current seats, not look-back activity)**; Giving counts donation records across the 90-day window:

```sh
python3 -m pco_seat_audit --subscriptions examples/subscriptions-synthetic.txt --account-roots examples/account-roots-synthetic.json --usage examples/usage.json --as-of 2026-10-13T12:00:00Z --output report.txt
```

To try an invented Change plans ladder:

```sh
python3 -m pco_seat_audit --subscriptions examples/subscriptions-synthetic.txt --account-roots examples/account-roots-synthetic.json --change-plans examples/change-plans-synthetic.txt --ladder-date 2026-10-13 --usage examples/usage.json --as-of 2026-10-13T12:00:00Z --output report.txt
```

To review a paste and write an editable partial plan without usage or a token:

```sh
python3 -m pco_seat_audit --subscriptions examples/subscriptions-synthetic.txt --import-only --output report.txt
```

Import-only writes JSON to the file and shows the billing review in the terminal. All examples are synthetic. The additional `examples/*-page-quirks.txt` fixtures reproduce page structure with invented values.

## Start with your saved subscription page

You can review your billing without a token or account connection. Copy **Account settings → Subscriptions** into a UTF-8 plain text file named `saved-subscriptions.txt`. Alternatively, save the page as HTML and use that file with the same option; neither route loads linked pages. Run:

```sh
python3 -m pco_seat_audit --subscriptions saved-subscriptions.txt --import-only --output my-plan.json
```

This shows what was extracted and writes an editable plan file; it does not measure usage. To compare usage offline, you also need a complete usage file and, when Services is included, a saved account capacity file:

```sh
python3 -m pco_seat_audit --subscriptions saved-subscriptions.txt --usage my-usage.json --account-roots my-account-roots.json --output report.txt
```

These JSON files are structured text with field names and values, not ordinary copied page text. The tool does not export them from an account. If you do not already have them, use the account connection below to obtain usage automatically. Every usage audit needs your own `--subscriptions` or `--plan` input; invented plans are never selected automatically.

## Connect your account to obtain usage

1. In Planning Center, open **Account settings → Subscriptions**. Select all the page text, copy it, and paste it into a plain text editor. Save as **UTF-8 plain text**, named `saved-subscriptions.txt`. Copy the whole page, including the headings, prices and plans; no cleanup is needed.
2. On that page, open **Change plans**. Select all, copy and paste into a second UTF-8 plain text file named `saved-change-plans.txt`. Leave the plans unchanged. Keep both files and the report private. The tool reads these files; it does not open a browser.
3. Create a **Personal Access Token** in [your developer account](https://api.planningcenteronline.com/oauth/applications). It supplies an application id and secret. The token carries the **full access of the person who made it**; it cannot be restricted to reading. This tool only reads through GET requests, with no request body. Never put either value in a command argument, source file or report.
4. From the tool's folder, run this command in an interactive terminal. It asks for both token values without displaying them and keeps them only until it finishes. Replace the filenames with paths to your private files if they are elsewhere; use the date you copied Change plans:

```sh
python3 -m pco_seat_audit setup --live --subscriptions saved-subscriptions.txt --change-plans saved-change-plans.txt --ladder-date 2026-10-13 --output report.txt
```

If you already have `PCO_APP_ID` and `PCO_SECRET` set as environment variables (values held by your terminal), the equivalent one-command route is:

```sh
python3 -m pco_seat_audit fetch --subscriptions saved-subscriptions.txt --change-plans saved-change-plans.txt --output report.txt
```

Both routes obtain usage and produce the report; you do not write usage JSON. They use the past 90 days by default. Add `--days 30` or `--days 60` for a different window, `--as-of` for a fixed reference time, and `--ladder-date YYYY-MM-DD` to label when you copied the ladder. A pasted ladder with **date not supplied** has an unknown age; the built-in date does not date your paste. Review the billing extraction and advice before changing any plan yourself.

5. If you set environment values, remove them afterwards. In Bash (Linux/macOS):

```sh
unset PCO_APP_ID PCO_SECRET
```

In Windows PowerShell:

```powershell
Remove-Item Env:PCO_APP_ID, Env:PCO_SECRET -ErrorAction SilentlyContinue
```

Delete the token on the same developer-account page to revoke its access. `setup` alone gives instructions without requesting account data.

The report includes every recognised subscription. Paid products are assessed from complete counts or plainly marked **NOT ASSESSED** with a reason; that does not mean unused. People, Calendar and Groups have no built-in usage measure and always need a manual check. With live or saved seat capacities, Services uses occupied seats now, labelled Team Members. A manual activity metric without seat capacities uses its declared unit instead, as in the first example's service plans; the report explains that this does not measure occupied seats. Imported Giving uses donation records for the requested dates: a 90-day donation total compared with a monthly allowance is not a monthly usage figure. Review each billing month's donations before changing plans. Manual Giving metrics can differ, as in the first example's unique donors. Check-Ins counts distinct people across the window against a daily allowance; this is not daily usage, so check daily use too. Registrations and Publishing read further pages and then select records by date. See Limitations for the meaning of each count.

The account connection uses the API (Planning Center's interface for tools), with GET requests that read data and have no request body. Requests are spaced at least a quarter second apart. Lists arrive in pages of up to 100 records; Giving usually needs just one record and the confirmed date-filtered total. The tool follows further pages on the same Planning Center list, keeps date filters, and stops after 100 pages per collection. An account with more than 100 historical records can still be assessed. If completeness cannot be confirmed, the report omits the incomplete count and all downgrade or saving advice for that product. Bad input or bad responses give a one-line error and do not write a complete report. The token is never stored; only counts from activity reach the report.

A lower tier is suggested only if it costs less and holds current use. If it cannot fit, the report says the current tier is needed and shows how many units would have to be freed to reach the next tier down, with the conditional monthly saving. It does not label that case low usage. Prices and storage still need administrator review.

## Plan information and pasted pages

The account summary response (called an organisation root in the technical reference) supplies Services' allowed and remaining seats, plus storage fields. Occupied seats are allowed minus remaining. Check-Ins supplies its daily capacity. These account capacities take priority over pasted or manual limits; disagreements are explained next to the affected product. Services storage is labelled **used / allowed**, with **unit unknown to this tool**. These raw account values are not converted to MB or GB; check storage in Planning Center before changing plans. Account summaries do not give complete paid status or prices, so the saved pages are needed.

The importer recognises eight products, their longer headings, and typographic hyphens. It tolerates ordinary status, button, description and billing-period lines within six lines of a heading, but stops at a completed plan, a new section or an invoice total. An unknown short heading may need a manual correction. Bare free measured products are listed as NOT PAID without inventing their capacity or querying usage. `--billing-file` is an alias for `--subscriptions`. Bare `$`/`USD` prices are treated as monthly; positive/zero prices mean paid/not paid. Confirm discounts, tax and current billing. Unknown currencies or periods cannot support a guessed USD saving.

If the page cannot supply a value, the billing review says `not found — enter it` and shows the lines it considered. Create a small correction file such as `my-corrections.json`. For example, if Giving's paid status, tier, capacity and price were missing, this is the shape to use (the values below are invented; replace every value with your actual subscription):

```json
{
  "products": [
    {"product": "giving", "paid": true, "tier": "200 Monthly Donations", "limit": 200, "monthly_price": 32}
  ]
}
```

Add `--plan my-corrections.json` alongside `--subscriptions` in the audit command. Include only fields you need to correct. Explicit manual values override the paste, with corrections shown. Missing products are never assumed unpaid. People, Calendar and Groups remain **NOT ASSESSED — billing imported, usage not measured**, including free plans, unless a matching manual metric is supplied. A named Publishing tier has no numeric capacity to enter: compare its features manually. An Unlimited tier likewise has no numeric limit; zero recorded use is shown without a false missing-limit warning.

## Price ladder

A pasted product's ladder replaces that product's complete default ladder. Calendar accepts Room and Rooms; Services storage parentheses are retained. Duplicate tiers and mismatched units fail clearly. Other products retain the built-in general monthly USD ladder dated **2026-10-13**, before tax. This is a reference, not a current quote or an account's paid price. A ladder never supplies a missing subscription price. Confirm storage, features and current prices before making a change.

## Manual inputs and tests

Offline use takes `--usage` and never uses the environment token. A usage JSON has one array per measured product, with resource objects containing `attributes` and ISO 8601 activity dates; `relationships` can supply person identifiers. Missing identifiers make distinct usage incomplete. `--account-roots` takes offline Services and Check-Ins root responses. Imported Services seats need those roots or live mode.

A plan JSON has a `products` list. Measured products need `product`, `paid`, `tier`, a positive `limit`, `unit` and `metric`. Metrics count records or distinct field values; Services also supports `account_seats`. Optional `lower_tier`, `monthly_price` and `lower_monthly_price` specify a cheaper tier manually. Named or Unlimited imported tiers may have unknown numeric capacity.

Run acceptance tests with `python3 -m unittest discover -s tests`. Tests discard inherited token variables and block network connections. Scratch files stay in the ignored `.scratch` folder. The [API review](docs/api-review.md) describes the API choices and the [changelog](CHANGELOG.md) lists changes. Authorised private GET checks verified page parsing and date-filter semantics; no account data is included here.

## Limitations

- This local tool cannot add a screen inside Planning Center. It never changes subscriptions.
- Complete billing needs saved pages or manual corrections. Page layouts may change, so review extracted values.
- Services with live or saved seat capacities measures occupied seats now (Team Members). Manual activity metrics without seat capacities use their declared unit; the first example counts service plans across the window, not seats. Check-Ins compares distinct people across the window with a daily capacity; imported Giving compares donation records across the window with a monthly capacity, while the first manual example counts unique donors. These are conservative window comparisons, not exact billing-period calculations. Registrations counts attendee records rather than the largest event; Publishing counts published episodes.
- People, Calendar and Groups have no built-in usage comparison. Named Publishing tiers cannot be compared by features automatically.
- Pagination is bounded. Incomplete counts cannot support any downgrade or saving advice.
- Suggestions check numeric capacity. Storage, add-ons, currency, billing period and features need manual review; estimates are not quotes.
- Unlimited tiers have no half-limit test. A complete count may still fit a cheaper numeric tier; otherwise compare features manually.
