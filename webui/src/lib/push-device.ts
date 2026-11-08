export const PUSH_SUBSCRIPTION_EVENT = "nanobot:push-subscription";

export async function pushDeviceId(subscription: PushSubscription): Promise<string> {
  const bytes = await crypto.subtle.digest("SHA-256", new TextEncoder().encode(subscription.endpoint));
  return Array.from(new Uint8Array(bytes), byte => byte.toString(16).padStart(2, "0")).join("");
}
