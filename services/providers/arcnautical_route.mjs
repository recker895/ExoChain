import { computeRoute, PORTS, getPortByLocode } from '@arcnautical/maritime-routing';
import { readFileSync } from 'node:fs';

const { version } = JSON.parse(readFileSync(new URL('../../node_modules/@arcnautical/maritime-routing/package.json', import.meta.url), 'utf8'));
const catalog = JSON.parse(readFileSync(new URL('../../config/maritime_catalog.json', import.meta.url), 'utf8'));
// Extend/correct public port records BEFORE the package builds its lazy lookup.
// No node_modules patch, fabricated geometry or alias to an airport/city point.
for (const port of catalog.ports) {
  const record = { ...port, countryCode: port.locode.slice(0, 2), portType: 'container' };
  const index = PORTS.findIndex(p => p.locode === port.locode);
  if (index < 0) PORTS.push(record);
  else PORTS[index] = { ...PORTS[index], ...record };
}
const [origin, destination, optionsJson] = process.argv.slice(2);
if (origin === '--ports') {
  process.stdout.write(JSON.stringify(PORTS.filter(p => !catalog.blocked_legacy_codes[p.locode]).map(p => p.locode)));
  process.exit(0);
}
if (!origin || !destination) {
  throw new Error('Origin and destination UN/LOCODEs are required');
}
for (const code of [origin, destination]) {
  if (catalog.blocked_legacy_codes[code]) throw new Error(catalog.blocked_legacy_codes[code]);
  if (!getPortByLocode(code)) throw new Error(`Port ${code} is not supported by the geographic routing provider`);
}
if (origin === destination) throw new Error('Departure and destination ports must differ');
// The package logs grid loading; keep stdout a single machine-readable JSON value.
console.log = (...parts) => console.error(...parts);
const options = optionsJson ? JSON.parse(optionsJson) : undefined;
const result = computeRoute(origin, destination, options);
if (!result?.route_geojson?.features?.length) throw new Error('The routing provider could not find a supported sea route');
process.stdout.write(JSON.stringify({ package_version: version, result }));
