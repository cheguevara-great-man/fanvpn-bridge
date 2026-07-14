# Integration tests

These tests run the local HTTP gateway against an in-process fake extension.
They cover transparent bytes, large bodies, concurrency, cancellation,
disconnects and backpressure without contacting real providers.
