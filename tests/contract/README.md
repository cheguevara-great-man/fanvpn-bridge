# Contract tests

This directory will validate every Native Messaging envelope against
`contracts/native-messaging-v1.schema.json`, including serialized message size,
chunk ordering, cumulative acknowledgements and version negotiation.

Contract tests must not require Chrome, FanVPN or API credentials.
