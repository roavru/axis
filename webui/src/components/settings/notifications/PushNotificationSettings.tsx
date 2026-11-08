import { useEffect, useState } from "react";
import { useTranslation } from "react-i18next";
import { BellRing } from "lucide-react";
import { SettingsRow } from "@/components/settings/shared/SettingsControls";
import { ToggleButton } from "@/components/ui/toggle-button";
import { Button } from "@/components/ui/button";
import { useClient } from "@/providers/ClientProvider";
import { fetchPushConfiguration } from "@/lib/api";

type Feedback = {
  key: "pushFailed" | "pushQueued" | "denied" | "dismissed" | "workerUnavailable";
  detail?: string;
};

function failure(error: unknown): Feedback {
  return {
    key: "pushFailed",
    detail: error instanceof Error || error instanceof DOMException
      ? `${error.name}: ${error.message}` : undefined,
  };
}

async function deviceId(subscription: PushSubscription): Promise<string> {
  const bytes = await crypto.subtle.digest("SHA-256", new TextEncoder().encode(subscription.endpoint));
  return Array.from(new Uint8Array(bytes), byte => byte.toString(16).padStart(2, "0")).join("");
}

function publicKey(value: string): Uint8Array<ArrayBuffer> {
  const text = atob(value.replace(/-/g, "+").replace(/_/g, "/"));
  return Uint8Array.from(text, character => character.charCodeAt(0));
}

export function PushNotificationSettings() {
  const { client, token, webuiCapabilities } = useClient();
  const { t } = useTranslation();
  const supported = webuiCapabilities.includes("webui.push.v1") && window.isSecureContext
    && client.gatewayOrigin === window.location.origin
    && "serviceWorker" in navigator && "PushManager" in window && typeof Notification !== "undefined";
  const [enabled, setEnabled] = useState(false);
  const [busy, setBusy] = useState(false);
  const [loading, setLoading] = useState(true);
  const [feedback, setFeedback] = useState<Feedback | null>(null);
  useEffect(() => {
    let current = true;
    if (!supported) { setLoading(false); return; }
    const refresh = async () => {
      try {
        const registration = await navigator.serviceWorker.getRegistration();
        const subscription = await registration?.pushManager.getSubscription();
        const status = await fetchPushConfiguration(token, subscription ? await deviceId(subscription) : "");
        if (current) {
          setEnabled(!!subscription && status.registered && Notification.permission === "granted");
          setLoading(false);
        }
      } catch (error) {
        if (current) { setFeedback(failure(error)); setLoading(false); }
      }
    };
    void refresh();
    window.addEventListener("focus", refresh);
    return () => { current = false; window.removeEventListener("focus", refresh); };
  }, [supported, token]);

  async function toggle(value: boolean) {
    setBusy(true); setFeedback(null);
    try {
      // Request permission before the first await to preserve the button gesture on iOS.
      if (value && Notification.permission !== "granted") {
        const permission = await Notification.requestPermission();
        if (permission !== "granted") {
          setFeedback({ key: permission === "denied" ? "denied" : "dismissed" });
          return;
        }
      }
      const registration = await navigator.serviceWorker.getRegistration();
      if (!registration?.active) {
        setFeedback({ key: "workerUnavailable" });
        return;
      }
      let subscription = await registration.pushManager.getSubscription();
      if (value) {
        const configuration = await fetchPushConfiguration(token, "");
        const key = publicKey(configuration.public_key);
        if (subscription?.options.applicationServerKey
          && new Uint8Array(subscription.options.applicationServerKey).toString() !== key.toString()) {
          await subscription.unsubscribe(); subscription = null;
        }
        subscription ??= await registration.pushManager.subscribe({ userVisibleOnly: true, applicationServerKey: key });
        if (client.gatewayOrigin !== window.location.origin) throw new Error("host changed");
        await client.requestMutation("settings.push.subscribe", {
          subscription: subscription.toJSON(), origin: window.location.origin,
          bodies: { completed: t("settings.notifications.completed"), failed: t("settings.notifications.failed"),
            attention: t("settings.notifications.interrupted"), test: t("settings.notifications.testBody") },
        });
      } else if (subscription) {
        await client.requestMutation("settings.push.unsubscribe", { id: await deviceId(subscription) });
        await subscription.unsubscribe();
      }
      setEnabled(value);
    } catch (error) { setFeedback(failure(error)); }
    finally { setBusy(false); }
  }

  async function test() {
    setBusy(true); setFeedback(null);
    try {
      const subscription = await (await navigator.serviceWorker.getRegistration())?.pushManager.getSubscription();
      if (!subscription) throw new Error("subscription");
      await client.requestMutation("settings.push.test", { id: await deviceId(subscription) });
      setFeedback({ key: "pushQueued" });
    } catch (error) { setFeedback(failure(error)); }
    finally { setBusy(false); }
  }

  return <div>
    <SettingsRow title={t("settings.notifications.pushTitle")} description={t("settings.notifications.pushDescription")}>
      <>
        {enabled && <Button type="button" variant="ghost" size="icon" className="me-3" disabled={busy || loading}
          aria-label={t("settings.notifications.test")} onClick={() => void test()}><BellRing className="h-4 w-4" /></Button>}
        <ToggleButton checked={enabled} disabled={!supported || busy || loading} onChange={value => void toggle(value)}
          ariaLabel={t("settings.notifications.pushTitle")} label={t(enabled ? "settings.values.on" : "settings.values.off")} />
      </>
    </SettingsRow>
    {(!supported || feedback) && <p role="status" className="settings-list-inset pb-3 text-[12px] text-muted-foreground">
      {t(`settings.notifications.${!supported ? "pushUnsupported" : feedback?.key}`)}
      {supported && feedback?.detail && <span className="mt-1 block break-words font-mono">{feedback.detail}</span>}
    </p>}
  </div>;
}
