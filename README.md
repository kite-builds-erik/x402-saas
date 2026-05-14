# x402-saas

> Hosted x402 paywalls. Sign up with a wallet, get a paywalled proxy URL in 60 seconds. We take 1% of routed USDC volume. You keep 99%, we handle the 402 dance.

[![tests](https://img.shields.io/badge/tests-38%2F38_passing-brightgreen)]()
[![license](https://img.shields.io/badge/license-MIT-blue)]()
[![demo](https://img.shields.io/badge/demo-live-blue)](https://x402-saas.surge.sh)
[![awesome-x402](https://img.shields.io/badge/listed_in-awesome--x402-7c3aed)](https://github.com/xpaysh/awesome-x402#hosted-facilitators)

**Live**: backend at [`x402-saas.onrender.com`](https://x402-saas.onrender.com/__x402/health) · landing at [`x402-saas.surge.sh`](https://x402-saas.surge.sh)

This is the multi-tenant managed companion to [`x402-kit`](../x402-kit). The kit is the open-source toolkit you self-host. **`x402-saas` is the hosted version we run for you** — when you don't want to manage infra, configure DNS, or write 402 middleware, you point your backend at our proxy and we take care of the rest.

## Demo

![x402-saas proxy flow](https://x402-saas.surge.sh/demo.gif)

One HTTP round-trip: agent calls a paid endpoint, gets back a 402 with
price + payTo, signs an EIP-3009 transfer, retries with the `X-PAYMENT`
header, receives the actual response. 99% of the USDC routes to the
merchant, 1% to x402-saas treasury. No agent registration, no API key.

Reproduce locally:

```bash
bash scripts/demo-walkthrough.sh
```

## How it works

```
Your customer (an agent or a human)
        │
        │  GET https://acme.kite.dev/forecast
        ▼
┌────────────────────────────────┐
│   x402-saas data plane         │
│                                │
│  1. Look up tenant from        │
│     subdomain "acme"           │
│  2. Return HTTP 402 with your  │
│     price + payTo              │
│  3. On valid X-PAYMENT,        │
│     forward to your backend    │
│  4. Settle: 99% → you, 1% → us │
│  5. Log event to your dash     │
└────────────────────────────────┘
        │
        ▼ forwards to
   https://api.acme.io/forecast (your origin)
```

## Tenant onboarding

In the dashboard:

1. **Connect wallet** (any EVM wallet — RainbowKit / WalletConnect).
2. Sign a SIWE challenge — proves you own the wallet that will receive payouts. No email, no password.
3. Configure routes:
   - Method + path (e.g. `GET /forecast`)
   - Price (USDC, e.g. `0.05`)
   - Backend URL (your origin)
4. **Done.** You get back `https://<your-slug>.kite.dev`.

Send that URL to your customers. We do the rest.

## What's in this repo

| Module | What |
|---|---|
| `src/db.ts` | SQLite-backed tenant + route + event ledger (Postgres next) |
| `src/slug.ts` | Slug derivation + reservation list |
| `src/auth.ts` | SIWE-style challenge + signature verification (viem) |
| `src/control-plane.ts` | REST API: `POST /auth/challenge`, `POST /tenants`, etc. |
| `src/data-plane.ts` | The proxy: slug-based routing, 402 negotiation, upstream forwarding, event logging |
| `src/server.ts` | Entrypoint: mounts control + data plane |
| `test/*.test.ts` | 38 tests covering all of the above |

## Running locally

```bash
npm install
npm run build
npm test               # 38 tests, ~1s
npm run dev            # boots on :4000
```

Smoke test against the live backend:

```bash
curl https://x402-saas.onrender.com/__x402/health
curl -X POST https://x402-saas.onrender.com/api/v1/auth/challenge \
  -H 'content-type: application/json' \
  -d '{"walletAddress":"0xC504Fd656330A823C3ffcBAB048c05cF45F60Bdf"}'

# Public platform impact data (no auth)
curl https://x402-saas.onrender.com/__x402/platform-metrics
```

## Stack

- Node 22+, TypeScript, Express
- `viem` for SIWE signature verification
- `better-sqlite3` for v0 ledger (Postgres in v1)
- Tests: `node:test` + real HTTP servers (no mocks of the network surface)

## Stub facilitator

The data plane currently uses a stub facilitator that accepts any `X-PAYMENT: stub:<address>` header. The real wiring (Coinbase facilitator + actual on-chain settlement) lives in [`x402-kit/packages/server`](../x402-kit/packages/server) and gets swapped in for production deploys. Stub mode is what lets us test the full request lifecycle deterministically.

## Fee mechanism

- Each tenant has a `fee_bps` (default `100` = 1.00%).
- On settlement, the route's `payTo` is constructed as a 99/1 split: 99% to the tenant, 1% to `X402_SAAS_FEE_WALLET`.
- If the underlying facilitator doesn't support multi-recipient settlement, fallback is post-settlement sweep via signed permit (decided at deploy time, not in code yet).

## Multi-tenancy & host parsing

The data plane reads `Host: <slug>.<domain>` to identify the tenant. In production, `*.kite.dev` is wildcard-routed at the edge. In tests / dev, `enforceHostMatch=false` enables an `X-Slug-Override` header so we can run all 38 tests without DNS surgery.

## License

MIT — same as `x402-kit`.

---

*See [`ARCHITECTURE.md`](./ARCHITECTURE.md) for the full design doc, sequencing plan, and capital plan.*
