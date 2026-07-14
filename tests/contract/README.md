# Contract tests

Contract behavior is covered by the protocol unit tests and checked against
`contracts/native-messaging-v1.schema.json`. The checks include serialized
message size, chunk ordering, cumulative acknowledgements and version
negotiation.

Contract tests must not require Chrome, FanVPN or API credentials.
