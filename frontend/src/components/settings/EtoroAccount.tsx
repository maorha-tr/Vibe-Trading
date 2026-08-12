import { useEffect, useState } from "react";
import { CircleUserRound, Loader2, LogIn, LogOut } from "lucide-react";
import { useTranslation } from "react-i18next";
import { toast } from "sonner";
import { api, type EtoroSsoStatus } from "@/lib/api";

/** "Sign in with eToro" account card for the Settings page.
 *
 * Self-contained like QVerisSettings: fetches its own status so it renders in
 * both the loading and loaded branches of Settings. Login is a top-level
 * navigation (OAuth redirect), not an XHR — the backend keeps the PKCE state.
 */
export function EtoroAccount() {
  const { t } = useTranslation();
  const [status, setStatus] = useState<EtoroSsoStatus | null>(null);
  const [loading, setLoading] = useState(true);
  const [busy, setBusy] = useState(false);

  const refresh = () => {
    api.getEtoroSsoStatus()
      .then(setStatus)
      .catch(() => setStatus(null))
      .finally(() => setLoading(false));
  };

  useEffect(() => {
    refresh();
    const params = new URLSearchParams(window.location.search);
    const outcome = params.get("etoro");
    if (outcome === "connected") {
      toast.success(t("settings.etoro.connected", { defaultValue: "Signed in with eToro." }));
    } else if (outcome === "error") {
      toast.error(
        t("settings.etoro.connectFailed", {
          defaultValue: "eToro sign-in failed: {{reason}}",
          reason: params.get("reason") || "unknown error",
        }),
      );
    }
    if (outcome) {
      params.delete("etoro");
      params.delete("reason");
      const query = params.toString();
      window.history.replaceState({}, "", `${window.location.pathname}${query ? `?${query}` : ""}`);
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  const disconnect = async () => {
    setBusy(true);
    try {
      await api.etoroSsoLogout();
      toast.success(t("settings.etoro.disconnected", { defaultValue: "eToro account disconnected." }));
      refresh();
    } catch (error) {
      toast.error(error instanceof Error ? error.message : String(error));
    } finally {
      setBusy(false);
    }
  };

  const signedIn = status?.connected === true;
  const usingKeys = status?.auth_mode === "keys";

  return (
    <section className="rounded-lg border bg-card p-5 shadow-sm">
      <div className="mb-4 space-y-1">
        <div className="flex items-center gap-2">
          <CircleUserRound className="h-4 w-4 text-primary" />
          <h2 className="text-base font-semibold">
            {t("settings.etoro.title", { defaultValue: "eToro account" })}
          </h2>
        </div>
        <p className="text-sm text-muted-foreground">
          {t("settings.etoro.description", {
            defaultValue:
              "Connect your eToro account to trade and pull market data — sign in with eToro (OAuth), or configure API keys via ETORO_API_KEY/ETORO_USER_KEY.",
          })}
        </p>
      </div>

      {loading ? (
        <div className="flex items-center text-sm text-muted-foreground">
          <Loader2 className="me-2 h-4 w-4 animate-spin" />
          {t("settings.loading")}
        </div>
      ) : signedIn ? (
        <div className="flex flex-wrap items-center justify-between gap-3">
          <div className="space-y-1 text-sm">
            <div className="font-medium">
              {status?.username
                ? t("settings.etoro.signedInAs", { defaultValue: "Signed in as {{name}}", name: status.username })
                : t("settings.etoro.signedIn", { defaultValue: "Signed in with eToro" })}
            </div>
            {status?.scopes && status.scopes.length > 0 && (
              <div className="text-xs text-muted-foreground">{status.scopes.join(" · ")}</div>
            )}
          </div>
          <button
            type="button"
            onClick={disconnect}
            disabled={busy}
            className="inline-flex items-center justify-center gap-2 rounded-md border px-3 py-2 text-sm text-muted-foreground transition hover:bg-muted hover:text-foreground disabled:cursor-not-allowed disabled:opacity-60"
          >
            {busy ? <Loader2 className="h-4 w-4 animate-spin" /> : <LogOut className="h-4 w-4" />}
            {t("settings.etoro.disconnect", { defaultValue: "Disconnect" })}
          </button>
        </div>
      ) : (
        <div className="flex flex-wrap items-center justify-between gap-3">
          <div className="space-y-1 text-sm text-muted-foreground">
            {usingKeys ? (
              <span>
                {t("settings.etoro.usingKeys", {
                  defaultValue: "Using API keys (x-api-key / x-user-key). Signing in with eToro is optional.",
                })}
              </span>
            ) : status?.client_configured ? (
              <span>{t("settings.etoro.notConnected", { defaultValue: "Not connected." })}</span>
            ) : (
              <span>
                {t("settings.etoro.notRegistered", {
                  defaultValue:
                    "OAuth app not registered yet — run agent/scripts/etoro_sso_setup.py once, then sign in here.",
                })}
              </span>
            )}
          </div>
          <a
            href={status?.login_url || "/auth/etoro/login?return_to=%2Fsettings"}
            aria-disabled={!status?.client_configured}
            onClick={(event) => {
              if (!status?.client_configured) event.preventDefault();
            }}
            className={`inline-flex items-center justify-center gap-2 rounded-md px-4 py-2 text-sm font-medium transition ${
              status?.client_configured
                ? "bg-primary text-primary-foreground hover:opacity-90"
                : "cursor-not-allowed border text-muted-foreground opacity-60"
            }`}
          >
            <LogIn className="h-4 w-4" />
            {t("settings.etoro.signIn", { defaultValue: "Sign in with eToro" })}
          </a>
        </div>
      )}
    </section>
  );
}
