# Why this exists

subhadipmitra@: A pointer, not a squat.

npm has no way to reserve a name: you claim one by publishing to it. The
unscoped `rotascale` was free while `@rotascale/sdk` was already ours, which is
the arrangement where somebody else registers the short name and every
`npm install rotascale` typo lands on their code.

npm's policy is against name-squatting, so this is deliberately a real package
that does one honest thing — it tells a reader where the SDK actually is. It
carries no code, declares no dependencies, and nothing depends on it.

Publish it from the account that owns the `@rotascale` scope, which is
`rotascale-subhadip`, NOT `subhadip-rotalabs`.
