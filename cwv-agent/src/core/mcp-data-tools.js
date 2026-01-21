/**
 * @fileoverview This file contains the implementation of the granular, on-demand
 * MCP (Model-Context-Protocol) tools for the CWV-Agent. These tools are designed
 * to be called by the Cursor agent to dynamically collect performance data and
 * context, allowing for a more intelligent and efficient diagnostic process.
 */

import { collect as collectPsi } from '../tools/psi.js';
import { collect as collectLabData } from '../tools/lab/index.js';
import { initializeSystem } from '../prompts/index.js';
import { createTask, getTaskStatus, getTaskResult } from './task-manager.js';
import fs from 'fs/promises';
import path from 'path';
import { fileURLToPath } from 'url';
import { z } from 'zod';

const __filename = fileURLToPath(import.meta.url);
const __dirname = path.dirname(__filename);

const urlAndOptionalDeviceType = {
  url: z.string().describe('The URL of the site to check against.'),
  deviceType: z.enum(['mobile', 'desktop']).default('mobile').describe('Device type (mobile or desktop)'),
};

const urlAndOptionalDeviceAndCacheType = {
  ...urlAndOptionalDeviceType,
  skipCache: z.boolean().default(false).describe('Whether to skip the cache and force a new data collection.'),
};

export const dataTools = {
  get_psi: {
    name: 'get_psi',
    description: 'Collects PageSpeed Insights (PSI) data for a given URL.',
    inputSchema: urlAndOptionalDeviceAndCacheType,
    execute: async ({ url, deviceType = 'mobile', skipCache = false }) => {
      const { full, summary } = await collectPsi(url, deviceType, { skipCache });
      return { full, summary };
    },
  },

  get_har: {
    name: 'get_har',
    description: 'Collects HAR data for a given URL.',
    inputSchema: urlAndOptionalDeviceAndCacheType,
    execute: async ({ url, deviceType = 'mobile', skipCache = false }) => {
      const { har, harSummary } = await collectLabData(url, deviceType, { skipCache });
      return { har, harSummary };
    },
  },

  get_code_coverage: {
    name: 'get_code_coverage',
    description: 'Collects JavaScript and CSS coverage data for a given URL.',
    inputSchema: urlAndOptionalDeviceAndCacheType,
    execute: async ({ url, deviceType = 'mobile', skipCache = false }) => {
      const { coverageData, coverageDataSummary } = await collectLabData(url, deviceType, { skipCache });
      return { coverageData, coverageDataSummary };
    },
  },

  get_rendered_html: {
    name: 'get_rendered_html',
    description: 'Collects the final rendered HTML of a page.',
    inputSchema: urlAndOptionalDeviceAndCacheType,
    execute: async ({ url, deviceType = 'mobile', skipCache = false }) => {
      const { fullHtml } = await collectLabData(url, deviceType, { skipCache });
      return { fullHtml };
    },
  },

  detect_framework: {
    name: 'detect_framework',
    description: 'Detects the framework or technology stack of a site (returns generic static site info).',
    inputSchema: {
      url: z.string().describe('The URL of the site to detect the framework of.'),
    },
    execute: async ({ url }) => {
      // Framework detection is now done externally via the HuggingFace dataset
      // This tool returns a generic response
      return { framework: 'Static HTML', note: 'Use --framework CLI flag to specify the framework type' };
    },
  },

  get_prompt_context: {
    name: 'get_prompt_context',
    description: 'Retrieves the specific prompt context for a given framework type.',
    inputSchema: {
      framework: z.string().describe('The type of framework (e.g., "Jekyll", "Hexo", "Static HTML")'),
    },
    execute: async ({ framework }) => {
      const context = initializeSystem(framework);
      return { context };
    },
  },

  start_psi_collection: {
    name: 'start_psi_collection',
    description: 'Starts a long-running collection of PageSpeed Insights (PSI) data. Returns a taskId.',
    inputSchema: {
      url: z.string().describe('The URL of the site to check against.'),
      deviceType: z.enum(['mobile', 'desktop']).default('mobile').describe('Device type (mobile or desktop)'),
      skipCache: z.boolean().default(false).describe('Whether to skip the cache and force a new data collection.'),
    },
    execute: async ({ url, deviceType, skipCache }) => {
      // The worker function is what will be executed in the background.
      const worker = () => collectPsi(url, deviceType, { skipCache });
      const taskId = createTask(worker);
      return { taskId };
    },
  },

  get_psi_status: {
    name: 'get_psi_status',
    description: 'Checks the status of a PSI data collection task (PENDING, RUNNING, COMPLETE, FAILED).',
    inputSchema: {
      taskId: z.string().describe('The ID of the task to check.'),
    },
    execute: async ({ taskId }) => {
      return getTaskStatus(taskId);
    },
  },

  get_psi_result: {
    name: 'get_psi_result',
    description: 'Retrieves the result of a completed PSI data collection task. The task is cleared from memory after retrieval.',
    inputSchema: {
      taskId: z.string().describe('The ID of the task to retrieve.'),
    },
    execute: async ({ taskId }) => {
      const result = getTaskResult(taskId);
      if (!result) {
        return {
          error: 'Result not available. The task may still be running, may have failed, or the ID is invalid. Use get_psi_status to check.',
          summary: '',
          full: {},
        };
      }
      return result;
    },
  },
};
