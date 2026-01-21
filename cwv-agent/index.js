import dotenv from 'dotenv';
import path from 'path';
import { fileURLToPath } from 'url';
import { parseArguments } from './src/cli/cli.js';
import { loadUrls } from './src/cli/urlLoader.js';
import { processUrl } from './src/core/actions.js';

// Load environment variables - parent .env first (has real API keys), then local as fallback
const __dirname = path.dirname(fileURLToPath(import.meta.url));
dotenv.config({ path: path.join(__dirname, '..', '.env'), override: true });  // parent .env (real keys)
dotenv.config({ path: path.join(__dirname, '.env') });  // cwv-agent/.env (fallback)

async function main() {
  // Parse command line arguments
  const argv = parseArguments();

  // Extract parameters
  const action = argv.action;
  const deviceType = argv.device;
  const skipCache = argv.skipCache;
  const outputSuffix = argv.outputSuffix;
  const blockRequests = argv.blockRequests;
  const model = argv.model;
  const fieldUrl = argv.fieldUrl;  // Separate URL for CrUX/PSI field data
  const framework = argv.framework;  // Framework type for context-specific guidance

  // Handle MCP reviewer action separately
  if (action === 'mcp-reviewer') {
    // Note: No console output for MCP mode - it interferes with JSON-RPC protocol
    await processUrl(null, action, deviceType, skipCache, outputSuffix, blockRequests, model, null, fieldUrl, framework);
    return;
  }

  // Load URLs for other actions
  const agentMode = argv.agentMode;

  // Load URLs
  const urls = loadUrls(argv);

  console.log(`Running ${action} for ${urls.length} URL(s) on ${deviceType}...`);
  if (skipCache) {
    console.log('Cache is disabled. Forcing new data collection.');
  }
  if (model) {
    console.log(`Using model: ${model}`);
  }
  if (fieldUrl) {
    console.log(`Field URL for CrUX/PSI: ${fieldUrl}`);
  }
  if (framework) {
    console.log(`Framework: ${framework}`);
  }

  // Process each URL
  for (const url of urls) {
    await processUrl(url, action, deviceType, skipCache, outputSuffix, blockRequests, model, agentMode, fieldUrl, framework);

    // Small delay between processing URLs
    if (urls.length > 1) {
      await new Promise(resolve => setTimeout(resolve, 60_000)); // 1min
    }
  }
}

// Run the main function
main().catch(error => {
  // Only log to stderr (not stdout) and only exit for non-MCP actions
  console.error('Fatal error:', error);
  // Don't exit if we're running MCP server (it should handle its own errors)
  if (!process.argv.includes('mcp-reviewer')) {
    process.exit(1);
  }
});
