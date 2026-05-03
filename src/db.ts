import Database from "better-sqlite3";
import { ulid } from "ulid";

const USDC_DECIMALS = 6;
const USDC_SCALE = 1_000_000n;

/** "0.05" → 50000 (USDC atomic units). Returns null if the input is malformed. */
function usdToAtomic(usd: string): number | null {
  if (!/^\d+(\.\d{1,6})?$/.test(usd)) return null;
  const [whole, frac = ""] = usd.split(".");
  const padded = (frac + "0".repeat(USDC_DECIMALS)).slice(0, USDC_DECIMALS);
  return Number(BigInt(whole) * USDC_SCALE + BigInt(padded));
}

/** 50000n → "0.050000". Always returns 6-decimal precision. */
function atomicToUsdString(atomic: bigint): string {
  return `${atomic / USDC_SCALE}.${(atomic % USDC_SCALE).toString().padStart(USDC_DECIMALS, "0")}`;
}

export interface Tenant {
  id: string;
  walletAddress: string;
  slug: string;
  network: string;
  createdAt: number;
  status: "active" | "paused" | "banned";
  feeBps: number;
}

export interface Route {
  id: string;
  tenantId: string;
  method: string;
  path: string;
  priceUsd: string;
  description: string | null;
  backendUrl: string;
  createdAt: number;
}

export interface EventRecord {
  id: string;
  tenantId: string;
  routeId: string | null;
  payer: string | null;
  status: "paid" | "rejected" | "free" | "error";
  amountUsd: string | null;
  txHash: string | null;
  facilitator: string | null;
  latencyMs: number | null;
  reason: string | null;
  createdAt: number;
}

export class SaasDb {
  readonly db: Database.Database;

  constructor(path: string) {
    this.db = new Database(path);
    this.db.pragma("journal_mode = WAL");
    this.db.pragma("foreign_keys = ON");
    this.migrate();
  }

  private migrate(): void {
    this.db.exec(`
      CREATE TABLE IF NOT EXISTS tenants (
        id              TEXT PRIMARY KEY,
        wallet_address  TEXT NOT NULL UNIQUE,
        slug            TEXT NOT NULL UNIQUE,
        network         TEXT NOT NULL,
        created_at      INTEGER NOT NULL,
        status          TEXT NOT NULL DEFAULT 'active',
        fee_bps         INTEGER NOT NULL DEFAULT 100
      );

      CREATE TABLE IF NOT EXISTS routes (
        id          TEXT PRIMARY KEY,
        tenant_id   TEXT NOT NULL REFERENCES tenants(id) ON DELETE CASCADE,
        method      TEXT NOT NULL,
        path        TEXT NOT NULL,
        price_usd   TEXT NOT NULL,
        description TEXT,
        backend_url TEXT NOT NULL,
        created_at  INTEGER NOT NULL,
        UNIQUE (tenant_id, method, path)
      );

      CREATE TABLE IF NOT EXISTS events (
        id            TEXT PRIMARY KEY,
        tenant_id     TEXT NOT NULL REFERENCES tenants(id) ON DELETE CASCADE,
        route_id      TEXT REFERENCES routes(id),
        payer         TEXT,
        status        TEXT NOT NULL,
        amount_usd    TEXT,
        amount_atomic INTEGER,            -- USDC atomic units; lets SUM run in SQL
        tx_hash       TEXT,
        facilitator   TEXT,
        latency_ms    INTEGER,
        reason        TEXT,
        created_at    INTEGER NOT NULL
      );

      CREATE INDEX IF NOT EXISTS idx_events_tenant_time ON events(tenant_id, created_at DESC);
      CREATE INDEX IF NOT EXISTS idx_events_route ON events(tenant_id, route_id, created_at DESC);
    `);
    // Backfill amount_atomic for older rows that pre-date the column.
    try {
      this.db.exec(`ALTER TABLE events ADD COLUMN amount_atomic INTEGER`);
    } catch {
      // column already exists
    }
  }

  createTenant(args: {
    walletAddress: string;
    slug: string;
    network: string;
    feeBps?: number;
  }): Tenant {
    const id = ulid();
    const createdAt = Date.now();
    const feeBps = args.feeBps ?? 100;
    this.db
      .prepare(
        `INSERT INTO tenants (id, wallet_address, slug, network, created_at, status, fee_bps)
         VALUES (?, ?, ?, ?, ?, 'active', ?)`,
      )
      .run(id, args.walletAddress.toLowerCase(), args.slug, args.network, createdAt, feeBps);
    return {
      id,
      walletAddress: args.walletAddress.toLowerCase(),
      slug: args.slug,
      network: args.network,
      createdAt,
      status: "active",
      feeBps,
    };
  }

  getTenantById(id: string): Tenant | null {
    const row = this.db.prepare(`SELECT * FROM tenants WHERE id = ?`).get(id) as
      | TenantRow
      | undefined;
    return row ? rowToTenant(row) : null;
  }

  getTenantBySlug(slug: string): Tenant | null {
    const row = this.db.prepare(`SELECT * FROM tenants WHERE slug = ?`).get(slug) as
      | TenantRow
      | undefined;
    return row ? rowToTenant(row) : null;
  }

  getTenantByWallet(walletAddress: string): Tenant | null {
    const row = this.db
      .prepare(`SELECT * FROM tenants WHERE wallet_address = ?`)
      .get(walletAddress.toLowerCase()) as TenantRow | undefined;
    return row ? rowToTenant(row) : null;
  }

  setTenantStatus(id: string, status: Tenant["status"]): void {
    this.db.prepare(`UPDATE tenants SET status = ? WHERE id = ?`).run(status, id);
  }

  addRoute(args: {
    tenantId: string;
    method: string;
    path: string;
    priceUsd: string;
    description?: string | null;
    backendUrl: string;
  }): Route {
    const id = ulid();
    const createdAt = Date.now();
    this.db
      .prepare(
        `INSERT INTO routes (id, tenant_id, method, path, price_usd, description, backend_url, created_at)
         VALUES (?, ?, ?, ?, ?, ?, ?, ?)`,
      )
      .run(
        id,
        args.tenantId,
        args.method.toUpperCase(),
        args.path,
        args.priceUsd,
        args.description ?? null,
        args.backendUrl,
        createdAt,
      );
    return {
      id,
      tenantId: args.tenantId,
      method: args.method.toUpperCase(),
      path: args.path,
      priceUsd: args.priceUsd,
      description: args.description ?? null,
      backendUrl: args.backendUrl,
      createdAt,
    };
  }

  routesForTenant(tenantId: string): Route[] {
    const rows = this.db
      .prepare(`SELECT * FROM routes WHERE tenant_id = ? ORDER BY created_at ASC`)
      .all(tenantId) as RouteRow[];
    return rows.map(rowToRoute);
  }

  routeForRequest(tenantId: string, method: string, path: string): Route | null {
    const row = this.db
      .prepare(
        `SELECT * FROM routes WHERE tenant_id = ? AND method = ? AND path = ? LIMIT 1`,
      )
      .get(tenantId, method.toUpperCase(), path) as RouteRow | undefined;
    return row ? rowToRoute(row) : null;
  }

  recordEvent(e: Omit<EventRecord, "id" | "createdAt">): EventRecord {
    const id = ulid();
    const createdAt = Date.now();
    const amountAtomic = e.amountUsd ? usdToAtomic(e.amountUsd) : null;
    this.db
      .prepare(
        `INSERT INTO events (id, tenant_id, route_id, payer, status, amount_usd, amount_atomic, tx_hash, facilitator, latency_ms, reason, created_at)
         VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)`,
      )
      .run(
        id,
        e.tenantId,
        e.routeId,
        e.payer,
        e.status,
        e.amountUsd,
        amountAtomic,
        e.txHash,
        e.facilitator,
        e.latencyMs,
        e.reason,
        createdAt,
      );
    return { id, createdAt, ...e };
  }

  recentEvents(tenantId: string, limit = 50): EventRecord[] {
    const rows = this.db
      .prepare(
        `SELECT * FROM events WHERE tenant_id = ? ORDER BY rowid DESC LIMIT ?`,
      )
      .all(tenantId, limit) as EventRow[];
    return rows.map(rowToEvent);
  }

  tenantMetrics(tenantId: string): {
    totalRequests: number;
    paidRequests: number;
    rejectedRequests: number;
    totalRevenueUsd: string;
    uniquePayers: number;
  } {
    // Single SQL round-trip — counts and revenue sum together. The
    // COALESCE(SUM(...)) for legacy rows missing amount_atomic falls back to
    // re-parsing amount_usd (rare; only for events written before the column).
    const totals = this.db
      .prepare(
        `SELECT
           COUNT(*) AS total,
           COUNT(CASE WHEN status='paid' THEN 1 END) AS paid,
           COUNT(CASE WHEN status='rejected' THEN 1 END) AS rejected,
           COUNT(DISTINCT CASE WHEN status='paid' THEN payer END) AS unique_payers,
           COALESCE(SUM(CASE WHEN status='paid' THEN amount_atomic END), 0) AS revenue_atomic,
           COUNT(CASE WHEN status='paid' AND amount_atomic IS NULL AND amount_usd IS NOT NULL THEN 1 END) AS legacy_count
         FROM events WHERE tenant_id = ?`,
      )
      .get(tenantId) as {
      total: number;
      paid: number;
      rejected: number;
      unique_payers: number;
      revenue_atomic: number;
      legacy_count: number;
    };

    let revenueAtomic = BigInt(totals.revenue_atomic);
    if (totals.legacy_count > 0) {
      // Fallback path for rows from before amount_atomic existed.
      const legacy = this.db
        .prepare(
          `SELECT amount_usd FROM events
           WHERE tenant_id = ? AND status='paid' AND amount_atomic IS NULL AND amount_usd IS NOT NULL`,
        )
        .all(tenantId) as { amount_usd: string }[];
      for (const r of legacy) {
        revenueAtomic += BigInt(usdToAtomic(r.amount_usd) ?? 0);
      }
    }

    return {
      totalRequests: totals.total,
      paidRequests: totals.paid,
      rejectedRequests: totals.rejected,
      totalRevenueUsd: atomicToUsdString(revenueAtomic),
      uniquePayers: totals.unique_payers,
    };
  }

  close(): void {
    this.db.close();
  }
}

interface TenantRow {
  id: string;
  wallet_address: string;
  slug: string;
  network: string;
  created_at: number;
  status: Tenant["status"];
  fee_bps: number;
}

interface RouteRow {
  id: string;
  tenant_id: string;
  method: string;
  path: string;
  price_usd: string;
  description: string | null;
  backend_url: string;
  created_at: number;
}

interface EventRow {
  id: string;
  tenant_id: string;
  route_id: string | null;
  payer: string | null;
  status: EventRecord["status"];
  amount_usd: string | null;
  tx_hash: string | null;
  facilitator: string | null;
  latency_ms: number | null;
  reason: string | null;
  created_at: number;
}

function rowToTenant(r: TenantRow): Tenant {
  return {
    id: r.id,
    walletAddress: r.wallet_address,
    slug: r.slug,
    network: r.network,
    createdAt: r.created_at,
    status: r.status,
    feeBps: r.fee_bps,
  };
}

function rowToRoute(r: RouteRow): Route {
  return {
    id: r.id,
    tenantId: r.tenant_id,
    method: r.method,
    path: r.path,
    priceUsd: r.price_usd,
    description: r.description,
    backendUrl: r.backend_url,
    createdAt: r.created_at,
  };
}

function rowToEvent(r: EventRow): EventRecord {
  return {
    id: r.id,
    tenantId: r.tenant_id,
    routeId: r.route_id,
    payer: r.payer,
    status: r.status,
    amountUsd: r.amount_usd,
    txHash: r.tx_hash,
    facilitator: r.facilitator,
    latencyMs: r.latency_ms,
    reason: r.reason,
    createdAt: r.created_at,
  };
}
