#!/usr/bin/env node
// Vital City — weekly 7-day report, produced by the growth dashboard itself.
//
// Opens the published growth dashboard in a headless Chrome, unlocks it with
// the passphrase from the macOS Keychain (typed into the page, never put in a
// URL), switches to the 7-day view and saves the dashboard's own
// "Report: this view" as a PDF and a Markdown file on the Desktop. Same code,
// same numbers, same readings as the button on the dashboard.
//
// Scheduled by launchd: ~/Library/LaunchAgents/com.vitalcity.dashboard-weekly.plist
// (Thursdays, 12:00). Replaces weekly_report.py, retired 2026-09-11.
//
// Passphrase: $VC_NETWORK_PASS if set, else Keychain item "vc-network-pass".
// Output folder: $VC_WEEKLY_OUT if set (used for tests), else ~/Desktop.
// On failure it writes a short "...-FAILED.txt" note to the same folder, so a
// broken run is visible instead of silent.
import { spawn, execFileSync } from "node:child_process";
import { mkdtempSync, rmSync, writeFileSync } from "node:fs";
import { tmpdir, homedir } from "node:os";
import { join } from "node:path";

const DASH = "https://vitalcity-nyc.github.io/vital-city-catalogue/growth/index.html";
const CHROME = "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome";
const OUT = process.env.VC_WEEKLY_OUT || join(homedir(), "Desktop");
const today = new Date().toLocaleDateString("en-CA"); // YYYY-MM-DD, local time
const base = join(OUT, `Vital-City-Weekly-${today}`);
const sleep = ms => new Promise(r => setTimeout(r, ms));

function passphrase() {
  if (process.env.VC_NETWORK_PASS) return process.env.VC_NETWORK_PASS;
  return execFileSync("/usr/bin/security", ["find-generic-password", "-s", "vc-network-pass", "-w"],
    { encoding: "utf8" }).trim();
}

async function main() {
  const pw = passphrase();
  const profile = mkdtempSync(join(tmpdir(), "vc-weekly-"));
  const port = 9300 + Math.floor(Math.random() * 500);
  const chrome = spawn(CHROME, [
    "--headless=new", `--remote-debugging-port=${port}`, `--user-data-dir=${profile}`,
    "--no-first-run", "--no-default-browser-check", "--disable-gpu", "about:blank",
  ], { stdio: "ignore" });
  try {
    let tabs = [];
    for (let i = 0; i < 75 && !tabs.length; i++) {
      try { tabs = (await (await fetch(`http://127.0.0.1:${port}/json/list`)).json()).filter(t => t.type === "page"); }
      catch { /* not up yet */ }
      if (!tabs.length) await sleep(200);
    }
    if (!tabs.length) throw new Error("headless Chrome did not start");

    const ws = new WebSocket(tabs[0].webSocketDebuggerUrl);
    await new Promise((res, rej) => { ws.onopen = res; ws.onerror = () => rej(new Error("could not connect to Chrome")); });
    let seq = 0; const pending = new Map();
    ws.onmessage = e => {
      const m = JSON.parse(e.data);
      if (m.id && pending.has(m.id)) {
        const { res, rej } = pending.get(m.id); pending.delete(m.id);
        m.error ? rej(new Error(m.error.message)) : res(m.result);
      }
    };
    const send = (method, params = {}) => new Promise((res, rej) => {
      const id = ++seq; pending.set(id, { res, rej }); ws.send(JSON.stringify({ id, method, params }));
    });
    const js = async expr => {
      const r = await send("Runtime.evaluate", { expression: expr, awaitPromise: true, returnByValue: true });
      if (r.exceptionDetails) throw new Error(r.exceptionDetails.exception?.description || r.exceptionDetails.text);
      return r.result.value;
    };

    await send("Page.enable"); await send("Runtime.enable");
    await send("Page.navigate", { url: `${DASH}?cb=${Date.now()}` });
    let gate = false;
    for (let i = 0; i < 150 && !gate; i++) {
      try { gate = await js("!!document.querySelector('#pw') && typeof unlock === 'function'"); } catch { /* navigating */ }
      if (!gate) await sleep(200);
    }
    if (!gate) throw new Error("the dashboard page did not load");

    await js(`document.querySelector('#pw').value = ${JSON.stringify(pw)}; document.querySelector('#unlock').click(); true`);
    let ready = false;
    for (let i = 0; i < 225 && !ready; i++) {
      ready = await js("!!(window.renderWeekPulse && renderWeekPulse._data && typeof pulseReportModel === 'function')");
      if (!ready) {
        const err = await js("(document.querySelector('#err') || {}).textContent || ''");
        if (err) throw new Error(`the dashboard refused to unlock: ${err}`);
        await sleep(200);
      }
    }
    if (!ready) throw new Error("the dashboard did not finish loading within 45 seconds");

    const rep = await js(`(() => {
      const b = document.querySelector('#pulseWin button[data-w="7"]'); if (b) b.click();
      const m = pulseReportModel(false);
      if (!m || !m.windows.length || !m.windows[0].tiles.length) throw new Error('the 7-day report came back empty');
      return { html: pulseReportHTML(m), md: pulseReportMD(m), asof: m.asof, tiles: m.windows[0].tiles.length };
    })()`);
    writeFileSync(`${base}.md`, rep.md);

    await js(`document.open(); document.write(${JSON.stringify(rep.html)}); document.close(); true`);
    await sleep(800);
    const pdf = await send("Page.printToPDF", {
      printBackground: true, paperWidth: 8.5, paperHeight: 11,
      marginTop: 0.4, marginBottom: 0.45, marginLeft: 0.4, marginRight: 0.4,
    });
    writeFileSync(`${base}.pdf`, Buffer.from(pdf.data, "base64"));
    console.log(`${new Date().toISOString()} wrote ${base}.pdf and .md (data as of ${rep.asof}, ${rep.tiles} tiles)`);
    ws.close();
  } finally {
    chrome.kill();
    await sleep(400);
    try { rmSync(profile, { recursive: true, force: true }); } catch { /* temp dir */ }
  }
}

main().catch(e => {
  const msg = `Vital City weekly report FAILED at ${new Date().toString()}\n\n${e.stack || e}\n\n` +
    `No report was written. Details: ~/Library/Logs/vital-city-weekly.err\n` +
    `To retry: launchctl kickstart -k gui/$(id -u)/com.vitalcity.dashboard-weekly\n`;
  try { writeFileSync(`${base}-FAILED.txt`, msg); } catch { /* nothing more to do */ }
  console.error(msg);
  process.exit(1);
});
