import express, { type Request, type Response, type NextFunction, type Router } from "express";
import { SaasDb, type Tenant, type Route } from "./db.js";
import {
  STUB_FACILITATOR,
  type FacilitatorClient,
  type PaymentPayload,
  type PaymentRequirements,
} from "./facilitator.js";

export interface DataPlaneOptions {
  db: SaasDb;
  domain: string;
  /**
   * If set, requests whose Host header doesn't match `*.${domain}` are rejected.
   * Useful when running behind a wildcard ingress; disable for unit tests.
   */
  enforceHostMatch?: boolean;
  /**
   * Override the upstream fetch implementation (for tests).
   */
  fetchImpl?: typeof fetch;
  /**
   * Treasury wallet that receives the platform fee.
   */
  feeWallet: string;
  /**
   * Facilitator implementation. Defaults to STUB_FACILITATOR (accepts
   * "stub:<addr>" headers). Inject HttpFacilitator in production.
   */
  facilitator?: FacilitatorClient;
}

export function dataPlaneRouter(opts: DataPlaneOptions): Router {
  const router = express.Router();
  const facilitator = opts.facilitator ?? STUB_FACILITATOR;
  const fetchImpl = opts.fetchImpl ?? fetch;

  router.use(express.raw({ type: "*/*", limit: "1mb" }));

  router.use((req: Request, res: Response, next: NextFunction) => {
    const enforce = opts.enforceHostMatch ?? false;
    let slug = parseSlugFromHost(String(req.headers.host ?? ""), opts.domain, enforce);
    if (!slug && !enforce) {
      const override = req.headers["x-slug-override"];
      if (typeof override === "string" && override.length > 0) slug = override;
    }
    if (!slug) {
      if (req.path === "/__x402/health") {
        return res.json({ ok: true, ts: Date.now(), service: "x402-saas-data-plane" });
      }
      return res.status(404).json({ error: "no_tenant_in_host" });
    }

    const tenant = opts.db.getTenantBySlug(slug);
    if (!tenant) {
      return res.status(404).json({ error: "tenant_not_found", slug });
    }
    if (tenant.status !== "active") {
      return res.status(503).json({ error: "tenant_paused", status: tenant.status });
    }

    if (req.path === "/__x402/health") {
      return res.json({ ok: true, ts: Date.now(), tenantId: tenant.id });
    }
    if (req.path === "/__x402/metrics") {
      return res.json(opts.db.tenantMetrics(tenant.id));
    }
    if (req.path === "/__x402/events") {
      return res.json({ events: opts.db.recentEvents(tenant.id, 50) });
    }

    return handleProxiedRequest({
      tenant,
      req,
      res,
      next,
      db: opts.db,
      facilitator,
      fetchImpl,
      feeWallet: opts.feeWallet,
    });
  });

  return router;
}

interface ProxiedRequestArgs {
  tenant: Tenant;
  req: Request;
  res: Response;
  next: NextFunction;
  db: SaasDb;
  facilitator: FacilitatorClient;
  fetchImpl: typeof fetch;
  feeWallet: string;
}

async function handleProxiedRequest(args: ProxiedRequestArgs): Promise<void> {
  const { tenant, req, res, db, facilitator, fetchImpl } = args;
  const start = Date.now();
  const route = db.routeForRequest(tenant.id, req.method, req.path);

  // Closure: every event from this request shares tenant/route/latency.
  // Each call site only specifies what differs (status, payer, amount, etc).
  type EventDelta = {
    status: "paid" | "rejected" | "free" | "error";
    payer?: string | null;
    amountUsd?: string | null;
    txHash?: string | null;
    facilitator?: string | null;
    reason?: string | null;
  };
  const facilitatorLabel = facilitator === STUB_FACILITATOR ? "stub" : "http";
  const log = (e: EventDelta): void => {
    db.recordEvent({
      tenantId: tenant.id,
      routeId: route?.id ?? null,
      payer: e.payer ?? null,
      status: e.status,
      amountUsd: e.amountUsd ?? null,
      txHash: e.txHash ?? null,
      facilitator: e.facilitator === undefined ? null : e.facilitator,
      latencyMs: Date.now() - start,
      reason: e.reason ?? null,
    });
  };

  if (!route) {
    log({ status: "error", reason: "route_not_found" });
    res.status(404).json({ error: "route_not_configured", method: req.method, path: req.path });
    return;
  }

  const paymentHeader = String(req.headers["x-payment"] ?? "");
  if (!paymentHeader) {
    res.status(402)
      .header("X-Accepts", x402AcceptsHeader(tenant, route))
      .json({
        error: "payment_required",
        accepts: [
          {
            scheme: "x402-v1",
            network: tenant.network,
            maxAmountRequired: route.priceUsd,
            payTo: tenant.walletAddress,
            asset: "USDC",
            description: route.description,
            extra: {
              feeBps: tenant.feeBps,
              treasuryFeeRecipient: args.feeWallet,
            },
          },
        ],
      });
    log({ status: "rejected", reason: "missing_x_payment" });
    return;
  }

  const verify = await facilitator.verify({
    paymentHeader,
    payTo: tenant.walletAddress,
    amountUsd: route.priceUsd,
    network: tenant.network,
  });
  if (!verify.ok) {
    res.status(402).json({ error: "payment_invalid", reason: verify.reason });
    log({ status: "rejected", reason: verify.reason });
    return;
  }

  // Forward upstream
  let upstream: globalThis.Response;
  try {
    const upstreamUrl = `${route.backendUrl.replace(/\/+$/, "")}${req.path}${
      req.url.includes("?") ? "?" + req.url.split("?")[1] : ""
    }`;
    upstream = await fetchImpl(upstreamUrl, {
      method: req.method,
      headers: forwardableHeaders(req),
      body:
        req.method === "GET" || req.method === "HEAD"
          ? undefined
          : (req.body as Buffer | undefined),
    });
  } catch (err) {
    const msg = err instanceof Error ? err.message : String(err);
    res.status(502).json({ error: "upstream_unreachable", message: msg });
    log({
      status: "error",
      payer: verify.payer,
      amountUsd: route.priceUsd,
      txHash: verify.txHash,
      facilitator: facilitatorLabel,
      reason: msg,
    });
    return;
  }

  const ct = upstream.headers.get("content-type") ?? "application/octet-stream";
  res.status(upstream.status).type(ct);
  const body = await upstream.arrayBuffer();
  res.send(Buffer.from(body));

  // Async settle (don't block response). On real facilitators this is the
  // on-chain settlement call; on the stub it's a no-op.
  let txHash: string | null = verify.txHash;
  let settleStatus: "paid" | "rejected" = "paid";
  let settleReason: string | null = null;
  if (facilitator.settle && verify.payload && verify.requirements) {
    try {
      const r = await facilitator.settle({
        payload: verify.payload as PaymentPayload,
        requirements: verify.requirements as PaymentRequirements,
      });
      if (r.success) {
        txHash = r.transaction ?? txHash;
      } else {
        settleStatus = "rejected";
        settleReason = r.errorReason ?? "settle failed";
      }
    } catch (err) {
      settleStatus = "rejected";
      settleReason = `settle threw: ${err instanceof Error ? err.message : String(err)}`;
    }
  }

  log({
    status: settleStatus,
    payer: verify.payer,
    amountUsd: route.priceUsd,
    txHash,
    facilitator: facilitatorLabel,
    reason: settleReason,
  });
}

function forwardableHeaders(req: Request): Record<string, string> {
  const out: Record<string, string> = {};
  const skip = new Set([
    "host",
    "connection",
    "content-length",
    "x-payment",
    "x-forwarded-for",
    "x-forwarded-host",
    "x-forwarded-proto",
  ]);
  for (const [k, v] of Object.entries(req.headers)) {
    if (!v || skip.has(k.toLowerCase())) continue;
    if (Array.isArray(v)) {
      out[k] = v.join(",");
    } else {
      out[k] = String(v);
    }
  }
  return out;
}

function x402AcceptsHeader(tenant: Tenant, route: Route): string {
  return `x402-v1 network=${tenant.network} amount=${route.priceUsd} payTo=${tenant.walletAddress}`;
}

export function parseSlugFromHost(
  host: string,
  domain: string,
  enforce: boolean,
): string | null {
  // strip port
  const cleanHost = host.split(":")[0].toLowerCase();
  const cleanDomain = domain.toLowerCase();
  if (cleanHost === cleanDomain) return null;
  if (cleanHost.endsWith(`.${cleanDomain}`)) {
    const slug = cleanHost.slice(0, cleanHost.length - cleanDomain.length - 1);
    if (slug.includes(".")) return null; // we don't allow nested slugs
    return slug;
  }
  // localhost dev: t-xxxx.localhost or x-slug-test.localhost
  if (cleanHost.endsWith(".localhost")) {
    const slug = cleanHost.slice(0, cleanHost.length - ".localhost".length);
    if (!slug.includes(".")) return slug;
  }
  // for tests / dev: allow x-slug-header injection
  if (!enforce) {
    return null;
  }
  return null;
}
