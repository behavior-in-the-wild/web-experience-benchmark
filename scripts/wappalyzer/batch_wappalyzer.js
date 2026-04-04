// batch_wappalyzer.js
const fs = require("fs");
const path = require("path");
const { spawn } = require("child_process");

function sleep(ms) {
  return new Promise((r) => setTimeout(r, ms));
}

function normalizeUrl(u) {
  u = (u || "").trim();
  if (!u) return "";
  if (u.startsWith("#")) return ""; // comment line
  if (!/^https?:\/\//i.test(u)) u = "https://" + u;
  return u;
}

function readUrls(filePath) {
  const txt = fs.readFileSync(filePath, "utf8");
  return txt
    .split(/\r?\n/)
    .map(normalizeUrl)
    .filter(Boolean);
}

function safeJsonParse(maybeJsonText) {
  const t = (maybeJsonText || "").trim();

  // Try direct parse first
  try {
    return { ok: true, value: JSON.parse(t) };
  } catch {}

  // Fallback: extract first {...} block from noisy output
  const i = t.indexOf("{");
  const j = t.lastIndexOf("}");
  if (i >= 0 && j > i) {
    try {
      return { ok: true, value: JSON.parse(t.slice(i, j + 1)) };
    } catch (e) {
      return { ok: false, error: e.message };
    }
  }
  return { ok: false, error: "No JSON object found in stdout" };
}

function computeStats(parsed) {
  // Wappalyzer outputs usually: { technologies: [ ... ] } (sometimes nested)
  let techs = [];
  if (Array.isArray(parsed?.technologies)) techs = parsed.technologies;
  else if (Array.isArray(parsed?.results?.technologies)) techs = parsed.results.technologies;

  const technology_names = techs.map((t) => t?.name).filter(Boolean);
  const category_counts = {};

  for (const t of techs) {
    const cats = Array.isArray(t?.categories) ? t.categories : [];
    for (const c of cats) {
      const name = c?.name || c?.slug || c?.id;
      if (!name) continue;
      category_counts[name] = (category_counts[name] || 0) + 1;
    }
  }

  return {
    technologies_count: technology_names.length,
    technology_names,
    category_counts,
  };
}

function runCliOnce({ cliPath, url, timeoutMs = 120000 }) {
  return new Promise((resolve) => {
    const nodeExe = process.execPath;

    // IMPORTANT: do not use shell:true (can break quoting on Windows)
    const child = spawn(nodeExe, [cliPath, url], {
      cwd: process.cwd(),
      windowsHide: true,
    });

    let stdout = "";
    let stderr = "";

    const killTimer = setTimeout(() => {
      try {
        child.kill("SIGKILL");
      } catch {}
    }, timeoutMs);

    child.stdout.on("data", (d) => (stdout += d.toString()));
    child.stderr.on("data", (d) => (stderr += d.toString()));

    child.on("close", (code) => {
      clearTimeout(killTimer);
      resolve({ code, stdout, stderr });
    });
  });
}

async function main() {
  const urlsFile = process.argv[2] || "urls.txt";
  const outFile = process.argv[3] || "wappalyzer_results.json";
  const delayMs = Number(process.argv[4] || 800); // throttle between runs

  const cliPath = path.join(process.cwd(), "src", "drivers", "npm", "cli.js");
  if (!fs.existsSync(cliPath)) {
    console.error("Cannot find CLI at:", cliPath);
    console.error("Run link step first:\n  node .\\bin\\link.js\n  node .\\bin\\manifest.js v3");
    process.exit(1);
  }

  if (!fs.existsSync(urlsFile)) {
    console.error("Missing urls file:", urlsFile);
    process.exit(1);
  }

  const urls = readUrls(urlsFile);

  // Resume support: load existing results and skip completed URLs
  let results = [];
  const done = new Set();
  if (fs.existsSync(outFile)) {
    try {
      results = JSON.parse(fs.readFileSync(outFile, "utf8"));
      for (const r of results) if (r?.url) done.add(r.url);
    } catch {
      // If file is corrupt, start fresh (we still also log to NDJSON)
      results = [];
    }
  }

  const ndjsonFile = outFile.replace(/\.json$/i, "") + ".ndjson";

  console.log(`URLs: ${urls.length} | Already done: ${done.size}`);
  console.log(`Output: ${outFile} (+ ${ndjsonFile})`);

  for (const url of urls) {
    if (done.has(url)) {
      console.log(`[SKIP] ${url}`);
      continue;
    }

    const started = Date.now();
    const { code, stdout, stderr } = await runCliOnce({ cliPath, url });
    const duration_ms = Date.now() - started;

    const parsedAttempt = safeJsonParse(stdout);
    const record = {
      url,
      ts: new Date().toISOString(),
      duration_ms,
      exit_code: code,
      ok: code === 0 && parsedAttempt.ok,
      parse_error: parsedAttempt.ok ? null : parsedAttempt.error,
      stderr: (stderr || "").trim() || null,
      data: parsedAttempt.ok ? parsedAttempt.value : null,
      stats: parsedAttempt.ok ? computeStats(parsedAttempt.value) : null,
    };

    results.push(record);
    done.add(url);

    // Persist after each URL (safe for long runs)
    fs.writeFileSync(outFile, JSON.stringify(results, null, 2));
    fs.appendFileSync(ndjsonFile, JSON.stringify(record) + "\n");

    console.log(`[${record.ok ? "OK" : "FAIL"}] ${url} (${duration_ms} ms)`);

    // Be polite to sites + avoid stressing Chrome
    await sleep(delayMs);
  }

  console.log("Done.");
}

main().catch((e) => {
  console.error("Fatal:", e);
  process.exit(1);
});