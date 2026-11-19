# Harbor decisions

## D-11 — Two workers

Two workers were selected to bound memory on the demonstration host. Increasing
the queue limit does not create more workers. The queue limit is 24, while the
batch limit of 40 counts records inside an indexing batch, not queued operations.

## D-12 — Read-only input

The demo indexes notes without editing them. `read_only` is enabled and
`external_fetch` is disabled. Scheduled indexing is enabled, but it uses the same
read-only authority as an ordinary indexing request.

## D-13 — One attempt

Automatic retries were removed because a timeout does not prove the provider
failed to receive a request. The 20-second request timeout and 90-second turn
deadline bound different things: an individual request and the overall turn.

## D-14 — Retention

Run records are retained for 14 days. No rule for customer billing, geographic
failover, or payment-card data is defined in this demonstration.
