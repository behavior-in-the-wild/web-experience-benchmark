import { collect as collectCrux } from '../tools/crux.js';
import { collect as collectLabData } from '../tools/lab/index.js';
import { collect as collectPsi } from '../tools/psi.js';
import { collect as collectCode } from '../tools/code.js';
import { estimateTokenSize } from '../utils.js';

export async function getCrux(pageUrl, deviceType, options) {
  const { full, summary, fromCache } = await collectCrux(pageUrl, deviceType, options);
  if (full.error && full.error.code === 404) {
    console.warn('ℹ️  No CrUX data for that page.');
  } else if (full.error) {
    console.error('❌ Failed to collect CrUX data.', full.error.message);
  } else if (fromCache) {
    console.log('✓ Loaded CrUX data from cache. Estimated token size: ~', estimateTokenSize(full, options.model));
  } else {
    console.log('✅ Processed CrUX data. Estimated token size: ~', estimateTokenSize(full, options.model));
  }
  return { full, summary };
}

export async function getPsi(pageUrl, deviceType, options) {
  const { full, summary, fromCache } = await collectPsi(pageUrl, deviceType, options);
  if (fromCache) {
    console.log('✓ Loaded PSI data from cache. Estimated token size: ~', estimateTokenSize(full, options.model));
  } else {
    console.log('✅ Processed PSI data. Estimated token size: ~', estimateTokenSize(full, options.model));
  }
  return { full, summary };
}

export async function getLabData(pageUrl, deviceType, options) {
  // Check environment variables to skip heavy data collection
  const skipHar = process.env.SKIP_HAR_ANALYSIS === 'true';
  const skipPerfEntries = process.env.SKIP_PERFORMANCE_ENTRIES === 'true';
  const skipFullHtml = process.env.SKIP_FULL_HTML === 'true';
  const skipCoverage = process.env.SKIP_COVERAGE_ANALYSIS === 'true';
  const skipCode = process.env.SKIP_CODE_ANALYSIS === 'true';

  if (skipHar && skipPerfEntries && skipFullHtml && skipCoverage && skipCode) {
    console.log('🚀 Skipping heavy data collection due to environment variables');
    return {
      har: null, harSummary: null,
      perfEntries: null, perfEntriesSummary: null,
      fullHtml: null, jsApi: null,
      coverageData: null, coverageDataSummary: null
    };
  }

  const { har, harSummary, perfEntries, perfEntriesSummary, fullHtml, jsApi, coverageData, coverageDataSummary, fromCache } = await collectLabData(pageUrl, deviceType, options);
  if (fromCache) {
    if (!skipHar) console.log('✓ Loaded HAR data from cache. Estimated token size: ~', estimateTokenSize(har, options.model));
    if (!skipPerfEntries) console.log('✓ Loaded Performance Entries data from cache. Estimated token size: ~', estimateTokenSize(perfEntries, options.model));
    if (!skipFullHtml) console.log('✓ Loaded full rendered HTML markup from cache. Estimated token size: ~', estimateTokenSize(fullHtml, options.model));
    if (!skipCode) console.log('✓ Loaded JS API data from cache. Estimated token size: ~', estimateTokenSize(jsApi, options.model));
    if (!skipCoverage) console.log('✓ Loaded coverage data from cache. Estimated token size: ~', estimateTokenSize(coverageData, options.model));
  } else {
    if (!skipHar) console.log('✅ Processed HAR data. Estimated token size: ~', estimateTokenSize(har, options.model));
    if (!skipPerfEntries) console.log('✅ Processed Performance Entries data. Estimated token size: ~', estimateTokenSize(perfEntries, options.model));
    if (!skipFullHtml) console.log('✅ Processed full rendered HTML markup. Estimated token size: ~', estimateTokenSize(fullHtml, options.model));
    if (!skipCode) console.log('✅ Processed JS API data. Estimated token size: ~', estimateTokenSize(jsApi, options.model));
    if (!skipCoverage) console.log('✅ Processed coverage data. Estimated token size: ~', estimateTokenSize(coverageData, options.model));
  }
  return { har, harSummary, perfEntries, perfEntriesSummary, fullHtml, jsApi, coverageData, coverageDataSummary };
}

export async function getCode(pageUrl, deviceType, requests, options) {
  const { codeFiles, stats } = await collectCode(pageUrl, deviceType, requests, options);
  if (stats.fromCache === stats.total) {
    console.log('✓ Loaded code from cache. Estimated token size: ~', estimateTokenSize(codeFiles, options.model));
  } else if (stats.fromCache > 0) {
    console.log(`✓ Partially loaded code from cache (${stats.fromCache}/${stats.total}). Estimated token size: ~`, estimateTokenSize(codeFiles, options.model));
  } else if (stats.failed > 0) {
    console.error('❌ Failed to collect all project code. Estimated token size: ~', estimateTokenSize(codeFiles, options.model));
  } else {
    console.log('✅ Processed project code. Estimated token size: ~', estimateTokenSize(codeFiles, options.model));
  }
  return { codeFiles, stats };
}

export default async function collectArtifacts(pageUrl, deviceType, options) {
  // Use fieldUrl for CrUX/PSI if provided (for public URLs), otherwise use pageUrl
  const fieldUrl = options.fieldUrl || pageUrl;

  if (options.fieldUrl) {
    console.log(`📊 Using field URL for CrUX/PSI: ${fieldUrl}`);
    console.log(`🔬 Using lab URL for browser data: ${pageUrl}`);
  }

  // CrUX and PSI use fieldUrl for API calls but cache under pageUrl for consistency
  const cruxPsiOptions = { ...options, cacheUrl: pageUrl };
  const { full: crux, summary: cruxSummary } = await getCrux(fieldUrl, deviceType, cruxPsiOptions);
  const { full: psi, summary: psiSummary } = await getPsi(fieldUrl, deviceType, cruxPsiOptions);

  // Lab data uses pageUrl (can be localhost)
  const { har, harSummary, perfEntries, perfEntriesSummary, fullHtml, jsApi, coverageData, coverageDataSummary } = await getLabData(pageUrl, deviceType, options);
  const requests = har?.log?.entries?.map((e) => e.request.url) || [];

  // Check if code analysis should be skipped
  const skipCode = process.env.SKIP_CODE_ANALYSIS === 'true';
  let resources = {};

  if (skipCode) {
    console.log('🚀 Skipping code analysis due to SKIP_CODE_ANALYSIS environment variable');
  } else {
    const { codeFiles } = await getCode(pageUrl, deviceType, requests, options);
    resources = codeFiles;
  }

  return {
    har,
    harSummary,
    psi,
    psiSummary,
    resources,
    perfEntries,
    perfEntriesSummary,
    crux,
    cruxSummary,
    fullHtml,
    jsApi,
    coverageData,
    coverageDataSummary,
  };
}
