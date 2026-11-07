import { cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, beforeEach, expect, it, vi } from "vitest";
import { PushNotificationSettings } from "@/components/settings/notifications/PushNotificationSettings";
import i18n from "@/i18n";

const state = vi.hoisted(() => ({ capabilities: ["webui.push.v1"], remote: false, mutation: vi.fn(), configuration: vi.fn() }));
vi.mock("@/providers/ClientProvider", () => ({ useClient: () => ({
  client: { requestMutation: state.mutation, gatewayOrigin: state.remote ? "https://other.test" : window.location.origin },
  token: "owner-token", webuiCapabilities: state.capabilities,
}) }));
vi.mock("@/lib/api", () => ({ fetchPushConfiguration: state.configuration }));

const subscription = {
  endpoint: "https://fcm.googleapis.com/fcm/send/device", options: {},
  toJSON: () => ({ endpoint: "https://fcm.googleapis.com/fcm/send/device", keys: {} }), unsubscribe: vi.fn(),
};
const subscribe = vi.fn();
const getSubscription = vi.fn();
const permission = vi.fn();

beforeEach(async () => {
  vi.clearAllMocks();
  await i18n.changeLanguage("en");
  state.capabilities = ["webui.push.v1"];
  state.remote = false;
  state.configuration.mockResolvedValue({ public_key: btoa("a".repeat(65)), registered: false });
  state.mutation.mockResolvedValue({ id: "device" });
  subscribe.mockResolvedValue(subscription);
  getSubscription.mockResolvedValue(null);
  subscription.unsubscribe.mockResolvedValue(true);
  permission.mockResolvedValue("granted");
  vi.stubGlobal("isSecureContext", true);
  vi.stubGlobal("PushManager", class {});
  vi.stubGlobal("Notification", { permission: "default", requestPermission: permission });
  Object.defineProperty(navigator, "serviceWorker", { configurable: true, value: {
    getRegistration: vi.fn().mockResolvedValue({ active: {}, pushManager: { getSubscription, subscribe } }),
  } });
});
afterEach(() => { cleanup(); vi.unstubAllGlobals(); });

it("registers this device with the authenticated host and sends its test through the server", async () => {
  render(<PushNotificationSettings />);
  await waitFor(() => expect(screen.getByRole("switch")).toBeEnabled());
  fireEvent.click(screen.getByRole("switch"));
  expect(permission).toHaveBeenCalledOnce();
  await waitFor(() => expect(screen.getByRole("switch")).toHaveAttribute("aria-checked", "true"));
  expect(subscribe).toHaveBeenCalledWith(expect.objectContaining({ userVisibleOnly: true }));
  expect(state.mutation).toHaveBeenCalledWith("settings.push.subscribe", expect.objectContaining({
    origin: window.location.origin, subscription: subscription.toJSON(),
    bodies: expect.objectContaining({ completed: "Task completed. Click to view the result." }),
  }));
  getSubscription.mockResolvedValue(subscription);
  fireEvent.click(screen.getByRole("button", { name: "Test notification" }));
  await waitFor(() => expect(state.mutation).toHaveBeenCalledWith("settings.push.test", expect.objectContaining({ id: expect.any(String) })));
  expect(screen.getByRole("status")).toHaveTextContent("Test queued");
});

it("does not offer background push on an older compatible host", () => {
  state.capabilities = ["webui.core.v1"];
  render(<PushNotificationSettings />);
  expect(screen.getByRole("switch")).toBeDisabled();
  expect(state.configuration).not.toHaveBeenCalled();
});

it("keeps the switch off when registration fails", async () => {
  state.mutation.mockRejectedValue(new Error("Disconnected"));
  render(<PushNotificationSettings />);
  await waitFor(() => expect(screen.getByRole("switch")).toBeEnabled());
  fireEvent.click(screen.getByRole("switch"));
  await waitFor(() => expect(screen.getByRole("status")).toHaveTextContent("Could not update notifications"));
  expect(screen.getByRole("switch")).toHaveAttribute("aria-checked", "false");
});

it("keeps device subscriptions separate from a gateway on another origin", () => {
  state.remote = true;
  render(<PushNotificationSettings />);
  expect(screen.getByRole("switch")).toBeDisabled();
  expect(state.configuration).not.toHaveBeenCalled();
});
