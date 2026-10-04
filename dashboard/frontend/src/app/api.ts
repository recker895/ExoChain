export const API = process.env.NEXT_PUBLIC_API_URL ?? "http://127.0.0.1:8000";
export async function api<T>(
  path: string,
  token: string,
  body?: unknown,
): Promise<T> {
  const response = await fetch(API + path, {
    method: body === undefined ? "GET" : "POST",
    headers: {
      ...(token ? { Authorization: `Bearer ${token}` } : {}),
      ...(body !== undefined ? { "Content-Type": "application/json" } : {}),
    },
    body: body === undefined ? undefined : JSON.stringify(body),
  });
  if (!response.ok) {
    const result = await response
      .json()
      .catch(() => ({ detail: "Backend request failed" }));
    throw new Error(
      typeof result.detail === "string"
        ? result.detail
        : `Request rejected (${response.status}). Check the canonical data contract.`,
    );
  }
  return response.json();
}
export async function subscribe(
  path: string,
  token: string,
  signal: AbortSignal,
  onEvent: (value: unknown) => void,
) {
  const response = await fetch(API + path, {
    headers: token ? { Authorization: `Bearer ${token}` } : {},
    signal,
  });
  if (!response.ok || !response.body)
    throw new Error(`Stream unavailable (${response.status})`);
  const reader = response.body.getReader();
  const decoder = new TextDecoder();
  let buffer = "";
  while (!signal.aborted) {
    const { done, value } = await reader.read();
    if (done) return;
    buffer += decoder.decode(value, { stream: true });
    let end;
    while ((end = buffer.indexOf("\n\n")) !== -1) {
      const packet = buffer.slice(0, end);
      buffer = buffer.slice(end + 2);
      const line = packet.split("\n").find((l) => l.startsWith("data: "));
      if (line) onEvent(JSON.parse(line.slice(6)));
    }
  }
}
