import {defineConfig} from '@playwright/test';
export default defineConfig({testDir:'tests/ui',testMatch:'task-runs-refactor.spec.ts',workers:1,retries:0,timeout:60000,
  expect:{timeout:15000},outputDir:'../../ui-review/task-run-refactor-20261003/browser-results',
  reporter:[['list'],['json',{outputFile:'../../ui-review/task-run-refactor-20261003/browser-results.json'}]],
  use:{baseURL:'http://127.0.0.1:3187',headless:true,screenshot:'only-on-failure',trace:'retain-on-failure',extraHTTPHeaders:{'x-nerya-local-peer':'qa-fixture-peer-not-real'}}});
