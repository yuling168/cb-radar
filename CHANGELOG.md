# Changelog

## Unreleased — Phase 2 CB basic data

### Added

- Official TPEx/MOPS CB master-data collector.
- `cb_master` current master table.
- `conversion_price_events` effective-date history.
- `cb_monthly_balance` monthly history.
- Cross-source validation and auditable source URLs.
- Initial end-to-end verification with five active CBs.
- Direct MOPS issue-unit parsing and TPEx structured guarantee status.
- MOPS CB price-change announcements reconciled by effective date so a stale
  monthly filing cannot override a newer effective conversion price.
- Current conversion-price effective date and display formatting for issue
  amount, outstanding units and guarantee status.
- Exchangeable bonds excluded using the official MOPS Chinese bond name.

### Not included

- No strategy, moving average, conversion-value or notification features.

## 2026-09-03 — Daily announcement history and redemption lifecycle

- Daily workflow: Phase 1 → announcement collector → Phase 2 → Phase 3 → validation → Dashboard → single commit/push, at weekdays 19:30 Asia/Taipei.
- Added idempotent TWSE／TPEx official announcement history, raw snapshots and failure audit records.
- Added one-time MOPS historical-detail backfill table; it is not a daily dependency.
- Forced redemption lifecycle now uses saved official announcement bodies and the contractual redemption baseline date.
- 15601 historical backfill completed: `2026-09-02 / 已贖回`.
- Important commits: `064df8d`, `04eb459`, `af2db43`, `fb70e0b`, `b55bce8`, `afc5d8f`, `96aec6b`.
- GitHub Actions `CB Daily Collector #9` completed end-to-end on the `96aec6b` workflow in about 12 minutes 51 seconds; next operational check is the scheduled 19:30 run.

## 2026-08-29 — Phase 1 completed

### Completed

- Daily CB Collector using official TPEx data.
- SQLite historical accumulation in `data/cb_history.db`.
- GitHub Actions daily automation.
- Automatic DB and Dashboard commit/push when tracked outputs change.
- Static Dashboard and GitHub Pages deployment.
- Date filtering.
- CB name/code search.
- Table sorting.
- Mobile responsive base layout.

### Known limitation

- The mobile CB detail table may still require horizontal scrolling inside the table area.

### Do not implement yet

- Mobile card／stacked-row layout.
- Phase 2 features, including CB basic data, underlying-stock prices, derived indicators, strategies, notifications and AI analysis.
