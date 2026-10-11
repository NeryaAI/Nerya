import {defineConfig} from '@playwright/test';
import taskRuns from './playwright.task-runs.config';

export default defineConfig({...taskRuns,
  use:{...taskRuns.use,actionTimeout:10000},
  testMatch:['task-runs-refactor.spec.ts','i18n-copy.spec.ts','backtest-research.spec.ts','workflow-experience.spec.ts','workbench-layout.spec.ts'],
  outputDir:'../../ui-review/i18n-cleanup/browser-results',
  reporter:[['list'],['json',{outputFile:'../../ui-review/i18n-cleanup/browser-results.json'}]],
});
