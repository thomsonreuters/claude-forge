# Implementation Checklist

## Cost Tracking and Spend Caps

- [x] Record proxy request costs to `~/.forge/costs/requests/*.jsonl`
- [x] Record Forge verb cost deltas to `~/.forge/costs/verbs/*.jsonl`
- [x] Add `forge proxy costs` for verb and model cost views
- [x] Surface live proxy cost totals in metrics/status data
- [x] Bootstrap cap enforcement from request logs after proxy restart
- [x] Support daily and monthly caps in proxy-owned `proxy.yaml`
- [x] Support `post` and `strict` cap modes
- [x] Support `reject` and `warn` cap actions
- [x] Add regression tests for config load, cap coercion, rollover, strict mode, and warn mode
- [x] Document proxy cost config, log locations, and HTTP 429 contract

## Subprocess Proxy

- [x] Add `SessionIntent.subprocess_proxy`
- [x] Add `forge session start --subprocess-proxy <proxy_id>`
- [x] Set `FORGE_SUBPROCESS_PROXY` for direct-mode sessions that route child jobs through a proxy
- [x] Guard subprocess runs when the configured proxy cannot be resolved
- [x] Inherit subprocess proxy intent through resume, fork, and relaunch children
- [x] Document why `--subprocess-proxy` and `--proxy` are mutually exclusive

## Follow-ups

- [ ] Consider a one-time test patch refactor away from `forge.cli.session` wildcard compatibility exports
- [ ] Add debug timing around multi-proxy metrics snapshots if users report slow status panels
- [ ] Decide whether paid E2E tests should carry the `paid` pytest marker in addition to `integration` and `slow`
