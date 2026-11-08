import { useEffect } from "react";
import type { NanobotClient } from "@/lib/nanobot-client";
import { PUSH_SUBSCRIPTION_EVENT, pushDeviceId } from "@/lib/push-device";

export function usePushPresence(client: NanobotClient, enabled: boolean) {
  const origin = client.gatewayOrigin;
  useEffect(() => {
    if (!enabled || !window.isSecureContext || origin !== window.location.origin) return;
    let stopped = false;
    let pageActive = true;
    let generation = 0;
    let identifier: string | null = null;
    const send = (visible: boolean) => {
      if (!stopped && client.status === "open" && client.gatewayOrigin === window.location.origin) {
        void client.requestMutation("settings.push.presence", { id: identifier, visible }, 5_000).catch(() => {});
      }
    };
    const visible = () => pageActive && document.visibilityState === "visible";
    const refresh = async () => {
      const current = ++generation;
      try {
        const registration = await navigator.serviceWorker?.getRegistration();
        const subscription = await registration?.pushManager.getSubscription();
        const next = subscription ? await pushDeviceId(subscription) : null;
        if (stopped || current !== generation) return;
        if (identifier && next !== identifier) send(false);
        identifier = next;
      } catch { /* A browser without push can still report that the user is reading. */ }
      if (!stopped && current === generation) send(visible());
    };
    const visibilityChanged = () => {
      // Send immediately: mobile pages may be suspended before async lookups finish.
      send(visible());
      if (visible()) void refresh();
    };
    const hidden = () => { pageActive = false; generation++; send(false); };
    const refreshed = () => { void refresh(); };
    const shown = () => { pageActive = true; void refresh(); };
    const unsubscribe = client.onStatus(status => { if (status === "open") void refresh(); });
    const timer = window.setInterval(() => { if (visible()) void refresh(); }, 15_000);
    document.addEventListener("visibilitychange", visibilityChanged);
    window.addEventListener("pagehide", hidden);
    window.addEventListener("pageshow", shown);
    window.addEventListener("focus", visibilityChanged);
    window.addEventListener(PUSH_SUBSCRIPTION_EVENT, refreshed);
    navigator.serviceWorker?.addEventListener("controllerchange", refreshed);
    return () => {
      send(false);
      stopped = true;
      generation++;
      unsubscribe();
      window.clearInterval(timer);
      document.removeEventListener("visibilitychange", visibilityChanged);
      window.removeEventListener("pagehide", hidden);
      window.removeEventListener("pageshow", shown);
      window.removeEventListener("focus", visibilityChanged);
      window.removeEventListener(PUSH_SUBSCRIPTION_EVENT, refreshed);
      navigator.serviceWorker?.removeEventListener("controllerchange", refreshed);
    };
  }, [client, enabled, origin]);
}
