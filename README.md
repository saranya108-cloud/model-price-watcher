# model-price-watcher

Model Price Watcher is an open-source tool for tracking LLM inference prices, discounts, free routes, and temporary promotions across model providers.

LLM pricing changes constantly. Providers introduce temporary discounts, free models, batch pricing, time-of-use rates, and promotional routes that can be easy to miss.

Model Price Watcher is designed to answer a simple question:

What good models are unusually cheap or free right now?

Rather than relying on static pricing tables, Model Price Watcher monitors pricing over time so it can identify meaningful changes and surface opportunities when they appear.

What it watches

Model Price Watcher is intended to track:

* Standard input and output token pricing
* Cached-input pricing
* Free model routes
* Temporary provider promotions
* Percentage discounts
* Batch pricing
* Time-of-use pricing
* Provider-specific specials
* Price increases and decreases
* Promotion start and expiration dates when available

The goal is not merely to find the cheapest model.

The goal is to make changing inference economics visible.

Example

Instead of displaying only:

Model A
Input: $2.00 / 1M tokens
Output: $10.00 / 1M tokens

Model Price Watcher should eventually be able to report:

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

For time-dependent pricing:

⏰ OFF-PEAK PRICING
Model B
Provider: Example Provider
Off-peak pricing begins in 35 minutes.
Current output price: $1.00 / 1M
Off-peak output price: $0.50 / 1M
Potential savings: 50%

Initial scope

Version 0.1 will intentionally be small.

The first release is planned to include:

* OpenRouter pricing support
* Detection of free and discounted models
* Normalized input/output pricing
* Local price-history storage
* Price-change detection
* A simple current-deals view
* Lookup by model name

Additional providers can be added through independent provider adapters.

Planned provider support

Potential providers include:

* OpenRouter
* DeepSeek
* Cheaper Inference
* NanoGPT
* Nous Research
* Other inference providers with publicly accessible pricing

Provider support will be added incrementally.

Commands

The initial interface is expected to include commands such as:

/deals

Show notable current model discounts and free routes.

/price <model>

Show known current pricing for a particular model across supported providers.

Future versions may include:

/watch <model>

Track a model and alert when its price changes significantly.

/compare <model>

Compare current pricing for the same or equivalent model across providers.

Local-first design

Model Price Watcher is intended to be local-first.

Where practical:

* Price history is stored locally.
* No Model Price Watcher account is required.
* No telemetry is required.
* No user API keys are uploaded to Model Price Watcher.
* Public pricing sources are preferred.
* Provider adapters remain modular and replaceable.

The initial local datastore is expected to use SQLite.

Hermes integration

Model Price Watcher is being designed with Hermes Agent integration in mind.

A Hermes plugin could expose tools such as:

model_deals()

or:

model_price("model-name")

This would allow an agent to ask questions such as:

Which capable model is unusually cheap right now?

Are any models I use currently free?

Is this model discounted on another provider?

Would waiting for an off-peak window reduce the cost of this batch job?

Model Price Watcher will initially report pricing opportunities only.

It will not automatically change models, providers, account settings, or initiate paid inference.

Automatic routing may be explored separately in the future.

Longer-term ideas

Possible future features include:

* Telegram or other notification alerts
* Historical price charts
* User-defined discount thresholds
* Promotion-expiration tracking
* Model capability metadata
* Tool-use and vision filters
* Context-window filtering
* Batch-price comparison
* Effective task-cost estimates
* Price-per-successful-task metrics
* Provider reliability history
* Scheduled price scans
* Cross-provider model aliases
* Web dashboard

Eventually, Model Price Watcher could combine price information with observed agent performance.

That would make it possible to distinguish between:

the cheapest model per token

and

the cheapest model that reliably completes the job.

Data accuracy

Model prices can change without notice.

Model Price Watcher should record:

* The source of each price
* When that price was observed
* Whether a price appears standard, promotional, free, batch-only, or time-dependent

Users should verify provider pricing before making significant purchasing or routing decisions.

Model Price Watcher is an independent project and is not affiliated with, endorsed by, or operated by any model provider or inference platform.

Contributing

Contributions will be welcome as the project develops.

Useful contributions may include:

* New provider adapters
* Pricing-source improvements
* Tests
* Model-name normalization
* Documentation
* Promotion detection
* Price-history tools

Provider adapters should remain isolated so that changes to one provider do not affect the rest of the system.

License

MIT License.

See LICENSE for details.

⸻

Model Price Watcher

Because the cheapest place to run a model can change before you notice.