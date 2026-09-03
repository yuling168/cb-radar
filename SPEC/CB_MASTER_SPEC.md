# CB Master Data Specification

## Scope

`master_collector.py` collects official CB issuance, current balance, put date, current conversion price, conversion-price history and monthly balance history. It does not calculate strategy signals, conversion value, premium rate or moving averages.

## Official Sources

1. TPEx OpenAPI `https://www.tpex.org.tw/openapi/v1/bond_ISSBD5_data`
   - Active TWD CB universe and issuance fields.
2. TPEx structured list `https://www.tpex.org.tw/www/zh-tw/bond/convSearch`
   - Official MOPS detail URL for each bond.
3. MOPS `https://mopsov.twse.com.tw/mops/web/t120sg01?...`
   - Official issue units, filed conversion price/effective date and month-end outstanding balance.
   - Historical `monyr_reg=YYYYMM` pages are scanned backward to preserve official monthly balances and distinct conversion-price events.
4. MOPS `https://mopsov.twse.com.tw/mops/web/ajax_t108sb08_1`
   - Official conversion-price change announcements, including CB code, adjusted price and formal effective date.
   - The current price is the newest official event whose effective date is not later than the Collector's Asia/Taipei run date.
5. TPEx `https://www.tpex.org.tw/www/zh-tw/bond/convDelist`
   - Official recent delisting list and formal delisting date for ordinary delistings.
6. MOPS `https://mopsov.twse.com.tw/mops/web/ajax_t05st01`
   - For a notice explicitly exercising the issuer's redemption right, the detail
     field `轉換公司債收回基準日` establishes the lifecycle date and reason `已贖回`.

No third-party website is an operational data source.

## Selection Rules

- `BondType = 5`: domestic convertible bond.
- `ListingStatus = 2`: currently listed.
- `Currency = 1`: New Taiwan dollar.
- The active universe is evaluated at the Asia/Taipei run date as
  `issue_date <= run_date AND (delisting_date IS NULL OR delisting_date > run_date)`.
  A bond announced to delist in the future therefore remains active until that
  formal date. For a MOPS-confirmed forced redemption, `delisting_date` is the
  contractual `轉換公司債收回基準日`, not TPEx's following termination-trading date.
- Delisted CBs are retained as historical rows. They and their price-event and
  monthly-balance history are never deleted merely because of delisting.
- Normally the Collector uses the MOPS detail link supplied by TPEx `convSearch`.
  If TPEx active issue data confirms a current CB but `convSearch` temporarily
  omits its link, the Collector may build the official parameterized MOPS query
  as a fallback. The fallback must pass the same complete-field and cross-source
  validation; a missing link never relaxes data-quality requirements.
- The MOPS official Chinese bond name must identify the instrument as `轉換公司債`.
  Official names identifying `交換公司債` are excluded and any previously stored
  master/history rows for those codes are removed transactionally.
- `--codes` may restrict a run to named active CBs for validation; omitting it processes all active TWD CBs found in the official sources.

## Validation Rules

- All required TPEx fields must exist.
- Dates are validated and normalized to `YYYY-MM-DD`.
- Amounts are integer New Taiwan dollars; issue amount must be positive and balance may be zero.
- `cb_master.issue_amount` is the official issued face principal, consistent
  with `issue_units * 發行面額` and suitable as the denominator for remaining
  ratio. It normally uses MOPS `申請發行總額`; when the official `發行張數` and
  face value exactly identify a lower, partially issued MOPS `實際發行總額`, that
  verified issued principal is used instead. TPEx `IssueAmount` is
  cross-validated against MOPS `實際發行總額`. Issuance-premium proceeds never
  replace the verified face principal in `cb_master`.
- MOPS supplies the official `發行面額`, `實際發行總額` and `發行張數`.
  Some monthly pages use the `發行張數` display for the current outstanding
  units rather than the original issue count. Therefore `issue_units` is
  normalized per bond as `實際發行總額 / 發行面額`; both amounts must divide
  exactly, and the displayed MOPS count must equal either the calculated
  original units or that month's `balance_amount / 發行面額`. Any other value
  fails validation. No fixed par value is assumed.
- No `balance_units` column is stored. Displayed balance units are calculated from
  each bond's official par value (`issue_amount / issue_units`) and
  `balance_amount / par_value`; non-integral official values fail validation.
- TPEx `Guaranteed=1` with a nonblank guarantee description maps to `is_secured=1`; `Guaranteed=2` with a blank description maps to `0`; other codes map to `NULL`.
- Conversion prices are positive numeric values.
- MOPS issue date, maturity date and actual issue amount must equal TPEx values.
- `cb_master.balance_amount` and `cb_master.balance_date` always come from the
  same TPEx issue-data row: `OutstandingAmount` and `Date`, respectively. MOPS
  monthly balances never replace or date the latest master state, even when a
  MOPS amount equals the TPEx amount. The Collector never substitutes its run
  date for the official TPEx `Date`.
- MOPS `monyr_reg` identifies a reporting month, not a verified as-of date.
  Only a reporting month whose calendar month-end is strictly before the
  Collector run date may be stored in `cb_monthly_balance` or used as a MOPS
  month-end balance. An unfinished reporting month is never projected to its
  future month-end. A TPEx `Date` after the run date is invalid and aborts
  collection.
- TPEx's official issue date and conversion price at issuance directly create
  the initial conversion-price event.
- MOPS monthly filings and official price-change announcements provide later
  events. The earliest available MOPS month may already contain an adjusted
  price and is not required to equal the issuance price.
- If MOPS monthly filings assign different prices to the same effective date,
  the ambiguous monthly event is not selected or stored. Every conflicting
  price must be resolved by the TPEx initial event or a MOPS official
  price-change announcement; otherwise collection fails.
- Any HTTP, JSON, HTML, required-field or cross-source validation failure aborts before SQLite is opened; missing values are never guessed or changed to zero.

## Tables

### `cb_master`

One current or historical row per CB. `put_date` is the first investor put date reported by TPEx, or `NULL` when TPEx reports no put right. `issue_units` is the official number of NT$100,000-par units. `is_secured` is 1, 0 or `NULL` for secured, unsecured or officially indeterminate. `balance_amount` is the newest verified official balance and `balance_date` is its official as-of date. `current_conversion_price` is selected from all verified monthly and announcement events by effective date.

`current_conversion_price_effective_date` stores the `effective_date` of the same
event selected for `current_conversion_price`. The two fields are updated together.

`balance_amount` remains the official amount in New Taiwan dollars. User-facing
reports display a derived whole-unit count instead of that amount. Remaining
ratio continues to use `balance_amount / issue_amount`; neither value is replaced
by the display-only unit count.

User-facing labels and formatting:

- `issue_amount / 100000000` → `發行總額（億元）`, without forcing an integer.
- derived balance units → `餘額張數`.
- `balance_date` → `餘額日期`, normalized to `YYYY-MM-DD`; `NULL` is blank in the UI.
- `current_conversion_price_effective_date` → `轉換價格生效日`.
- `is_secured` 1 / 0 / `NULL` → `擔保` 有 / 無 / 未知.
- `delisting_date` → `下市日期`, normalized to `YYYY-MM-DD`; `NULL` is blank in the UI.
  For `已贖回`, this is MOPS `轉換公司債收回基準日`; otherwise it is TPEx's official
  delisting date.
- `delisting_reason` → `下市原因`; it is `NULL`, `提前贖回`, `到期`, or `已下市`.
  `NULL` is blank in the UI (never an empty string or a display value such as
  「未知」). `已下市` is the fallback only after official delisting is known and
  no reliable official reason is available.

Lifecycle synchronization is append-only for confirmed history: a later TPEx
recent-delisting response that no longer contains an older bond must not clear
its stored `delisting_date` or `delisting_reason`. A more-specific official
reason may replace the fallback `已下市`.

### `conversion_price_events`

Primary key: `(cb_code, effective_date)`. Each distinct official effective price is retained. A historical price for date D is:

```sql
SELECT conversion_price
FROM conversion_price_events
WHERE cb_code = :cb_code
  AND effective_date <= :date
ORDER BY effective_date DESC
LIMIT 1;
```

### `cb_monthly_balance`

Primary key: `(cb_code, year_month)`. Each completed, verified MOPS reporting
month is retained as `YYYY-MM`; reruns update that month rather than creating
duplicates. The Collector never stores the run date's unfinished month.

All three tables retain `source`, `source_url` and `collected_at` for auditability.

## CLI

All active TWD CBs:

```bash
python master_collector.py
```

Selected active CBs:

```bash
python master_collector.py --codes 11011,12561
```

An alternate SQLite path may be supplied with `--database`.

## Data-timing rule

The monthly filing can lag an already effective price-change announcement. Monthly snapshots remain the balance-history source, while the official CB announcement table supplies newer price events. Future-dated announcements may be stored as events but are never selected as `cb_master.current_conversion_price` before their effective date.
