import catalog from "../../../../config/maritime_catalog.json";
import type { RequestSpec } from "./types";

export { catalog };
export const portLabel = (code: string) => {
  if (!code || code === "—") return "Port not recorded";
  const port = catalog.ports.find(p => p.locode === code.toUpperCase());
  return port ? `${port.name} · ${port.locode}` : `${code.toUpperCase()} · custom supported port`;
};

export function maritimeInput(request: RequestSpec, departure: string, arrival: string, speed: number) {
  const origin = departure.trim().toUpperCase(), destination = arrival.trim().toUpperCase();
  if (![origin, destination].every(code => /^[A-Z]{2}[A-Z0-9]{3}$/.test(code)))
    throw new Error("Choose two ports, or enter valid five-character custom UN/LOCODEs.");
  if (origin === destination) throw new Error("Departure and destination ports must differ.");
  for (const code of [origin, destination]) {
    const reason = (catalog.blocked_legacy_codes as Record<string, string>)[code];
    if (reason) throw new Error(reason);
  }
  if (!Number.isFinite(speed) || speed < 1 || speed > 40) throw new Error("Planning speed must be between 1 and 40 knots.");
  const reference = request.vessel_reference;
  if (!reference || !/^\d{7}$/.test(reference.imo || "") || !/^\d{9}$/.test(reference.mmsi))
    throw new Error("Choose a vessel with a sourced IMO and MMSI.");
  const shipment = `MARITIME-${origin}-${destination}-${reference.imo}`;
  return {
    request: { ...request, shipment_ids: [shipment], vessel_reference: { ...reference, shipment_id: shipment } },
    arcnautical_route: { origin_locode: origin, destination_locode: destination, shipment_id: shipment, planning_speed_knots: speed },
  };
}
