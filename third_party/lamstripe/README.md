# lamstripe

`lamstripe` is an opinionated third-party Lammergeier package for Stripe's REST API.
It uses Lam stdlib HTTP, JSON, env, and Result primitives, with raw Go only for
standard-library URL form encoding.

The package exposes both low-level `rawGet` / `rawPostForm` escape hatches and
higher-level option classes for the most common payment flows.

## Configuration

```bash
export STRIPE_SECRET_KEY="sk_test_..."
export STRIPE_BASE_URL="https://api.stripe.com/v1"
export STRIPE_API_VERSION="2024-06-20"
```

`STRIPE_BASE_URL` is optional and mainly useful for tests or Stripe-compatible
mock servers. `STRIPE_API_VERSION` is optional.

## Quick start

```lammergeier
from lamstripe import StripeClient, StripePaymentIntentOptions
from lamerrors import Result

func main() {
    stripe: StripeClient = StripeClient.fromEnv()
    stripe.setIdempotencyKey("order_123")

    opts: StripePaymentIntentOptions = StripePaymentIntentOptions(1200, "USD")
    opts.description = "Lam order"
    opts.receiptEmail = "buyer@example.com"
    opts.setMetadata("order_id", "ord_123")

    res: Result = stripe.createPaymentIntentWithOptions(opts)
    if res.ok() {
        print("created")
    } else {
        print(str(res.error))
    }
}
```

## Opinionated helpers

- `StripeCustomerOptions` builds customer creation fields with metadata.
- `StripePaymentIntentOptions` defaults `automatic_payment_methods[enabled]` to `true`, normalizes currency to lowercase, and supports customer, receipt email, capture method, description, and metadata.
- `StripeCheckoutSessionOptions` requires a price ID, quantity, success URL, and cancel URL, with optional customer email, client reference ID, promotion codes, and metadata.
- `Stripe.validateAmount` and `Stripe.validateCurrency` return `Result` validation failures before making HTTP calls.
- `StripeResponse.id()`, `StripeResponse.url()`, and `StripeResponse.clientSecret()` are convenience accessors for common response fields.
- `StripeClient.setIdempotencyKey(key)` sets the Stripe idempotency header for subsequent writes.

## API reference

### `StripeConfig`

- `StripeConfig.fromEnv()` reads `STRIPE_SECRET_KEY`, optional `STRIPE_BASE_URL`, and optional `STRIPE_API_VERSION`.
- `cfg.isReady()` returns whether a secret key is configured.

### `StripeClient`

- `StripeClient.fromEnv()` creates a client from environment variables.
- `rawGet(path)` returns `Result[StripeResponse]`.
- `rawPostForm(path, fields)` sends `application/x-www-form-urlencoded` data and returns `Result[StripeResponse]`.
- `createCustomer(email, name="")` wraps `POST /customers`.
- `createCustomerWithOptions(opts)` validates and creates a customer.
- `createPaymentIntent(amount, currency, description="")` wraps `POST /payment_intents`.
- `createPaymentIntentWithOptions(opts)` validates and creates a payment intent.
- `retrievePaymentIntent(id)` wraps `GET /payment_intents/{id}`.
- `createCheckoutSession(successUrl, cancelUrl, mode="payment", priceId="", quantity=1)` wraps `POST /checkout/sessions`.
- `createCheckoutSessionWithOptions(opts)` validates and creates a checkout session.

Non-2xx Stripe responses are returned as `Result.Err(Error("StripeApiError", ...))` with the `StripeResponse` as the cause.

## Testing

```bash
/usr/bin/python3 third_party/lamstripe/tests/run_lamstripe_tests.py --verbose
```

Live tests are guarded and require a Stripe test key:

```bash
export LAMSTRIPE_LIVE_TESTS=1
export STRIPE_SECRET_KEY="sk_test_..."
/usr/bin/python3 third_party/lamstripe/tests/run_lamstripe_tests.py --live --verbose
```
