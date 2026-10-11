const { test } = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');
const ts = require('typescript');

function fixture() {
  const models = [], charts = [];
  let disconnected = false;
  class NativeRenderer {
    mountIndicator(model) { models.push(model); return model; }
    removeIndicator() {}
    ensurePane() {}
  }
  class Vela {
    constructor(container, options) {
      this.options = options;
      this.renderer = { set() {}, onCrosshairMove() { return () => {}; }, onClick() { return () => {}; } };
      charts.push(this);
    }
    ready() { return Promise.resolve(); }
    setVisibleRangePreset() {}
    setVisibleRange(range) { this.range = range; }
    resize() {}
    destroy() { this.destroyed = true; }
  }
  class ResizeObserver { observe() {} disconnect() { disconnected = true; } }
  const cache = new Map();
  function load(filename) {
    if (cache.has(filename)) return cache.get(filename);
    const module = { exports: {} };
    const code = ts.transpileModule(fs.readFileSync(filename, 'utf8'), { compilerOptions: { module: ts.ModuleKind.CommonJS, target: ts.ScriptTarget.ES2022 } }).outputText;
    vm.runInNewContext(code, { module, exports: module.exports, ResizeObserver, requestAnimationFrame: callback => queueMicrotask(callback),
      require(name) { return name === '@luxalgo/vela' ? { NativeRenderer, Vela } : name.startsWith('.') ? load(path.resolve(path.dirname(filename), name + '.ts')) : require(name); } }, { filename });
    cache.set(filename, module.exports);
    return module.exports;
  }
  const container = { style: {}, dataset: {}, addEventListener() {}, removeEventListener() {}, querySelectorAll() { return []; } };
  const api = load(path.resolve(__dirname, '../lib/velaChart.ts'));
  return { ...api, container, models, charts, get disconnected() { return disconnected; } };
}
const time = 1790000000;
const theme = { text: '#aaa', grid: '#333', isLight: false };

test('Vela boundary normalizes time, rejects invalid observations and retains sparse gaps', async () => {
  const f = fixture();
  assert.equal(f.velaTime(time), time * 1000);
  assert.equal(f.velaTime(time * 1000), time * 1000);
  assert.equal(f.velaTime(new Date(time * 1000).toISOString()), time * 1000);
  const plot = f.createVelaChart(f.container, { height: 240, theme, series: [
    { type: 'line', name: 'NAV', data: [{ time: time + 60, value: 12 }, { time, value: 10 }, { time, value: 11 }, { time: 'invalid', value: 99 }, { time: time + 90, value: NaN }] },
    { type: 'line', name: 'Sparse', data: [{ time: time + 60, value: 4 }] },
  ] });
  await plot.ready;
  assert.deepEqual(Array.from(f.charts[0].options.data, bar => [bar.time, bar.close]), [[time * 1000, 11], [(time + 60) * 1000, 12]]);
  assert.deepEqual(Array.from(f.models[1].series[0].points, point => point.value), [null, 4]);
  assert.equal(f.container.dataset.chartReady, 'true');
  plot.destroy();
  assert.equal(f.disconnected, true);
  assert.equal(f.charts[0].destroyed, true);
});

test('volume colors, simultaneous markers, reference levels and regions survive conversion', async () => {
  const f = fixture();
  const plot = f.createVelaChart(f.container, { height: 240, theme, series: [
    { type: 'candlestick', name: 'Price', data: [{ time, open: 10, high: 14, low: 9, close: 12 }] },
    { type: 'histogram', name: 'Volume', price_format: 'volume', data: [{ time, value: 400, color: '#abc123' }] },
  ], markers: [
    { id: 'fill', time, position: 'belowBar', shape: 'arrowUp', color: '#00ff00' },
    { id: 'signal', time, position: 'aboveBar', shape: 'circle', color: '#ffaa00' },
  ], overlays: [{ type: 'price_line', price: 11, line_style: 'dashed' }, { type: 'region', from: time, to: time + 60 }] });
  await plot.ready;
  assert.equal(f.models[1].paneId, 'volume');
  assert.equal(f.models[1].series[0].points[0].color, '#abc123');
  const overlay = f.models.at(-1);
  assert.equal(overlay.labels.length, 2);
  assert.equal(overlay.labels[0].x, time * 1000);
  assert.equal(overlay.labels[0].style, 'arrowup');
  assert.equal(overlay.labels[1].style, 'circle');
  assert.equal(overlay.priceLines[0].price, 11);
  assert.equal(overlay.priceLines[0].lineStyle, 'dashed');
  assert.equal(overlay.backgrounds[0].to, (time + 60) * 1000);
  plot.destroy();
});

test('unmount before Vela is ready never mounts stale data or overlays', async () => {
  const f = fixture();
  const plot = f.createVelaChart(f.container, { height: 240, theme, series: [] });
  plot.destroy();
  plot.setOverlays({ markers: [] });
  await plot.ready;
  assert.equal(f.models.length, 0);
  assert.equal(f.container.dataset.chartReady, undefined);
});

test('sparse candles remain anchored when another series adds timestamps', async () => {
  const f = fixture();
  const bar = { open: 10, high: 14, low: 9, close: 12 };
  const plot = f.createVelaChart(f.container, { height: 240, theme, series: [
    { type: 'candlestick', name: 'Price', data: [{ time, ...bar }, { time: time + 120, ...bar }] },
    { type: 'line', name: 'Signal', data: [{ time: time + 60, value: 11 }] },
  ] });
  await plot.ready;
  assert.equal(f.models[0].anchorTime, time * 1000);
  assert.equal(f.models[1].anchorTime, (time + 120) * 1000);
  assert.equal(f.models[0].series[0].bars.length, 1);
  assert.equal(f.models[1].series[0].bars.length, 1);
  assert.deepEqual(Array.from(f.models[2].series[0].points, point => point.value), [null, 11, null]);
  plot.destroy();
});
