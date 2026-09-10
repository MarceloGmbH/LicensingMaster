/** Thin fetch wrapper for the licensing-master /admin API (same origin). */

async function req<T>(path: string, opts: RequestInit = {}): Promise<T> {
  const r = await fetch(path, {
    ...opts,
    headers: { "Content-Type": "application/json", ...(opts.headers || {}) },
    credentials: "include",
  });
  const body = await r.json().catch(() => ({}));
  if (!r.ok) {
    const e = body?.errors?.[0];
    throw new Error(e ? `${e.code}: ${e.message}` : `HTTP ${r.status}`);
  }
  return body.data as T;
}

export interface Product {
  product_id: number;
  code: string;
  name: string;
  is_active: boolean;
}

export interface TenantRow {
  tenant_id: number;
  product_code: string;
  slug: string;
  name: string;
  contact_email: string | null;
  status: string;
  seat_limit: number | null;
  seats_used: number;
  valid_until: string | null;
  subscription_status: string | null;
  is_paid: boolean | null;
}

export interface Subscription {
  subscription_id: number;
  plan_name: string;
  seat_limit: number;
  window_days: number;
  grace_days: number;
  valid_until: string;
  status: string;
  is_paid: boolean;
  seats_used: number;
  seats_available: number;
}

export interface BatchToken {
  batch_token_id: number;
  token: string;
  quota: number;
  seats_consumed: number;
  expires_at: string | null;
  is_revoked: boolean;
  note: string | null;
  created_at: string;
}

export interface Device {
  device_activation_id: number;
  hardware_uuid: string;
  device_name: string;
  branch_ref: string | null;
  state: string;
  days_remaining: number;
  valid_until: string | null;
  is_revoked: boolean;
  last_seen_at: string | null;
}

export interface Payment {
  payment_id: number;
  amount: string;
  currency: string;
  recorded_at: string;
  actor_email: string | null;
  note: string | null;
}

export interface TenantDetail {
  tenant: TenantRow & { product_code: string };
  subscription: Subscription;
  batch_tokens: BatchToken[];
  devices: Device[];
  payments: Payment[];
}

export const api = {
  products: () => req<{ items: Product[] }>("/admin/products").then((d) => d.items),
  tenants: (product?: string) =>
    req<{ items: TenantRow[] }>(`/admin/tenants${product ? `?product=${product}` : ""}`).then(
      (d) => d.items,
    ),
  tenant: (id: number) => req<TenantDetail>(`/admin/tenants/${id}`),
  createTenant: (body: Record<string, unknown>) =>
    req<TenantDetail>("/admin/tenants", { method: "POST", body: JSON.stringify(body) }),
  patchSubscription: (id: number, body: Record<string, unknown>) =>
    req<TenantDetail>(`/admin/tenants/${id}/subscription`, {
      method: "PATCH",
      body: JSON.stringify(body),
    }),
  genToken: (id: number, body: Record<string, unknown>) =>
    req<BatchToken>(`/admin/tenants/${id}/batch-tokens`, {
      method: "POST",
      body: JSON.stringify(body),
    }),
  revokeToken: (tokenId: number) =>
    req<BatchToken>(`/admin/batch-tokens/${tokenId}/revoke`, { method: "POST" }),
  recordPayment: (id: number, body: Record<string, unknown>) =>
    req<TenantDetail>(`/admin/tenants/${id}/payments`, {
      method: "POST",
      body: JSON.stringify(body),
    }),
  suspend: (id: number) => req<TenantDetail>(`/admin/tenants/${id}/suspend`, { method: "POST" }),
  reactivate: (id: number) =>
    req<TenantDetail>(`/admin/tenants/${id}/reactivate`, { method: "POST" }),
  mintServiceToken: (id: number, name: string) =>
    req<{ token: string }>(`/admin/tenants/${id}/service-tokens`, {
      method: "POST",
      body: JSON.stringify({ name }),
    }),
  revokeDevice: (hw: string) =>
    req<Device>(`/admin/devices/${encodeURIComponent(hw)}/revoke`, { method: "POST" }),
};

export function fmtDate(iso: string | null | undefined): string {
  if (!iso) return "—";
  const d = new Date(iso);
  return Number.isNaN(d.getTime())
    ? "—"
    : `${String(d.getDate()).padStart(2, "0")}/${String(d.getMonth() + 1).padStart(2, "0")}/${d.getFullYear()}`;
}

export function stateBadge(state: string): string {
  if (state === "ACTIVE") return "ok";
  if (state === "GRACE" || state === "PAST_DUE") return "warn";
  if (state === "EXPIRED" || state === "REVOKED" || state === "SUSPENDED") return "bad";
  return "dim";
}
