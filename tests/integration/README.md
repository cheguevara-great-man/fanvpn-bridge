# Integration tests

This directory will run the local HTTP gateway against an in-process fake
extension. It will cover transparent bytes, SSE, large bodies, concurrency,
cancellation, disconnects and backpressure without contacting real providers.
