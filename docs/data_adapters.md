# Real-data adapters

The repository includes ingestion adapters for two documented external formats:

- Tardis normalized `incremental_book_L2` plus optional `trades` CSV files.
- A paired LOBSTER `message` and finite-level `orderbook` CSV file.

The adapters do not download data and the repository does not redistribute
vendor samples. Pass local paths to files obtained under your own access terms.
The CSVs under `tests/fixtures/adapters` are fictional, hand-authored test data.

Official format references:

- [Tardis downloadable CSV schemas](https://docs.tardis.dev/downloadable-csv-data-types)
- [Tardis order-book reconstruction guidance](https://docs.tardis.dev/faq/data)
- [LOBSTER output structure](https://data.lobsterdata.com/info/DataStructure.php)
- [LOBSTER sample-file page](https://data.lobsterdata.com/info/DataSamples.php)

## Tardis

```python
from lobmm.data import TardisCSVAdapter, TardisData, write_parquet
from lobmm.validation import validate_frame

adapter = TardisCSVAdapter(
    tick_size="0.50",
    quantity_multiplier=1000,
    timestamp_source="local_timestamp",
    trade_match_window_ns=1_000_000,
)
events = adapter.checked(
    TardisData(
        book=r"D:\market-data\incremental_book_L2.csv.gz",
        trades=r"D:\market-data\trades.csv.gz",
    )
)
validate_frame(events)
write_parquet(events, "data/processed/session.parquet")
print(dict(adapter.last_diagnostics))
```

Tardis `amount` is the new absolute quantity at a price, not an increment.
The adapter maintains a book per resting side and emits:

- `ADD` for an increase from the preceding absolute amount;
- `CANCEL` for an otherwise unexplained decrease;
- `RESET` followed by `SNAPSHOT` levels whenever `is_snapshot` changes from
  false to true;
- no event for a repeated amount or a zero-size snapshot level.

Rows before the first snapshot are discarded, as required by the Tardis
reconstruction guidance. A file with no snapshot is rejected.

Trade files require extra care. Emitting both an absolute book decrease and
the corresponding print as independent reductions would consume displayed
depth twice. Instead, the adapter matches a print to observed decreasing
volume at the same price and resting side within `trade_match_window_ns`.
Matched volume is labeled `TRADE`; residual decreasing volume is `CANCEL`.
Unmatched print volume is counted in diagnostics and is not inserted into the
executable-depth stream. A taker `buy` maps to resting `ASK`; a taker `sell`
maps to resting `BID`; `unknown` aggressor side is skipped.

`local_timestamp` is the safe default because Tardis defines it as arrival
time and uses it to order downloadable ranges. Selecting exchange `timestamp`
is supported, but a regression is rejected rather than silently sorted.
Canonical trade timestamps use the supporting book-change timestamp because
that is when the displayed reduction is observable in this stream.

`quantity_multiplier` converts fractional venue amounts to exact integer
simulator units. For example, a multiplier of `1000` maps `0.001` to one unit.
Any amount that is not exactly representable is rejected.

### Tardis limitations

- Trade/book reconciliation is a conservative offline attribution heuristic,
  not exchange message linkage. Feed-channel skew, aggregated updates, hidden
  liquidity, liquidations, and off-book prints can leave trades unmatched.
- An overly large matching window can misclassify cancellations as trades.
  Report results across plausible windows, including zero.
- The adapter accepts one exchange/symbol pair per conversion and rejects mixed
  instruments.
- Prices must align exactly to the configured tick size. Venue-specific
  contract multipliers and inverse-contract economics remain caller concerns.

## LOBSTER

```python
from lobmm.data import LOBSTERCSVAdapter, LOBSTERData
from lobmm.validation import validate_frame

adapter = LOBSTERCSVAdapter(
    tick_size="0.01",
    trading_date="2012-06-21",
    levels=10,
)
events = adapter.checked(
    LOBSTERData(
        messages=r"D:\market-data\AAPL_2012-06-21_34200000_57600000_message_10.csv",
        orderbook=r"D:\market-data\AAPL_2012-06-21_34200000_57600000_orderbook_10.csv",
    )
)
validate_frame(events)
```

LOBSTER files are headerless and paired row-for-row. Message time is seconds
after local midnight; the adapter requires a trading date and defaults to
`America/New_York`, then converts the exact decimal time to epoch nanoseconds.
LOBSTER prices are divided by 10,000 before exact tick conversion.

The order-book row contains state after its matching message. Consequently the
first available row is represented as `RESET` plus `SNAPSHOT` levels; its
causing message is not emitted a second time. Later mappings are:

| LOBSTER event | Canonical treatment |
|---|---|
| 1 submission | `ADD` when visible in the requested depth |
| 2 partial cancellation | `CANCEL` when visible |
| 3 deletion | `CANCEL` when visible |
| 4 visible execution | `TRADE` on the resting order's direction |
| 5 hidden execution | not emitted as displayed-depth consumption |
| 6 cross/auction trade | not emitted as displayed-depth consumption |
| 7 halt/resume | clear on halt; reset/snapshot on first resume indicator |

Direction in LOBSTER already identifies the resting limit order:
`+1` is `BID` and `-1` is `ASK`. This differs from datasets where trade side
means aggressor.

Each paired order-book row is treated as the source of truth. After preserving
the visible causal message, residual changes are emitted as `ADD`/`CANCEL`.
This matters at finite depth: when a new best price enters a two-level file,
the old second level can leave the visible window even though that order was
not cancelled at the exchange. Diagnostics call these events window
reconciliation, and they must not be interpreted as true cancellation flow.

### LOBSTER limitations

- LOBSTER levels are occupied prices, not fixed tick offsets.
- Only the requested depth is observable. Events outside it cannot be
  reconstructed, and window-boundary deltas do not preserve order identity.
- Hidden executions and auction/cross prints cannot support Level 2 queue fills
  and are intentionally excluded from `TRADE`.
- Trading-halt rows duplicate the prior displayed book. The adapter clears the
  canonical book during a halt and restores the paired state on resume; it
  does not infer auction state.
- Corporate actions, symbol metadata, and session selection are not encoded in
  the paired files. Record them separately.

## Provenance checklist

For every converted session, retain a sidecar manifest outside the raw-data
license boundary with:

- provider, venue, symbol, session date, requested depth, and timezone;
- source filenames plus cryptographic checksums;
- adapter class and project commit;
- tick size, quantity multiplier, timestamp selection, and trade-match window;
- adapter diagnostics and stream-validation result;
- the applicable vendor license or access reference.

Never describe historical replay results as live performance. Queue position
remains estimated from aggregated depth, and a finite-depth adapter cannot
recover individual order priority.
