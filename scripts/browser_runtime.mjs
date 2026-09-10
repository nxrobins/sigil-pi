// Development evidence only. No production API, policy or agent execution here.
import {access, readFile} from 'node:fs/promises';
import {constants} from 'node:fs';
import {createRequire} from 'node:module';
import path from 'node:path';

try {
  const manifest = JSON.parse(await readFile(new URL('../package.json', import.meta.url), 'utf8'));
  const expected = manifest.devDependencies?.playwright;
  if (!/^\d+\.\d+\.\d+$/.test(expected || '')) throw new Error('Playwright must have an exact development dependency pin.');
  if (Number(process.versions.node.split('.')[0]) < 20) throw new Error('Browser checks require Node.js 20 or later.');
  const require = createRequire(import.meta.url);
  const override = process.env.PI_PLAYWRIGHT_DIR;
  if (override !== undefined && !path.isAbsolute(override)) throw new Error('PI_PLAYWRIGHT_DIR must be an absolute installed-package directory.');
  const entry = override || 'playwright';
  const metadata = override ? path.join(override, 'package.json') : 'playwright/package.json';
  const installed = require(metadata);
  if (installed.version !== expected) throw new Error('Installed Playwright does not match package.json; run npm ci --ignore-scripts.');
  const dependencyRequire = createRequire(require.resolve(metadata));
  if (dependencyRequire('playwright-core/package.json').version !== expected) throw new Error('Installed playwright-core does not match the exact dependency pin.');
  const executable = require(entry).chromium.executablePath();
  await access(executable, constants.X_OK);
  console.log(JSON.stringify({node: process.versions.node, playwright: installed.version, chromium: executable}));
} catch (error) {
  console.error(JSON.stringify({error: 'browser_runtime_unavailable', detail: error.message,
    setup: 'Install the pinned development packages with npm ci --ignore-scripts, then npx --no-install playwright install chromium (CI also uses --with-deps).'}));
  process.exitCode = 1;
}
