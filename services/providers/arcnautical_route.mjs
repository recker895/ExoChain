import { computeRoute } from '@arcnautical/maritime-routing';
import { readFileSync } from 'node:fs';

const { version } = JSON.parse(readFileSync(new URL('../../node_modules/@arcnautical/maritime-routing/package.json', import.meta.url), 'utf8'));
const [origin, destination, optionsJson] = process.argv.slice(2);
if (!origin || !destination) {
  throw new Error('Origin and destination UN/LOCODEs are required');
}
// The package logs grid loading; keep stdout a single machine-readable JSON value.
console.log = (...parts) => console.error(...parts);
const options = optionsJson ? JSON.parse(optionsJson) : undefined;
const result = computeRoute(origin, destination, options);
process.stdout.write(JSON.stringify({ package_version: version, result }));
