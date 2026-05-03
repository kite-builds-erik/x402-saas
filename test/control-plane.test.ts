import { test } from "node:test";
import assert from "node:assert/strict";
import { mkdtempSync, rmSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";
import express from "express";
import type { Server } from "node:http";
import type { AddressInfo } from "node:net";
import { privateKeyToAccount, generatePrivateKey } from "viem/accounts";
import { SaasDb } from "../dist/db.js";
import { controlPlaneRouter } from "../dist/control-plane.js";

interface RunningCp {
  url: string;
  db: SaasDb;
  close(): Promise<void>;
}

async function startCp(): Promise<RunningCp> {
  const dir = mkdtempSync(join(tmpdir(), "x402-saas-cp-"));
  const db = new SaasDb(join(dir, "test.db"));
  const app = express();
  app.use("/api/v1", controlPlaneRouter({ db, domain: "kite.test" }));
  const server: Server = await new Promise((resolve) => {
    const s = app.listen(0, () => resolve(s));
  });
  const port = (server.address() as AddressInfo).port;
  return {
    url: `http://127.0.0.1:${port}`,
    db,
    close: () =>
      new Promise<void>((resolve, reject) => {
        server.close((err) => (err ? reject(err) : resolve()));
        db.close();
        rmSync(dir, { recursive: true, force: true });
      }),
  };
}

test("POST /auth/challenge returns a SIWE challenge with message", async () => {
  const cp = await startCp();
  try {
    const acct = privateKeyToAccount(generatePrivateKey());
    const res = await fetch(`${cp.url}/api/v1/auth/challenge`, {
      method: "POST",
      headers: { "content-type": "application/json" },
      body: JSON.stringify({ walletAddress: acct.address }),
    });
    assert.equal(res.status, 200);
    const body = (await res.json()) as {
      challenge: { walletAddress: string; nonce: string };
      message: string;
    };
    assert.equal(body.challenge.walletAddress, acct.address.toLowerCase());
    assert.match(body.message, /Sign in to x402-kit SaaS/);
  } finally {
    await cp.close();
  }
});

test("POST /tenants creates a tenant after valid SIWE signature", async () => {
  const cp = await startCp();
  try {
    const acct = privateKeyToAccount(generatePrivateKey());

    const challengeRes = await fetch(`${cp.url}/api/v1/auth/challenge`, {
      method: "POST",
      headers: { "content-type": "application/json" },
      body: JSON.stringify({ walletAddress: acct.address }),
    });
    const { challenge, message } = (await challengeRes.json()) as {
      challenge: unknown;
      message: string;
    };
    const signature = await acct.signMessage({ message });

    const createRes = await fetch(`${cp.url}/api/v1/tenants`, {
      method: "POST",
      headers: { "content-type": "application/json" },
      body: JSON.stringify({
        challenge,
        signature,
        network: "base-sepolia",
        routes: [
          {
            method: "GET",
            path: "/forecast",
            priceUsd: "0.05",
            description: "Weather",
            backendUrl: "https://api.example.com",
          },
        ],
      }),
    });
    assert.equal(createRes.status, 201);
    const body = (await createRes.json()) as {
      tenant: { walletAddress: string; slug: string; proxyUrl: string };
      routes: Array<{ path: string }>;
    };
    assert.equal(body.tenant.walletAddress, acct.address.toLowerCase());
    assert.match(body.tenant.proxyUrl, /^https:\/\/[\w-]+\.kite\.test$/);
    assert.equal(body.routes.length, 1);
  } finally {
    await cp.close();
  }
});

test("POST /tenants rejects invalid signature", async () => {
  const cp = await startCp();
  try {
    const alice = privateKeyToAccount(generatePrivateKey());
    const mallory = privateKeyToAccount(generatePrivateKey());

    const challengeRes = await fetch(`${cp.url}/api/v1/auth/challenge`, {
      method: "POST",
      headers: { "content-type": "application/json" },
      body: JSON.stringify({ walletAddress: alice.address }),
    });
    const { challenge, message } = (await challengeRes.json()) as {
      challenge: unknown;
      message: string;
    };
    // Mallory signs alice's message
    const badSig = await mallory.signMessage({ message });

    const createRes = await fetch(`${cp.url}/api/v1/tenants`, {
      method: "POST",
      headers: { "content-type": "application/json" },
      body: JSON.stringify({
        challenge,
        signature: badSig,
        network: "base-sepolia",
        routes: [
          {
            method: "GET",
            path: "/x",
            priceUsd: "0.01",
            backendUrl: "https://api.example.com",
          },
        ],
      }),
    });
    assert.equal(createRes.status, 401);
  } finally {
    await cp.close();
  }
});

test("POST /tenants rejects bad route input", async () => {
  const cp = await startCp();
  try {
    const acct = privateKeyToAccount(generatePrivateKey());
    const challengeRes = await fetch(`${cp.url}/api/v1/auth/challenge`, {
      method: "POST",
      headers: { "content-type": "application/json" },
      body: JSON.stringify({ walletAddress: acct.address }),
    });
    const { challenge, message } = (await challengeRes.json()) as {
      challenge: unknown;
      message: string;
    };
    const signature = await acct.signMessage({ message });

    // bad path
    const badPathRes = await fetch(`${cp.url}/api/v1/tenants`, {
      method: "POST",
      headers: { "content-type": "application/json" },
      body: JSON.stringify({
        challenge,
        signature,
        network: "base-sepolia",
        routes: [
          { method: "GET", path: "no-leading-slash", priceUsd: "0.01", backendUrl: "https://x.example" },
        ],
      }),
    });
    assert.equal(badPathRes.status, 400);

    // bad price
    const badPriceRes = await fetch(`${cp.url}/api/v1/tenants`, {
      method: "POST",
      headers: { "content-type": "application/json" },
      body: JSON.stringify({
        challenge,
        signature,
        network: "base-sepolia",
        routes: [
          { method: "GET", path: "/x", priceUsd: "free", backendUrl: "https://x.example" },
        ],
      }),
    });
    assert.equal(badPriceRes.status, 400);
  } finally {
    await cp.close();
  }
});

test("POST /tenants rejects expired challenge", async () => {
  const cp = await startCp();
  try {
    const acct = privateKeyToAccount(generatePrivateKey());

    const challengeRes = await fetch(`${cp.url}/api/v1/auth/challenge`, {
      method: "POST",
      headers: { "content-type": "application/json" },
      body: JSON.stringify({ walletAddress: acct.address }),
    });
    const { challenge, message } = (await challengeRes.json()) as {
      challenge: { expiresAt: number };
      message: string;
    };
    // Backdate the challenge so server-side validation rejects it.
    challenge.expiresAt = Date.now() - 1000;
    const signature = await acct.signMessage({ message });

    const createRes = await fetch(`${cp.url}/api/v1/tenants`, {
      method: "POST",
      headers: { "content-type": "application/json" },
      body: JSON.stringify({
        challenge,
        signature,
        network: "base-sepolia",
        routes: [
          {
            method: "GET",
            path: "/x",
            priceUsd: "0.01",
            backendUrl: "https://api.example.com",
          },
        ],
      }),
    });
    assert.equal(createRes.status, 401);
    const body = (await createRes.json()) as { error: string; reason: string };
    assert.match(body.reason, /expired/i);
  } finally {
    await cp.close();
  }
});

test("creating a second tenant with same wallet returns 409", async () => {
  const cp = await startCp();
  try {
    const acct = privateKeyToAccount(generatePrivateKey());
    async function signAndCreate() {
      const challengeRes = await fetch(`${cp.url}/api/v1/auth/challenge`, {
        method: "POST",
        headers: { "content-type": "application/json" },
        body: JSON.stringify({ walletAddress: acct.address }),
      });
      const { challenge, message } = (await challengeRes.json()) as {
        challenge: unknown;
        message: string;
      };
      const signature = await acct.signMessage({ message });
      return fetch(`${cp.url}/api/v1/tenants`, {
        method: "POST",
        headers: { "content-type": "application/json" },
        body: JSON.stringify({
          challenge,
          signature,
          network: "base-sepolia",
          routes: [
            {
              method: "GET",
              path: "/x",
              priceUsd: "0.01",
              backendUrl: "https://x.example",
            },
          ],
        }),
      });
    }
    const first = await signAndCreate();
    assert.equal(first.status, 201);
    const second = await signAndCreate();
    assert.equal(second.status, 409);
  } finally {
    await cp.close();
  }
});
