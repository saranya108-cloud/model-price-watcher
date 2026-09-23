# model-price-watcher

Model Price Watcher is an open-source pricing-intelligence tool for people who already know which LLMs they want to use. It helps them find the best current price for those models, including discounts, free routes, promotions, batch rates, cached-input rates, and time-dependent rates.

LLM pricing changes constantly. Providers introduce temporary discounts, free routes, batch pricing, time-dependent rates, and promotions that can be easy to miss.

Model Price Watcher answers a practical question:

> Where can I run the models I already use at the best price right now?

You choose the model. Model Price Watcher helps you find the best available price for it.

It does not evaluate model quality, choose or recommend models, route inference, or automatically switch models or providers.

## What it watches

### Implemented library support

The Python library supports OpenRouter catalog history and a separate Cheaper
Inference **public Standard advertised-price** history. Exact offering IDs remain
distinct, including case and suffixes. There is no automatic model selection,
alias matching, routing, or switching. The broader features and commands below
are plans, not a delivered command-line interface.

Cheaper Inference results describe observed advertised base token prices. They
do not guarantee a final request charge, availability, savings, or free execution.
Standard prices are not compared with ZDR prices. A true ZDR flag without an
explicit route retains the row as evidence-only; a ZDR route rejects the whole
Standard catalog. Media, unknown conditions, and unsupported rows retain their
presence without inventing numeric token quotes. Unknown prices are not zero.

Use `providers.cheaper_inference.parse_catalog` for supplied JSON,
`cheaper_inference_acquisition.scan_cheaper_inference` for one explicitly invoked
keyless request, and `advertised_queries.view_selected_advertised_offerings` for
selected exact IDs. The scanner uses only the fixed public catalog URL and an
injected transport/clock. It has no credentials, retries, pagination, scheduler,
or inference calls. Failed scans and remote empty catalogs do not replace history;
an explicitly written successful offline empty catalog is an absence barrier.

```python
from datetime import datetime, timezone
from model_price_watcher.storage import open_database
from model_price_watcher.advertised_queries import view_selected_advertised_offerings

connection = open_database("history.sqlite")
try:
    report = view_selected_advertised_offerings(
        connection, offering_ids=["your-exact-catalog-id"],
        now=datetime.now(timezone.utc),
    )
    for lookup in report.lookups:
        if lookup.current_observation is not None:
            quote = lookup.current_observation.advertised_quote
            if quote is not None:
                print("Observed advertised input price:", quote.input_usd_per_million)
finally:
    connection.close()
```

`recent_observed_advertised_decrease_ids` reports decreases observed less than
seven days ago with both components known, unchanged billing basis, and neither
component increasing. Even a decrease smaller than 5% counts here. Repeated
unchanged observations do not refresh that window. Absence, unknown components,
changed basis, unsupported quotes, or time discontinuity clear the event.
An expired unchanged event remains available for historical explanation.

`zero_advertised_base_token_rate_ids` means “Zero advertised base token rates;
other charges or conditions may apply.” These quotes never enter legacy deal or
zero-token lists. An absent selected ID means “Not observed in the latest public
Standard catalog.” A changed basis means “Price basis changed; no comparable
decrease calculated.” Public source metadata declares interpretation and does
not authenticate supplied offline JSON or establish upstream freshness.

### Database compatibility

New empty databases initialize at schema version 2. Version-1 databases require
the explicit choice `open_database(path, migrate_v1=True)`. This transaction adds
the nullable advertised-quote column and preserves legacy records; it does not
reinterpret OpenRouter prices. Version-1 software cannot read the resulting
version-2 database. Backup creation and downgrade are not provided here.

Only the exact supported schema is accepted. Custom columns, constraints,
indexes, triggers, views, and SQLite `ANALYZE` statistics are refused, with no
automatic repair. Invalid stored evidence fails before selected-ID filtering.
If rollback fails, the original exception retains the rollback failure as
context; discard and reopen the indeterminate connection rather than reuse it.

Migration tests use disposable fixtures. Existing user data is not migrated as
part of installation or validation. Run the offline suite from the repository:

```sh
PYTHONDONTWRITEBYTECODE=1 PYTHONPATH=src python3 -m unittest discover -s tests -v
```

### Planned coverage

Model Price Watcher is intended to track:

- Standard input and output token pricing
- Cached-input pricing
- Free model routes
- Temporary provider promotions
- Percentage discounts
- Batch pricing
- Time-of-use pricing
- Provider-specific specials
- Price increases and decreases
- Promotion start and expiration dates when available

The goal is to make changing inference prices visible for models users have already selected.

## Example

Instead of displaying only:

```text
Model A
Input: $2.00 / 1M tokens
Output: $10.00 / 1M tokens
```

Model Price Watcher should eventually be able to report:

```text
🔥 MODEL DEAL
Model A
Provider: Example Provider
Input:  $2.00 / 1M tokens
Output: $10.00 / 1M tokens
Discount: 50%
Previous observed price:
Input:  $4.00 / 1M
Output: $20.00 / 1M
First observed: 2026-09-18
Promotion expiration: Unknown
```

For time-dependent pricing:

```text
⏰ OFF-PEAK PRICING
Model B
Provider: Example Provider
Off-peak pricing begins in 35 minutes.
Current output price: $1.00 / 1M
Off-peak output price: $0.50 / 1M
Potential savings: 50%
```

## Initial scope

Version 0.1 will intentionally be small.

The first release is planned to include:

- OpenRouter pricing support
- Detection of free and discounted routes
- Normalized input/output pricing
- Local price-history storage
- Meaningful price-change detection
- A simple current-deals view
- Current-price lookup by model name

Additional providers can be added through independent provider adapters.

## Planned provider support

Potential providers include:

- OpenRouter
- DeepSeek
- Cheaper Inference
- NanoGPT
- Nous Research
- Other inference providers with publicly accessible pricing

Provider support will be added incrementally.

## Commands

The initial interface is expected to include commands such as:

```text
/deals
```

Find notable current deals, including discounts and free routes. This command surfaces pricing opportunities; it does not recommend models based on quality or capability.

```text
/price <model>
```

Look up current pricing for a model across supported providers.

Future versions may include:

```text
/watch <model>
```

Monitor a selected model for meaningful future price changes.

```text
/compare <model>
```

Compare current pricing for the same model across supported providers.

## Local-first design

Model Price Watcher is intended to be local-first.

Where practical:

- Price history is stored locally.
- No Model Price Watcher account is required.
- No telemetry is required.
- No user API keys are uploaded to Model Price Watcher.
- Public pricing sources are preferred.
- Provider adapters remain modular and replaceable.

The initial local datastore is expected to use SQLite.

## Hermes integration

Model Price Watcher is being designed with Hermes Agent integration in mind.

A Hermes plugin could expose tools such as:

```python
model_deals()
```

or:

```python
model_price("model-name")
```

This would allow an agent to ask questions such as:

Where is the model I use cheapest right now?

Is a model I use currently available through a free route?

Is this model discounted on another provider?

Would waiting for an off-peak window reduce the cost of this batch job?

Model Price Watcher reports pricing opportunities only. It does not:

- Choose a model
- Rank models by quality or intelligence
- Recommend a "best model"
- Automatically switch models or providers
- Route inference
- Change account settings
- Initiate paid inference

## Longer-term ideas

Possible future features include:

- Telegram or other notification alerts
- Historical price charts
- User-defined discount thresholds
- Promotion-expiration tracking
- Route capability metadata
- Route compatibility filters
- Context-window filtering
- Batch-price comparison
- Effective cost estimates for selected models
- Observed cost-per-successful-task for selected models
- Provider reliability history
- Scheduled price scans
- Cross-provider model aliases
- Web dashboard

Eventually, Model Price Watcher could combine price information with observed agent performance for models users have already selected.

That would help users distinguish between:

> the lowest price per token for a selected model

and

> the lowest effective cost when that model reliably completes the job.

This would provide cost context, not model-quality rankings or recommendations.

## Data accuracy

Model prices can change without notice. For each observed price, Model Price Watcher should record:

- The source of each price
- When that price was observed
- Whether a price appears standard, promotional, free, batch-only, or time-dependent

Users should verify provider pricing before making significant purchasing or routing decisions.

Model Price Watcher is an independent project and is not affiliated with, endorsed by, or operated by any model provider or inference platform.

## Contributing

Contributions will be welcome as the project develops.

Useful contributions may include:

- New provider adapters
- Pricing-source improvements
- Tests
- Model-name normalization
- Documentation
- Promotion detection
- Price-history tools

Provider adapters should remain isolated so that changes to one provider do not affect the rest of the system.

## License

MIT License.

See [LICENSE](LICENSE) for details.

---

Model Price Watcher

Because the best price for the model you already chose can change before you notice.
