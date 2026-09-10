"use client";

import { useEffect, useState } from "react";
import { useTranslations } from "next-intl";
import { DEFAULT_SETTINGS, useUiSettings, type UiSettings, type ThemeMode, type LanguagePreference } from "../../lib/settings";
import { confirm } from "../../lib/dialogs";
import { SwitchControl } from "../SwitchControl";
import { SettingsIcon } from "../icons";
import { Row, SettingsGroup, CompactSelect as Select } from "./SettingsFields";

/** Browser preferences are independent of provider, vault and runtime forms. */
export function InterfaceSettings({ venues }: { venues: { name: string; label: string }[] }) {
  const [settings, patch] = useUiSettings();
  const appearance = useTranslations("settings.appearance");
  const display = useTranslations("settings.displayCard");
  const chart = useTranslations("settings.chartCard");
  const model = useTranslations("settings.modelCard");
  const tabs = useTranslations("settings.tabs");
  const common = useTranslations("common");
  const ui = useTranslations("ui");
  const [symbol, setSymbol] = useState(settings.kline.symbol);
  useEffect(() => { setSymbol(settings.kline.symbol); }, [settings.kline.symbol]);
  const patchChart = (next: Partial<UiSettings["kline"]>) => patch({ kline: { ...settings.kline, ...next } });
  function commitSymbol() {
    const next = symbol.trim().toUpperCase();
    if (next && next !== settings.kline.symbol) patchChart({ symbol: next });
    setSymbol(next || settings.kline.symbol);
  }
  async function reset() {
    if (await confirm({ title: ui("resetPreferences"), message: ui("resetPreferencesDescription") })) patch(DEFAULT_SETTINGS);
  }
  return (
    <div id="settings-panel-interface" role="region" aria-label={tabs("interface")} className="space-y-7">
      <SettingsGroup title={appearance("title")} description={appearance("description")}>
        <Row label={appearance("theme")} desc={appearance("themeDesc")}>
          <Select value={settings.darkMode} onChange={(value) => patch({ darkMode: value as ThemeMode })} options={[
            { value: "system", label: appearance("themeSystem") }, { value: "light", label: appearance("themeLight") }, { value: "dark", label: appearance("themeDark") },
          ]} />
        </Row>
        <Row label={appearance("language")} desc={appearance("languageDesc")}>
          <Select value={settings.language === "zh" ? "zh" : "en"} onChange={(value) => patch({ language: value as LanguagePreference })} options={[{ value: "en", label: "English" }, { value: "zh", label: "中文" }]} />
        </Row>
      </SettingsGroup>
      <SettingsGroup title={display("title")} description={display("description")}>
        <Row label={display("timezone")} desc={display("timezoneDesc")}>
          <Select value={settings.timezone} onChange={(value) => patch({ timezone: value as UiSettings["timezone"] })} options={[
            { value: "auto", label: "Auto" }, { value: "utc+0", label: "UTC+0" }, { value: "utc+8", label: "UTC+8 Shanghai" },
            { value: "utc+9", label: "UTC+9 Tokyo" }, { value: "utc-5", label: "UTC-5 New York" }, { value: "utc-8", label: "UTC-8 Los Angeles" },
          ]} />
        </Row>
        <Row label={display("refreshCadence")} desc={display("refreshCadenceDesc")}>
          <Select value={String(settings.refreshSeconds || 0)} onChange={(value) => patch({ refreshSeconds: Number(value) })} options={[
            { value: "0", label: display("refreshOff") }, { value: "5", label: "5 sec" }, { value: "10", label: "10 sec" }, { value: "30", label: "30 sec" }, { value: "60", label: "1 min" },
          ]} />
        </Row>
        <Row label={display("compactMode")} desc={display("compactModeDesc")}>
          <SwitchControl checked={settings.compact} label={display("compactMode")} onCheckedChange={(value) => patch({ compact: value })} />
        </Row>
      </SettingsGroup>
      <SettingsGroup title={chart("title")} description={chart("description")}>
        <Row label={chart("venue")} desc={chart("venueDesc")}>
          {venues.length ? <Select value={settings.kline.venue} onChange={(value) => patchChart({ venue: value as UiSettings["kline"]["venue"] })} options={venues.map((venue) => ({ value: venue.name, label: venue.label }))} /> : <span className="text-xs text-[color:var(--text-muted)]">{model("noVenues")}</span>}
        </Row>
        <Row label={chart("symbol")} desc={chart("symbolDesc")}>
          <input value={symbol} onChange={(event) => setSymbol(event.target.value)} onBlur={commitSymbol}
            onKeyDown={(event) => { if (event.key === "Enter" && !event.nativeEvent.isComposing && event.keyCode !== 229) { event.preventDefault(); commitSymbol(); } }}
            aria-label={chart("symbol")} placeholder="BTCUSDT" className="input-dark w-full min-w-0 font-mono text-sm sm:w-44" />
        </Row>
        <Row label={chart("timeframe")} desc={chart("timeframeDesc")}>
          <Select value={settings.kline.interval} onChange={(value) => patchChart({ interval: value as UiSettings["kline"]["interval"] })} options={["1m", "5m", "15m", "1h", "4h", "1d"].map((value) => ({ value, label: value }))} />
        </Row>
        <Row label={chart("candles")} desc={chart("candlesDesc")}>
          <Select value={String(settings.kline.count)} onChange={(value) => patchChart({ count: Number(value) })} options={[48, 96, 192, 288].map((value) => ({ value: String(value), label: String(value) }))} />
        </Row>
        <Row label={chart("showVolume")} desc={chart("showVolumeDesc")}>
          <SwitchControl checked={settings.showVolume} label={chart("showVolume")} onCheckedChange={(value) => patch({ showVolume: value })} />
        </Row>
        <Row label={chart("resetSettings")} desc={chart("resetSettingsDesc")}>
          <button type="button" className="btn btn-ghost" onClick={() => void reset()}><SettingsIcon size={14} />{common("reset")}</button>
        </Row>
      </SettingsGroup>
    </div>
  );
}
