# Hermes Free Model Verifier ☤

**Manually discover and test accessible free/free-tier AI models for [Hermes Agent](https://github.com/NousResearch/hermes-agent).**

For people who want to explore Hermes on free provider quotas without guessing which listed models actually work. This is a community-maintained, unofficial **early preview (v0.1.0)**, not affiliated with Nous Research or the providers.

## Why it exists

A model appearing in a provider catalog does **not** mean your account can use it. Some return `403`, `404`, `429`, or an empty response. Even a model that generates text may fail on tool/function calls, which an AI agent needs.

This plugin adds **explicit, on-demand tests**:

- **OpenRouter**: reads the account-scoped model listing, selects models marked zero-priced or `:free`, and probes them using your configured key.
- **NVIDIA NIM / Google AI Studio**: can probe configured accounts, but **catalog membership does not establish Free Tier entitlement**. Manual billing acknowledgement is required before any tests.
- **Text test**: one actual completion request.
- **Tool-cycle test**: two actual completion requests; a synthetic `hermes_add(7,5)` function call followed by a supplied `12` result. **No real Hermes tool is executed.**
- **Local saved results**: small, per-credential receipts under Hermes home; no key or raw API response is saved.
- **Hermes `/freeverify` slash command**: reads the saved result without network calls.

Nothing changes `model.default`, fallbacks, cron, service state, or production jobs. There are no scheduled API requests, automated failovers, model rotations, or telemetry.

## Install (after the repository is published)

For Hermes `>=0.21.0`:

```bash
hermes plugins install excuzivedeveloper/hermes-free-model-verifier
hermes plugins enable hermes-free-model-verifier
```

Restart the current Hermes shell or gateway **only when convenient** so its plugin loader sees the new slash/CLI commands; this is not an instruction to restart production services without an operator's approval.

You can also copy the repository directory into `$HERMES_HOME/plugins/hermes-free-model-verifier` and enable it with the plugin manager. This software is **not yet tested against every Hermes release or platform**.

## Usage

Hermes reads credentials through its own profile-aware secret resolver. Configure the provider with Hermes first; **do not paste keys into command arguments**.

```bash
# Offline: see last results; never consumes API quota
hermes freeverify list
hermes freeverify list --json

# Manual scans; default at most five models per invocation
hermes freeverify scan --provider openrouter --mode tools

# Target one OpenRouter free model; explicitly scoped
hermes freeverify scan --provider openrouter --model cohere/north-mini-code:free --mode tools

# No built-in hard cap: ask to scan all available qualifying OpenRouter models
# WARNING: one tool-cycle check uses up to two billable/quota-consuming requests.
hermes freeverify scan --provider openrouter --limit 0 --mode tools

# ONLY after confirming your API key and model are eligible for zero-cost use:
hermes freeverify scan --provider nvidia --model meta/muse-glimmer-30b --ack-unpriced
hermes freeverify scan --provider gemini --model gemini-3.1-flash-lite --ack-unpriced
```

In Telegram/Discord/other supported gateway surfaces:

```text
/freeverify
```

`/freeverify` displays saved results only. The *stock* `/model` picker is unchanged and models are never switched automatically.

### Important: the Telegram `/model` refresh button

Our original local experiment added a **“🔄 Refresh free models” button directly to Telegram `/model`**. That implementation patched internal Hermes modules and was developed against a particular installed revision. **It is not included in this portable v0.1 plugin**, because the supported public plugin API does not currently expose that exact picker callback. Publishing a brittle source monkeypatch as a universal installer would risk breaking people's bots during Hermes updates.

This is the **safe standalone foundation**: manual scanning and reusable verification results. We intend to add native Telegram `/model` picker integration only after a stable upstream extension point is available or an upstream PR is accepted.

## Status interpretation

| Status | Meaning |
|---|---|
| `full_pass` | Model called the synthetic function and processed its result. |
| `text_pass` | Text completion worked; tool support was **not** tested. |
| `no_valid_tool_call` | The test did not get the expected function invocation. Not proof the model can never call tools. |
| `http_403`, `http_404` | Upstream access or routing rejected the request. |
| `http_429` | Provider-side rate/capacity limit. Scanning that provider stops for this run. |
| `network_error` | No conclusive model compatibility result. |
| `incorrect_tool_result_followup` | Tool call was accepted, but the follow-up did not contain the expected result. |

A successful historical test is **not a guarantee** of current availability or of compatibility with every real Hermes tool. Some models need `tool_choice="required"`, which this conservative AUTO-mode test does not force. The tool test verifies a basic two-turn protocol, not model quality, context size, performance, or free pricing.

## Security and cost

- API calls happen **only** when you execute `hermes freeverify scan`.
- Saved state is `$HERMES_HOME/free-model-verifier.json` with restrictive `0600` permissions and atomic writes. It includes model names, status, timestamps, and a salted account identifier; no credentials or request bodies.
- Credentials are loaded with Hermes `agent.secret_scope.get_secret`, not by reading `.env` or `auth.json`. Multiplexed profile isolation is preserved.
- Endpoint URLs are fixed; redirects are refused so an API key cannot be forwarded to an arbitrary redirect URL.
- **NVIDIA and Google Free Tiers depend on the account, model and provider's rules.** The `--ack-unpriced` flag is your acknowledgement, **not evidence that a request is free**. Requests can consume quota or incur charges if your project is billed.
- OpenRouter is filtered to free variants or accounts reporting zero prompt/completion rates, but account policy may still limit usage.
- Tests may take time and consume daily requests. Default scan limit is 5; `--limit 0` explicitly permits all. API `429` stops the current provider scan.
- No unattended operations, polling, auto-rerouting, provider-key rotation, or attempts to evade provider limits.

## Local tests (no network)

```bash
python3 -m unittest discover -s tests -v
```

## Tested evidence from a private Linux experiment

On **2026-10-23**, a private Linux Hermes installation was used to probe 23 models: 21 completed the synthetic two-turn tool cycle (20 in AUTO mode and one with `tool_choice=required`), while two were inconclusive due to network/rate limits. **Those observations are not automated test coverage for this new portable plugin**, and model availability is time-sensitive. Provider/model lists are *not* hard-coded from that experiment.

## Roadmap

- [x] Manual free-model discovery / generation / full tool-cycle probing.
- [x] Local historical receipts and read-only `/freeverify` command (current credential only).
- [ ] Host-backed Hermes plugin validation on current stable releases.
- [ ] Native opt-in Telegram `/model` refresh and verified-only picker, without monkeypatching core.
- [ ] Better provenance for account quota / model price / transient provider failures.

## Related work / credit

- Hermes upstream discussion: [#112143 — dynamic free-model availability inventory](https://github.com/NousResearch/hermes-agent/issues/112143).
- [hermes-openrouter-free-rotator](https://github.com/GoSlowPoke168/hermes-openrouter-free-rotator) — a different approach: scheduled OpenRouter free-model routing.
- [hermes-portable](https://github.com/krisiliev-dev/hermes-portable) — free-first setup and health checks.
- [quantum-hermes-free-router-kit](https://github.com/spacepirate15/quantum-hermes-free-router-kit) — free-tier LiteLLM proxy routing.

This project does not copy their implementations. Respect all upstream licenses and your provider terms.

## License

MIT — see [LICENSE](LICENSE).
