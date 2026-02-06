#!/usr/bin/env node
/**
 * Collect union of all "packages" from final_packages.jsonl,
 * run npx pkg-stats for each, and save weekly downloads sorted by count.
 */

import fs from 'fs';
import path from 'path';
import { fileURLToPath } from 'url';
import { exec } from 'child_process';
import { promisify } from 'util';

const execAsync = promisify(exec);
const __dirname = path.dirname(fileURLToPath(import.meta.url));

const JSONL_PATH = path.join(__dirname, 'final_packages.jsonl');
const OUTPUT_PATH = path.join(__dirname, 'package_weekly_downloads.txt');
const CONCURRENCY = 5;

// 1. Build union set of all packages
const allPackages = new Set();
const lines = fs.readFileSync(JSONL_PATH, 'utf8').trim().split('\n');
for (const line of lines) {
  if (!line.trim()) continue;
  const row = JSON.parse(line);
  if (Array.isArray(row.packages)) {
    for (const pkg of row.packages) allPackages.add(pkg);
  }
}
let packages = [...allPackages].sort();
const limit = process.env.LIMIT ? parseInt(process.env.LIMIT, 10) : 0;
if (limit > 0) {
  packages = packages.slice(0, limit);
  console.error(`Union set: ${allPackages.size} total; running first ${packages.length} (LIMIT=${limit})`);
} else {
  console.error(`Union set: ${packages.length} unique packages`);
}

function runPkgStats(name) {
  return execAsync(`npx pkg-stats "${name}"`, {
    encoding: 'utf8',
    timeout: 60000,
    maxBuffer: 2 * 1024 * 1024,
  }).then(
    ({ stdout }) => {
      const m = stdout.match(/Total:\s*([\d,]+)\s+last week/i);
      return m ? parseInt(m[1].replace(/,/g, ''), 10) : 0;
    },
    (e) => {
      const combined = (e.stdout || '') + (e.stderr || '');
      const m = combined.match(/Total:\s*([\d,]+)\s+last week/i);
      return m ? parseInt(m[1].replace(/,/g, ''), 10) : 0;
    }
  );
}

async function runWithConcurrency(tasks, concurrency) {
  const results = [];
  let next = 0;
  async function worker() {
    while (next < tasks.length) {
      const i = next++;
      const name = tasks[i];
      const downloads = await runPkgStats(name);
      results[i] = { name, downloads };
      if (i % 50 === 0 || i === tasks.length - 1) {
        process.stderr.write(`Progress: ${next}/${tasks.length}\r`);
      }
    }
  }
  await Promise.all(Array.from({ length: concurrency }, worker));
  return results;
}

try {
  // 2. Run pkg-stats for each (parallel with limit)
  const results = await runWithConcurrency(packages, CONCURRENCY);
  console.error('');

  // 3. Sort by downloads descending, then by name
  results.sort((a, b) => {
    if (b.downloads !== a.downloads) return b.downloads - a.downloads;
    return a.name.localeCompare(b.name);
  });

  // 4. Write output file
  const linesOut = [
    '# package_name\tweekly_downloads',
    ...results.map((r) => `${r.name}\t${r.downloads}`),
  ];
  fs.writeFileSync(OUTPUT_PATH, linesOut.join('\n') + '\n', 'utf8');
  console.error(`Wrote ${OUTPUT_PATH}`);
} catch (e) {
  console.error(e);
  process.exit(1);
}
