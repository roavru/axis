import { act, cleanup, renderHook } from "@testing-library/react";
import { afterEach, beforeEach, expect, it, vi } from "vitest";
import { usePushPresence } from "@/hooks/usePushPresence";
import type { NanobotClient } from "@/lib/nanobot-client";
import type { ConnectionStatus } from "@/lib/types";
import { PUSH_SUBSCRIPTION_EVENT } from "@/lib/push-device";

vi.mock("@/lib/push-device", async importOriginal => ({
  ...await importOriginal<typeof import("@/lib/push-device")>(),
  pushDeviceId: vi.fn(async () => "a".repeat(64)),
}));

const mutation = vi.fn();
const getSubscription = vi.fn();
const offStatus = vi.fn();
let visibility: DocumentVisibilityState;
let status: ConnectionStatus;
let statusChanged: (value: ConnectionStatus) => void;
let client: NanobotClient;

beforeEach(() => {
  vi.useFakeTimers();
  vi.clearAllMocks();
  visibility = "visible";
  status = "open";
  mutation.mockResolvedValue({});
  getSubscription.mockResolvedValue({ endpoint: "https://fcm.googleapis.com/device" });
  vi.stubGlobal("isSecureContext", true);
  vi.stubGlobal("Notification", { permission: "granted" });
  Object.defineProperty(document, "visibilityState", { configurable: true, get: () => visibility });
  Object.defineProperty(navigator, "serviceWorker", { configurable: true, value: {
    getRegistration: vi.fn(async () => ({ pushManager: { getSubscription } })),
    addEventListener: vi.fn(), removeEventListener: vi.fn(),
  } });
  client = {
    gatewayOrigin: window.location.origin,
    get status() { return status; },
    requestMutation: mutation,
    onStatus: (callback: typeof statusChanged) => {
      statusChanged = callback;
      callback(status);
      return offStatus;
    },
  } as unknown as NanobotClient;
});

afterEach(() => { cleanup(); vi.useRealTimers(); vi.unstubAllGlobals(); });

async function settle() {
  await act(async () => { await vi.advanceTimersByTimeAsync(0); });
}

it("reports foreground, background, renewal and reconnection for this device", async () => {
  const { unmount } = renderHook(() => usePushPresence(client, true));
  await settle();
  expect(mutation).toHaveBeenLastCalledWith("settings.push.presence", { id: "a".repeat(64), visible: true }, 5_000);
  mutation.mockClear();
  await act(async () => { await vi.advanceTimersByTimeAsync(15_000); });
  expect(mutation).toHaveBeenCalledOnce();
  visibility = "hidden";
  act(() => { document.dispatchEvent(new Event("visibilitychange")); });
  expect(mutation).toHaveBeenLastCalledWith("settings.push.presence", expect.objectContaining({ visible: false }), 5_000);
  mutation.mockClear();
  await act(async () => { await vi.advanceTimersByTimeAsync(30_000); });
  expect(mutation).not.toHaveBeenCalled();
  visibility = "visible";
  act(() => { document.dispatchEvent(new Event("visibilitychange")); });
  await settle();
  mutation.mockClear();
  act(() => { statusChanged("open"); });
  await settle();
  expect(mutation).toHaveBeenCalledWith("settings.push.presence", expect.objectContaining({ visible: true }), 5_000);
  act(() => { window.dispatchEvent(new Event("pagehide")); });
  expect(mutation).toHaveBeenLastCalledWith("settings.push.presence", expect.objectContaining({ visible: false }), 5_000);
  act(() => { window.dispatchEvent(new Event("pageshow")); });
  await settle();
  expect(mutation).toHaveBeenLastCalledWith("settings.push.presence", expect.objectContaining({ visible: true }), 5_000);
  unmount();
  expect(offStatus).toHaveBeenCalledOnce();
});

it("starts reporting when background notifications are enabled after opening settings", async () => {
  getSubscription.mockResolvedValue(null);
  renderHook(() => usePushPresence(client, true));
  await settle();
  expect(mutation).not.toHaveBeenCalled();
  getSubscription.mockResolvedValue({ endpoint: "https://fcm.googleapis.com/device" });
  act(() => { window.dispatchEvent(new Event(PUSH_SUBSCRIPTION_EVENT)); });
  await settle();
  expect(mutation).toHaveBeenCalledWith("settings.push.presence", expect.objectContaining({ visible: true }), 5_000);
});

it.each(["older host", "another origin"])("leaves push delivery unchanged on %s", async scenario => {
  if (scenario === "another origin") Object.defineProperty(client, "gatewayOrigin", { value: "https://other.test" });
  renderHook(() => usePushPresence(client, scenario !== "older host"));
  await settle();
  expect(getSubscription).not.toHaveBeenCalled();
  expect(mutation).not.toHaveBeenCalled();
});
