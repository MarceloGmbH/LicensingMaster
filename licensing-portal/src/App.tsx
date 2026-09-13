import React, { useCallback, useEffect, useState } from "react";
import {
  api,
  fmtDate,
  stateBadge,
  type Product,
  type TenantDetail,
  type TenantRow,
} from "./api";

export const App: React.FC = () => {
  const [products, setProducts] = useState<Product[]>([]);
  const [active, setActive] = useState<string>("");
  const [tenants, setTenants] = useState<TenantRow[]>([]);
  const [openId, setOpenId] = useState<number | null>(null);
  const [err, setErr] = useState<string | null>(null);
  const [me, setMe] = useState<string>("");

  useEffect(() => {
    api.products().then((p) => {
      setProducts(p);
      setActive(p[0]?.code ?? "");
    }).catch((e) => setErr(String(e.message)));
    fetch("/admin/me", { credentials: "include" })
      .then((r) => (r.ok ? r.json() : null))
      .then((b) => b?.data?.email && setMe(b.data.email))
      .catch(() => {});
  }, []);

  const loadTenants = useCallback(() => {
    if (!active) return;
    api.tenants(active).then(setTenants).catch((e) => setErr(String(e.message)));
  }, [active]);

  useEffect(() => {
    loadTenants();
  }, [loadTenants]);

  return (
    <div className="wrap">
      <header className="top">
        <h1>Gestor de Licencias</h1>
        <span className="muted">licensing.alanadev.com</span>
        <span className="who">{me || "operador"}</span>
      </header>

      {err && <div className="err">{err} <button onClick={() => setErr(null)}>✕</button></div>}

      <div className="tabs">
        {products.map((p) => (
          <button
            key={p.code}
            className={p.code === active ? "active" : ""}
            onClick={() => {
              setActive(p.code);
              setOpenId(null);
            }}
          >
            {p.name}
          </button>
        ))}
      </div>

      {openId != null ? (
        <TenantView
          tenantId={openId}
          onBack={() => {
            setOpenId(null);
            loadTenants();
          }}
          onError={setErr}
        />
      ) : (
        <TenantList
          productCode={active}
          tenants={tenants}
          onOpen={setOpenId}
          onCreated={() => loadTenants()}
          onError={setErr}
        />
      )}
    </div>
  );
};

// --- list + create ------------------------------------------------------

const TenantList: React.FC<{
  productCode: string;
  tenants: TenantRow[];
  onOpen: (id: number) => void;
  onCreated: () => void;
  onError: (m: string) => void;
}> = ({ productCode, tenants, onOpen, onCreated, onError }) => {
  const [slug, setSlug] = useState("");
  const [name, setName] = useState("");
  const [email, setEmail] = useState("");
  const [seats, setSeats] = useState("5");
  const [baseUrl, setBaseUrl] = useState("");
  const [busy, setBusy] = useState(false);

  const create = async () => {
    if (!slug.trim() || !name.trim() || !baseUrl.trim()) return;
    setBusy(true);
    try {
      await api.createTenant({
        product_code: productCode,
        slug: slug.trim(),
        name: name.trim(),
        contact_email: email.trim() || null,
        seat_limit: Number(seats) || 1,
        base_url: baseUrl.trim(),
      });
      setSlug("");
      setName("");
      setEmail("");
      setBaseUrl("");
      onCreated();
    } catch (e) {
      onError(String((e as Error).message));
    } finally {
      setBusy(false);
    }
  };

  return (
    <>
      <div className="panel">
        <h2>Clientes — {productCode}</h2>
        <table>
          <thead>
            <tr>
              <th>Cliente</th>
              <th>Slug</th>
              <th>Estado</th>
              <th className="right">Asientos</th>
              <th>Vence</th>
              <th>Pago</th>
            </tr>
          </thead>
          <tbody>
            {tenants.length === 0 && (
              <tr>
                <td colSpan={6} className="muted">Sin clientes para este producto.</td>
              </tr>
            )}
            {tenants.map((t) => (
              <tr key={t.tenant_id} className="clickable" onClick={() => onOpen(t.tenant_id)}>
                <td><strong>{t.name}</strong></td>
                <td className="mono">{t.slug}</td>
                <td><span className={`badge ${stateBadge(t.status)}`}>{t.status}</span></td>
                <td className="right mono">
                  {t.seats_used} / {t.seat_limit ?? "—"}
                </td>
                <td className="mono">{fmtDate(t.valid_until)}</td>
                <td>
                  {t.is_paid == null ? "—" : t.is_paid ? (
                    <span className="badge ok">al día</span>
                  ) : (
                    <span className="badge bad">pendiente</span>
                  )}
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>

      <div className="panel">
        <h2>Nuevo cliente ({productCode})</h2>
        <div className="row">
          <label className="f">
            Slug
            <input value={slug} onChange={(e) => setSlug(e.target.value)} placeholder="farmacen" />
          </label>
          <label className="f">
            Razón social
            <input value={name} onChange={(e) => setName(e.target.value)} placeholder="Farmacen S.R.L." />
          </label>
          <label className="f">
            Email de contacto
            <input value={email} onChange={(e) => setEmail(e.target.value)} placeholder="admin@farmacen.bo" />
          </label>
          <label className="f">
            Asientos
            <input value={seats} onChange={(e) => setSeats(e.target.value)} style={{ width: 64 }} />
          </label>
          <label className="f">
            Base URL
            <input
              value={baseUrl}
              onChange={(e) => setBaseUrl(e.target.value)}
              type="url"
              placeholder="https://backend-cliente.com"
              required
            />
          </label>
          <button className="primary" disabled={busy || !slug.trim() || !name.trim() || !baseUrl.trim()} onClick={create}>
            {busy ? "Creando…" : "Crear cliente"}
          </button>
        </div>
      </div>
    </>
  );
};

// --- detail -----------------------------------------------------------

const TenantView: React.FC<{
  tenantId: number;
  onBack: () => void;
  onError: (m: string) => void;
}> = ({ tenantId, onBack, onError }) => {
  const [d, setD] = useState<TenantDetail | null>(null);
  const [mintedToken, setMintedToken] = useState<string | null>(null);
  const [serviceToken, setServiceToken] = useState<string | null>(null);
  const [quota, setQuota] = useState("2");
  const [payAmount, setPayAmount] = useState("");
  const [seatEdit, setSeatEdit] = useState("");
  const [baseUrlEdit, setBaseUrlEdit] = useState("");
  const [showDeleteModal, setShowDeleteModal] = useState(false);
  const [deletePassword, setDeletePassword] = useState("");
  const [deleteBusy, setDeleteBusy] = useState(false);
  const [deleteError, setDeleteError] = useState<string | null>(null);

  const reload = useCallback(() => {
    api.tenant(tenantId).then((x) => {
      setD(x);
      setSeatEdit(String(x.subscription.seat_limit));
      setBaseUrlEdit(x.tenant.base_url || "");
    }).catch((e) => onError(String(e.message)));
  }, [tenantId, onError]);

  useEffect(() => {
    reload();
  }, [reload]);

  if (!d) return <div className="panel">Cargando…</div>;
  const s = d.subscription;
  const pct = s.seat_limit > 0 ? Math.min(100, Math.round((s.seats_used / s.seat_limit) * 100)) : 0;

  const guard = async (fn: () => Promise<unknown>) => {
    try {
      await fn();
      reload();
    } catch (e) {
      onError(String((e as Error).message));
    }
  };

  const handleDelete = async () => {
    if (!deletePassword.trim()) {
      setDeleteError("La contraseña de administrador es requerida");
      return;
    }
    setDeleteBusy(true);
    setDeleteError(null);
    try {
      await api.deleteTenant(tenantId, deletePassword.trim());
      onBack();
    } catch (e) {
      setDeleteError(String((e as Error).message));
    } finally {
      setDeleteBusy(false);
    }
  };

  return (
    <>
      <button className="back" onClick={onBack}>← Volver a {d.tenant.product_code}</button>
      <div className="panel">
        <h2>
          {d.tenant.name} <span className="mono muted">/{d.tenant.slug}</span>{" "}
          <span className={`badge ${stateBadge(d.tenant.status)}`}>{d.tenant.status}</span>
        </h2>
        <div className="row">
          {d.tenant.status === "SUSPENDED" ? (
            <button className="primary" onClick={() => guard(() => api.reactivate(tenantId))}>
              Reactivar cliente
            </button>
          ) : (
            <button className="danger" onClick={() => guard(() => api.suspend(tenantId))}>
              Suspender cliente
            </button>
          )}
          <button
            onClick={() =>
              guard(async () => {
                const r = await api.mintServiceToken(tenantId, `${d.tenant.slug}-backend`);
                setServiceToken(r.token);
              })
            }
          >
            Emitir service token
          </button>
          <button className="danger" onClick={() => setShowDeleteModal(true)}>
            Eliminar cliente
          </button>
        </div>
        {serviceToken && (
          <div className="tokline">
            {serviceToken}
            <span className="muted">— guárdelo, no se vuelve a mostrar</span>
          </div>
        )}
      </div>

      <div className="grid">
        <div className="panel">
          <h2>Suscripción y cupo</h2>
          <div className="kv">
            <span>Estado</span>
            <span>
              <span className={`badge ${stateBadge(s.status)}`}>{s.status}</span>{" "}
              {s.is_paid ? "· al día" : "· pago pendiente"}
            </span>
          </div>
          <div className="kv"><span>Plan</span><span>{s.plan_name}</span></div>
          <div className="kv"><span>Vence</span><span className="mono">{fmtDate(s.valid_until)}</span></div>
          <div className="kv"><span>Ventana / gracia</span><span>{s.window_days} / {s.grace_days} días</span></div>
          <div className="kv">
            <span>Asientos</span>
            <span className="mono">{s.seats_used} / {s.seat_limit} ({s.seats_available} libres)</span>
          </div>
          <div className="bar"><i style={{ width: `${pct}%` }} /></div>
          <div className="row" style={{ marginTop: 8 }}>
            <label className="f">
              Cupo
              <input value={seatEdit} onChange={(e) => setSeatEdit(e.target.value)} style={{ width: 64 }} />
            </label>
            <button
              onClick={() =>
                guard(() => api.patchSubscription(tenantId, { seat_limit: Number(seatEdit) || 1 }))
              }
            >
              Guardar cupo
            </button>
          </div>
        </div>

        <div className="panel">
          <h2>Base URL</h2>
          <div className="row" style={{ alignItems: "flex-end" }}>
            <label className="f" style={{ flex: 1 }}>
              Base URL
              <input
                value={baseUrlEdit}
                onChange={(e) => setBaseUrlEdit(e.target.value)}
                type="url"
                placeholder="https://backend-cliente.com"
              />
            </label>
            <button
              disabled={!baseUrlEdit.trim()}
              onClick={() =>
                guard(() => api.patchTenant(tenantId, { base_url: baseUrlEdit.trim() }))
              }
            >
              Guardar
            </button>
          </div>
          {!baseUrlEdit && <p className="muted" style={{ marginTop: 8 }}>Sin Base URL configurada</p>}
        </div>
      </div>

      <div className="panel">
        <h2>Registrar pago</h2>
        <p className="muted" style={{ marginTop: 0 }}>
          Empuja <code>valid_until</code> +{s.window_days} días y marca la suscripción al día.
        </p>
        <div className="row">
          <label className="f">
            Monto
            <input value={payAmount} onChange={(e) => setPayAmount(e.target.value)} placeholder="350.00" />
          </label>
          <button
            className="primary"
            disabled={!payAmount.trim()}
            onClick={() =>
              guard(async () => {
                await api.recordPayment(tenantId, { amount: payAmount.trim() });
                setPayAmount("");
              })
            }
          >
            Registrar
          </button>
        </div>
        <table style={{ marginTop: 10 }}>
          <thead>
            <tr><th>Fecha</th><th className="right">Monto</th><th>Nota</th></tr>
          </thead>
          <tbody>
            {d.payments.length === 0 && (
              <tr><td colSpan={3} className="muted">Sin pagos registrados.</td></tr>
            )}
            {d.payments.map((p) => (
              <tr key={p.payment_id}>
                <td className="mono">{fmtDate(p.recorded_at)}</td>
                <td className="right mono">{p.amount} {p.currency}</td>
                <td className="muted">{p.note ?? "—"}</td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>

      <div className="panel">
        <h2>Tokens de activación (batch)</h2>
        <div className="row" style={{ marginBottom: 10 }}>
          <label className="f">
            Cupo del token
            <input value={quota} onChange={(e) => setQuota(e.target.value)} style={{ width: 72 }} />
          </label>
          <button
            className="primary"
            onClick={() =>
              guard(async () => {
                const r = await api.genToken(tenantId, { quota: Number(quota) || 1 });
                setMintedToken(r.token);
              })
            }
          >
            Generar token
          </button>
        </div>
        {mintedToken && <div className="tokline">{mintedToken}</div>}
        <table>
          <thead>
            <tr><th>Token</th><th className="right">Cupo</th><th className="right">Consumidos</th><th>Estado</th><th>Nota</th><th /></tr>
          </thead>
          <tbody>
            {d.batch_tokens.length === 0 && (
              <tr><td colSpan={6} className="muted">Sin tokens generados.</td></tr>
            )}
            {d.batch_tokens.map((t) => (
              <tr key={t.batch_token_id}>
                <td className="mono">{t.token}</td>
                <td className="right mono">{t.quota}</td>
                <td className="right mono">{t.seats_consumed}</td>
                <td>
                  {t.is_revoked ? (
                    <span className="badge bad">revocado</span>
                  ) : (
                    <span className="badge ok">activo</span>
                  )}
                </td>
                <td className="muted">{t.note ?? "—"}</td>
                <td className="right">
                  {!t.is_revoked && (
                    <button className="danger" onClick={() => guard(() => api.revokeToken(t.batch_token_id))}>
                      Revocar
                    </button>
                  )}
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>

      <div className="panel">
        <h2>Dispositivos vinculados</h2>
        <table>
          <thead>
            <tr>
              <th>Terminal</th>
              <th>hardware_uuid</th>
              <th>Sucursal</th>
              <th>Estado</th>
              <th>Vence</th>
              <th>Último latido</th>
              <th />
            </tr>
          </thead>
          <tbody>
            {d.devices.length === 0 && (
              <tr><td colSpan={7} className="muted">Sin dispositivos vinculados.</td></tr>
            )}
            {d.devices.map((dev) => (
              <tr key={dev.device_activation_id}>
                <td><strong>{dev.device_name}</strong></td>
                <td className="mono" style={{ maxWidth: 220, overflow: "hidden", textOverflow: "ellipsis" }}>
                  {dev.hardware_uuid}
                </td>
                <td className="muted">{dev.branch_ref ?? "—"}</td>
                <td><span className={`badge ${stateBadge(dev.state)}`}>{dev.state}</span></td>
                <td className="mono">{fmtDate(dev.valid_until)}</td>
                <td className="mono">{fmtDate(dev.last_seen_at)}</td>
                <td className="right">
                  {!dev.is_revoked && (
                    <button
                      className="danger"
                      onClick={() => guard(() => api.revokeDevice(dev.hardware_uuid))}
                    >
                      Revocar
                    </button>
                  )}
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>

      {showDeleteModal && (
        <div className="modal-overlay" onClick={() => setShowDeleteModal(false)}>
          <div className="modal" onClick={(e) => e.stopPropagation()}>
            <h3>Eliminar cliente</h3>
            <p className="muted">
              Esta acción es irreversible. Se eliminará el cliente <strong>{d.tenant.name}</strong>
              ({d.tenant.slug}) y todos sus datos asociados.
            </p>
            <label className="f" style={{ marginTop: 16, display: "block" }}>
              Contraseña de administrador
              <input
                type="password"
                value={deletePassword}
                onChange={(e) => setDeletePassword(e.target.value)}
                placeholder="Ingrese la contraseña de admin"
                autoFocus
              />
            </label>
            {deleteError && <div className="err" style={{ marginTop: 12 }}>{deleteError}</div>}
            <div className="row" style={{ marginTop: 16, justifyContent: "flex-end" }}>
              <button onClick={() => { setShowDeleteModal(false); setDeletePassword(""); setDeleteError(null); }}>
                Cancelar
              </button>
              <button
                className="danger"
                disabled={deleteBusy}
                onClick={handleDelete}
              >
                {deleteBusy ? "Eliminando…" : "Eliminar"}
              </button>
            </div>
          </div>
        </div>
      )}
    </>
  );
};
