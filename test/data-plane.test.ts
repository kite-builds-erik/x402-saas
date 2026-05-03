import { test } from "node:test";
import assert from "node:assert/strict";
import { mkdtempSync, rmSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";
import express from "express";
import type { Server } from "node:http";
import type { AddressInfo } from "node:net";
import { SaasDb } from "../dist/db.js";
import { dataPlaneRouter, parseSlugFromHost } from "../dist/data-plane.js";
import type { FacilitatorClient } from "../dist/facilitator.js";

interface RunningPlane {
  url: string;
  db: SaasDb;
  upstreamHits: Array<{ url: string; method: string; body: string }>;
  upstreamPayload: { status: number; body: string; contentType: string };
  close(): Promise<void>;
}

interface StartPlaneOptions {
  facilitator?: FacilitatorClient;
  fetchImpl?: typeof fetch;
}

async function startPlane(opts: StartPlaneOptions = {}): Promise<RunningPlane> {
  const dir = mkdtempSync(join(tmpdir(), "x402-saas-dp-"));
  const db = new SaasDb(join(dir, "test.db"));

  const upstreamHits: Array<{ url: string; method: string; body: string }> = [];
  const upstreamPayload = {
    status: 200,
    body: JSON.stringify({ ok: true, value: "pong" }),
    contentType: "application/json",
  };
  const defaultFakeFetch: typeof fetch = (async (input: unknown, init?: RequestInit) => {
    const url =
      typeof input === "string" ? input : input instanceof URL ? input.toString() : (input as Request).url;
    upstreamHits.push({
      url,
      method: init?.method ?? "GET",
      body: init?.body ? String(init.body) : "",
    });
    return new Response(upstreamPayload.body, {
      status: upstreamPayload.status,
      headers: { "content-type": upstreamPayload.contentType },
    });
  }) as typeof fetch;

  const app = express();
  app.use(
    dataPlaneRouter({
      db,
      domain: "kite.test",
      enforceHostMatch: false,
      fetchImpl: opts.fetchImpl ?? defaultFakeFetch,
      feeWallet: "0xfee0000000000000000000000000000000000000",
      facilitator: opts.facilitator,
    }),
  );
  const server = await new Promise<Server>((resolve) => {
    const s = app.listen(0, () => resolve(s));
  });
  const port = (server.address() as AddressInfo).port;

  return {
    url: `http://127.0.0.1:${port}`,
    db,
    upstreamHits,
    upstreamPayload,
    close: () =>
      new Promise<void>((resolve, reject) => {
        server.close((err) => (err ? reject(err) : resolve()));
        db.close();
        rmSync(dir, { recursive: true, force: true });
      }),
  };
}

test("parseSlugFromHost extracts slug from <slug>.<domain>", () => {
  assert.equal(parseSlugFromHost("acme.kite.test", "kite.test", true), "acme");
  assert.equal(parseSlugFromHost("acme.kite.test:3000", "kite.test", true), "acme");
  assert.equal(parseSlugFromHost("kite.test", "kite.test", true), null);
  assert.equal(parseSlugFromHost("nested.acme.kite.test", "kite.test", true), null);
  assert.equal(parseSlugFromHost("acme.localhost", "kite.test", false), "acme");
});

test("missing X-PAYMENT returns 402 with payment requirements", async () => {
  const p = await startPlane();
  try {
    p.db.createTenant({
      walletAddress: "0x1111111111111111111111111111111111111111",
      slug: "acme",
      network: "base-sepolia",
    });
    p.db.addRoute({
      tenantId: p.db.getTenantBySlug("acme")!.id,
      method: "GET",
      path: "/forecast",
      priceUsd: "0.05",
      backendUrl: "https://upstream.example",
    });

    const res = await fetch(`${p.url}/forecast`, { headers: { "x-slug-override": "acme" } });
    assert.equal(res.status, 402);
    const body = (await res.json()) as { error: string; accepts: Array<{ payTo: string }> };
    assert.equal(body.error, "payment_required");
    assert.equal(body.accepts[0].payTo, "0x1111111111111111111111111111111111111111");
    assert.equal(p.upstreamHits.length, 0);
  } finally {
    await p.close();
  }
});

test("valid X-PAYMENT proxies to upstream and records paid event", async () => {
  const p = await startPlane();
  try {
    const t = p.db.createTenant({
      walletAddress: "0x1111111111111111111111111111111111111111",
      slug: "acme",
      network: "base-sepolia",
    });
    p.db.addRoute({
      tenantId: t.id,
      method: "GET",
      path: "/forecast",
      priceUsd: "0.05",
      backendUrl: "https://upstream.example",
    });

    const res = await fetch(`${p.url}/forecast`, {
      headers: { "x-slug-override": "acme", "x-payment": "stub:0xpayer123" },
    });
    assert.equal(res.status, 200);
    const json = (await res.json()) as { ok: boolean; value: string };
    assert.equal(json.value, "pong");
    assert.equal(p.upstreamHits.length, 1);
    assert.match(p.upstreamHits[0].url, /upstream\.example\/forecast/);

    const events = p.db.recentEvents(t.id, 10);
    assert.equal(events.length, 1);
    assert.equal(events[0].status, "paid");
    assert.equal(events[0].payer, "0xpayer123");
    assert.equal(events[0].amountUsd, "0.05");
  } finally {
    await p.close();
  }
});

test("unknown route returns 404 + records error event", async () => {
  const p = await startPlane();
  try {
    const t = p.db.createTenant({
      walletAddress: "0x2222222222222222222222222222222222222222",
      slug: "acme2",
      network: "base-sepolia",
    });
    p.db.addRoute({
      tenantId: t.id,
      method: "GET",
      path: "/known",
      priceUsd: "0.05",
      backendUrl: "https://upstream.example",
    });

    const res = await fetch(`${p.url}/unknown`, {
      headers: { "x-slug-override": "acme2", "x-payment": "stub:0xanyone" },
    });
    assert.equal(res.status, 404);
    const events = p.db.recentEvents(t.id, 10);
    assert.equal(events.length, 1);
    assert.equal(events[0].status, "error");
    assert.equal(events[0].reason, "route_not_found");
  } finally {
    await p.close();
  }
});

test("paused tenant is rejected with 503", async () => {
  const p = await startPlane();
  try {
    const t = p.db.createTenant({
      walletAddress: "0x3333333333333333333333333333333333333333",
      slug: "paused",
      network: "base-sepolia",
    });
    p.db.setTenantStatus(t.id, "paused");
    p.db.addRoute({
      tenantId: t.id,
      method: "GET",
      path: "/x",
      priceUsd: "0.05",
      backendUrl: "https://upstream.example",
    });

    const res = await fetch(`${p.url}/x`, {
      headers: { "x-slug-override": "paused", "x-payment": "stub:0xanyone" },
    });
    assert.equal(res.status, 503);
  } finally {
    await p.close();
  }
});

test("upstream-unreachable returns 502 + records error event with paid amount", async () => {
  const dir = mkdtempSync(join(tmpdir(), "x402-saas-dp-"));
  const db = new SaasDb(join(dir, "test.db"));
  const erroringFetch: typeof fetch = (async () => {
    throw new Error("ECONNREFUSED upstream");
  }) as typeof fetch;
  const app = express();
  app.use(
    dataPlaneRouter({
      db,
      domain: "kite.test",
      enforceHostMatch: false,
      fetchImpl: erroringFetch,
      feeWallet: "0xfee0000000000000000000000000000000000000",
    }),
  );
  const server = await new Promise<Server>((resolve) => {
    const s = app.listen(0, () => resolve(s));
  });
  const port = (server.address() as AddressInfo).port;
  try {
    const t = db.createTenant({
      walletAddress: "0x5555555555555555555555555555555555555555",
      slug: "upfail",
      network: "base-sepolia",
    });
    db.addRoute({
      tenantId: t.id,
      method: "GET",
      path: "/x",
      priceUsd: "0.05",
      backendUrl: "https://upstream.example",
    });
    const res = await fetch(`http://127.0.0.1:${port}/x`, {
      headers: { "x-slug-override": "upfail", "x-payment": "stub:0xpaid" },
    });
    assert.equal(res.status, 502);
    const body = (await res.json()) as { error: string; message: string };
    assert.equal(body.error, "upstream_unreachable");
    assert.match(body.message, /ECONNREFUSED/);

    const events = db.recentEvents(t.id, 10);
    assert.equal(events.length, 1);
    assert.equal(events[0].status, "error");
    assert.equal(events[0].payer, "0xpaid"); // payer survives so we can refund
    assert.equal(events[0].amountUsd, "0.05"); // amount recorded for reconciliation
  } finally {
    await new Promise<void>((resolve, reject) =>
      server.close((err) => (err ? reject(err) : resolve())),
    );
    db.close();
    rmSync(dir, { recursive: true, force: true });
  }
});

test("settle-failure path records 'rejected' even after upstream returned 200 (silent revenue-loss prevention)", async () => {
  const dir = mkdtempSync(join(tmpdir(), "x402-saas-dp-"));
  const db = new SaasDb(join(dir, "test.db"));

  // Facilitator that ACCEPTS verify but FAILS on settle. The customer sees
  // upstream's 200 response, but we must record the event as rejected so the
  // tenant can chase the bad settlement (rather than thinking they got paid).
  const failingSettleFacilitator = {
    async verify({ paymentHeader }: { paymentHeader: string }) {
      return paymentHeader.startsWith("stub:")
        ? {
            ok: true as const,
            payer: paymentHeader.slice(5),
            txHash: null,
            payload: { x402Version: 1 } as never,
            requirements: { scheme: "exact" } as never,
          }
        : { ok: false as const, reason: "no stub" };
    },
    async settle() {
      return { success: false, errorReason: "facilitator rejected at settlement" };
    },
  };

  const fakeFetch: typeof fetch = (async () => new Response('{"ok":true}', {
    status: 200,
    headers: { "content-type": "application/json" },
  })) as typeof fetch;

  const app = express();
  app.use(
    dataPlaneRouter({
      db,
      domain: "kite.test",
      enforceHostMatch: false,
      fetchImpl: fakeFetch,
      feeWallet: "0xfee0000000000000000000000000000000000000",
      facilitator: failingSettleFacilitator,
    }),
  );
  const server = await new Promise<Server>((resolve) => {
    const s = app.listen(0, () => resolve(s));
  });
  const port = (server.address() as AddressInfo).port;
  try {
    const t = db.createTenant({
      walletAddress: "0x6666666666666666666666666666666666666666",
      slug: "settlefail",
      network: "base-sepolia",
    });
    db.addRoute({
      tenantId: t.id,
      method: "GET",
      path: "/y",
      priceUsd: "0.10",
      backendUrl: "https://upstream.example",
    });
    const res = await fetch(`http://127.0.0.1:${port}/y`, {
      headers: { "x-slug-override": "settlefail", "x-payment": "stub:0xpaidbutsettlefail" },
    });
    // Customer DID get the upstream response (we already streamed it back)
    assert.equal(res.status, 200);

    // Settle ran async after res.send; give it a tick to land
    await new Promise((r) => setTimeout(r, 50));

    const events = db.recentEvents(t.id, 10);
    assert.equal(events.length, 1);
    assert.equal(events[0].status, "rejected");
    assert.equal(events[0].payer, "0xpaidbutsettlefail");
    assert.equal(events[0].amountUsd, "0.10");
    assert.match(events[0].reason ?? "", /settlement|settle failed|facilitator rejected/i);
  } finally {
    await new Promise<void>((resolve, reject) =>
      server.close((err) => (err ? reject(err) : resolve())),
    );
    db.close();
    rmSync(dir, { recursive: true, force: true });
  }
});

test("__x402/health and __x402/metrics endpoints work scoped to tenant", async () => {
  const p = await startPlane();
  try {
    const t = p.db.createTenant({
      walletAddress: "0x4444444444444444444444444444444444444444",
      slug: "stats",
      network: "base-sepolia",
    });
    const r = p.db.addRoute({
      tenantId: t.id,
      method: "GET",
      path: "/y",
      priceUsd: "0.01",
      backendUrl: "https://upstream.example",
    });
    p.db.recordEvent({
      tenantId: t.id,
      routeId: r.id,
      payer: "0xX",
      status: "paid",
      amountUsd: "0.01",
      txHash: "0xtx",
      facilitator: "stub",
      latencyMs: 100,
      reason: null,
    });

    const health = await fetch(`${p.url}/__x402/health`, {
      headers: { "x-slug-override": "stats" },
    });
    assert.equal(health.status, 200);

    const metricsRes = await fetch(`${p.url}/__x402/metrics`, {
      headers: { "x-slug-override": "stats" },
    });
    assert.equal(metricsRes.status, 200);
    const metrics = (await metricsRes.json()) as { paidRequests: number };
    assert.equal(metrics.paidRequests, 1);
  } finally {
    await p.close();
  }
});
