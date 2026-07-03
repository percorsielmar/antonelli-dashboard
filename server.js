require('dotenv').config();
const express = require('express');
const cors = require('cors');
const fetch = require('node-fetch');
const path = require('path');
const fs = require('fs');
const db = require('./db');

const app = express();
const PORT = process.env.PORT || 3000;

// Se DATABASE_URL e' impostata usa Postgres (Neon) con cache in memoria,
// altrimenti fallback su file JSON locali (sviluppo).
const USE_DB = !!process.env.DATABASE_URL;
let snapshotCache = {}; // { 'YYYY-MM-DD': [ {ts, inverters} ] }

app.use(cors());
app.use(express.json());
app.use(express.static(path.join(__dirname, 'public')));

const SOLAX_API_BASE = 'https://www.eu.solaxcloud.com:9443/proxy/api';
const TOKEN_ID = process.env.SOLAX_TOKEN_ID;
const DEFAULT_SN = process.env.SOLAX_SN;
const DATA_DIR = process.env.DATA_DIR || path.join(__dirname, 'data');
const POLL_INTERVAL = 5 * 60 * 1000; // 5 minuti

if (!fs.existsSync(DATA_DIR)) fs.mkdirSync(DATA_DIR, { recursive: true });

// ---- IMPORTED HISTORICAL DATA (from SolaxCloud XLS exports) ----
// File versionato nel repo: sempre dalla cartella data del progetto,
// indipendentemente da DATA_DIR (che riguarda lo storage scrivibile).
const IMPORT_CSV = path.join(__dirname, 'data', 'solax_storico_completo.csv');
let importedHistory = {}; // { 'YYYY-MM-DD': { yield, feedIn, fromGrid, consumed } }

function loadImportedHistory() {
  if (!fs.existsSync(IMPORT_CSV)) return;
  const lines = fs.readFileSync(IMPORT_CSV, 'utf8').replace(/^\ufeff/, '').split('\n').slice(1); // skip header
  lines.forEach(line => {
    const parts = line.split(';');
    if (parts.length >= 5 && parts[0].match(/^\d{4}-\d{2}-\d{2}$/)) {
      importedHistory[parts[0]] = {
        yield: parseFloat(parts[1]) || 0,
        feedIn: parseFloat(parts[2]) || 0,
        fromGrid: parseFloat(parts[3]) || 0,
        consumed: parseFloat(parts[4]) || 0
      };
    }
  });
  console.log(`[Import] ${Object.keys(importedHistory).length} giorni di storico caricati`);
}
loadImportedHistory();

// Helper: get day data merging imported + polled
function getDayData(dateStr) {
  const imported = importedHistory[dateStr] || null;
  const polled = loadDay(dateStr);
  return { imported, polled };
}

// ---- DATA STORAGE ----
function todayFile() {
  const d = new Date();
  return path.join(DATA_DIR, `${d.getFullYear()}-${String(d.getMonth()+1).padStart(2,'0')}-${String(d.getDate()).padStart(2,'0')}.json`);
}

function dateFile(dateStr) {
  return path.join(DATA_DIR, `${dateStr}.json`);
}

function loadDay(dateStr) {
  if (USE_DB) return snapshotCache[dateStr] || [];
  const f = dateFile(dateStr);
  if (!fs.existsSync(f)) return [];
  try { return JSON.parse(fs.readFileSync(f, 'utf8')); } catch { return []; }
}

// Elenco delle date disponibili (DB cache o file)
function availableDates() {
  if (USE_DB) return Object.keys(snapshotCache).sort();
  return fs.readdirSync(DATA_DIR)
    .filter(f => f.endsWith('.json'))
    .map(f => f.replace('.json', ''))
    .sort();
}

function saveSnapshot(inverters) {
  const ts = new Date().toISOString();
  const d = new Date();
  const dayStr = `${d.getFullYear()}-${String(d.getMonth()+1).padStart(2,'0')}-${String(d.getDate()).padStart(2,'0')}`;

  if (USE_DB) {
    if (!snapshotCache[dayStr]) snapshotCache[dayStr] = [];
    snapshotCache[dayStr].push({ ts, inverters });
    db.insertSnapshot(dayStr, ts, inverters).catch(e => console.error('[DB] Errore insert:', e.message));
    return;
  }

  const f = todayFile();
  const day = loadDay(path.basename(f, '.json'));
  day.push({ ts, inverters });
  fs.writeFileSync(f, JSON.stringify(day), 'utf8');
}

// ---- POLLING SOLAXCLOUD ----
let lastData = null;

async function pollSolax() {
  try {
    const url = `${SOLAX_API_BASE}/getRealtimeInfo.do?tokenId=${TOKEN_ID}&sn=${DEFAULT_SN}`;
    const response = await fetch(url);
    const data = await response.json();
    if (data.success && Array.isArray(data.result)) {
      lastData = { ts: new Date().toISOString(), inverters: data.result };
      saveSnapshot(data.result);
      console.log(`[${new Date().toLocaleTimeString('it-IT')}] Snapshot salvato - ${data.result.length} inverter`);
    }
  } catch (error) {
    console.error('Errore polling:', error.message);
  }
}

// ---- API ENDPOINTS ----

// Dati real-time (ultimo snapshot o live)
app.get('/api/realtime', async (req, res) => {
  try {
    const sn = req.query.sn || DEFAULT_SN;
    const url = `${SOLAX_API_BASE}/getRealtimeInfo.do?tokenId=${TOKEN_ID}&sn=${sn}`;
    const response = await fetch(url);
    const data = await response.json();
    if (data.success && Array.isArray(data.result)) {
      lastData = { ts: new Date().toISOString(), inverters: data.result };
    }
    res.json(data);
  } catch (error) {
    if (lastData) return res.json({ success: true, result: lastData.inverters, cached: true });
    res.status(500).json({ error: error.message });
  }
});

// Dati giornalieri (serie temporale)
app.get('/api/history/day', (req, res) => {
  const date = req.query.date || new Date().toISOString().slice(0, 10);
  const inverterSN = req.query.inverter || null;
  const snapshots = loadDay(date);
  
  const series = snapshots.map(snap => {
    const invs = inverterSN 
      ? snap.inverters.filter(i => i.inverterSN === inverterSN)
      : snap.inverters;
    
    const totalPower = invs.reduce((s, i) => s + (parseFloat(i.acpower) || 0), 0) / 1000;
    const totalYield = invs.reduce((s, i) => s + (parseFloat(i.yieldtoday) || 0), 0);
    const feedIn = invs.reduce((s, i) => s + (parseFloat(i.feedinpower) || 0), 0) / 1000;
    
    return {
      time: new Date(snap.ts).toLocaleTimeString('it-IT', { hour: '2-digit', minute: '2-digit' }),
      ts: snap.ts,
      power: Math.round(totalPower * 10) / 10,
      yield: Math.round(totalYield * 10) / 10,
      feedIn: Math.round(feedIn * 10) / 10,
      inverters: snap.inverters.map(i => ({
        sn: i.inverterSN,
        power: (parseFloat(i.acpower) || 0) / 1000,
        yield: parseFloat(i.yieldtoday) || 0
      }))
    };
  });
  
  // Se non ci sono snapshot polling, prova dati importati
  if (series.length === 0 && !inverterSN) {
    const imported = importedHistory[date] || null;
    if (imported) {
      return res.json({
        date,
        points: 0,
        source: 'import',
        imported: {
          yield: imported.yield,
          feedIn: imported.feedIn,
          fromGrid: imported.fromGrid,
          consumed: imported.consumed
        },
        data: []
      });
    }
  }
  
  res.json({ date, points: series.length, data: series });
});

// Dati mensili (aggregati per giorno)
app.get('/api/history/month', (req, res) => {
  const year = parseInt(req.query.year) || new Date().getFullYear();
  const month = parseInt(req.query.month) || (new Date().getMonth() + 1);
  const inverterSN = req.query.inverter || null;
  const monthStr = `${year}-${String(month).padStart(2, '0')}`;
  
  const days = [];
  for (let d = 1; d <= 31; d++) {
    const dateStr = `${monthStr}-${String(d).padStart(2, '0')}`;
    const { imported, polled } = getDayData(dateStr);
    
    if (polled.length > 0 && !inverterSN) {
      // Usa dati polling (più dettagliati) se disponibili e non filtrati per inverter
      const lastSnap = polled[polled.length - 1];
      const invs = inverterSN
        ? lastSnap.inverters.filter(i => i.inverterSN === inverterSN)
        : lastSnap.inverters;
      const maxYield = invs.reduce((s, i) => s + (parseFloat(i.yieldtoday) || 0), 0);
      const peakPower = polled.reduce((max, snap) => {
        const filtered = inverterSN ? snap.inverters.filter(i => i.inverterSN === inverterSN) : snap.inverters;
        const p = filtered.reduce((s, i) => s + (parseFloat(i.acpower) || 0), 0) / 1000;
        return Math.max(max, p);
      }, 0);
      days.push({ date: dateStr, day: d, yield: Math.round(maxYield * 10) / 10, peakPower: Math.round(peakPower * 10) / 10, snapshots: polled.length, source: 'poll' });
    } else if (imported && !inverterSN) {
      // Usa dati importati da SolaxCloud
      days.push({ date: dateStr, day: d, yield: imported.yield, peakPower: 0, feedIn: imported.feedIn, fromGrid: imported.fromGrid, consumed: imported.consumed, snapshots: 0, source: 'import' });
    } else if (polled.length > 0 && inverterSN) {
      const lastSnap = polled[polled.length - 1];
      const invs = lastSnap.inverters.filter(i => i.inverterSN === inverterSN);
      const maxYield = invs.reduce((s, i) => s + (parseFloat(i.yieldtoday) || 0), 0);
      const peakPower = polled.reduce((max, snap) => {
        const filtered = snap.inverters.filter(i => i.inverterSN === inverterSN);
        const p = filtered.reduce((s, i) => s + (parseFloat(i.acpower) || 0), 0) / 1000;
        return Math.max(max, p);
      }, 0);
      days.push({ date: dateStr, day: d, yield: Math.round(maxYield * 10) / 10, peakPower: Math.round(peakPower * 10) / 10, snapshots: polled.length, source: 'poll' });
    }
    // Se non ci sono dati né importati né polled, skip
  }
  
  const totalYield = days.reduce((s, d) => s + d.yield, 0);
  res.json({ month: monthStr, days, totalYield: Math.round(totalYield * 10) / 10 });
});

// Dati annuali (aggregati per mese)
app.get('/api/history/year', (req, res) => {
  const year = parseInt(req.query.year) || new Date().getFullYear();
  const inverterSN = req.query.inverter || null;
  
  const months = [];
  for (let m = 1; m <= 12; m++) {
    const monthStr = `${year}-${String(m).padStart(2, '0')}`;
    let monthYield = 0;
    let monthPeak = 0;
    let monthFeedIn = 0;
    let monthFromGrid = 0;
    let monthConsumed = 0;
    let daysWithData = 0;
    
    for (let d = 1; d <= 31; d++) {
      const dateStr = `${monthStr}-${String(d).padStart(2, '0')}`;
      const { imported, polled } = getDayData(dateStr);
      
      if (polled.length > 0 && !inverterSN) {
        daysWithData++;
        const lastSnap = polled[polled.length - 1];
        monthYield += lastSnap.inverters.reduce((s, i) => s + (parseFloat(i.yieldtoday) || 0), 0);
        const dayPeak = polled.reduce((max, snap) => {
          const p = snap.inverters.reduce((s, i) => s + (parseFloat(i.acpower) || 0), 0) / 1000;
          return Math.max(max, p);
        }, 0);
        monthPeak = Math.max(monthPeak, dayPeak);
      } else if (imported && !inverterSN) {
        daysWithData++;
        monthYield += imported.yield;
        monthFeedIn += imported.feedIn;
        monthFromGrid += imported.fromGrid;
        monthConsumed += imported.consumed;
      } else if (polled.length > 0 && inverterSN) {
        daysWithData++;
        const lastSnap = polled[polled.length - 1];
        const invs = lastSnap.inverters.filter(i => i.inverterSN === inverterSN);
        monthYield += invs.reduce((s, i) => s + (parseFloat(i.yieldtoday) || 0), 0);
        const dayPeak = polled.reduce((max, snap) => {
          const filtered = snap.inverters.filter(i => i.inverterSN === inverterSN);
          const p = filtered.reduce((s, i) => s + (parseFloat(i.acpower) || 0), 0) / 1000;
          return Math.max(max, p);
        }, 0);
        monthPeak = Math.max(monthPeak, dayPeak);
      }
    }
    
    const monthNames = ['Gen','Feb','Mar','Apr','Mag','Giu','Lug','Ago','Set','Ott','Nov','Dic'];
    months.push({
      month: m,
      label: monthNames[m-1],
      yield: Math.round(monthYield * 10) / 10,
      peakPower: Math.round(monthPeak * 10) / 10,
      feedIn: Math.round(monthFeedIn * 10) / 10,
      fromGrid: Math.round(monthFromGrid * 10) / 10,
      consumed: Math.round(monthConsumed * 10) / 10,
      daysWithData
    });
  }
  
  const totalYield = months.reduce((s, m) => s + m.yield, 0);
  res.json({ year, months, totalYield: Math.round(totalYield * 10) / 10 });
});

// Anni disponibili
app.get('/api/history/years', (req, res) => {
  const years = new Set();
  Object.keys(importedHistory).forEach(d => years.add(parseInt(d.substring(0, 4))));
  availableDates().forEach(d => years.add(parseInt(d.substring(0, 4))));
  res.json({ years: [...years].sort() });
});

// Lista date disponibili
app.get('/api/history/dates', (req, res) => {
  res.json({ dates: availableDates() });
});

// ---- EXPORT CSV ----
app.get('/api/export/csv', (req, res) => {
  const from = req.query.from;
  const to = req.query.to;
  const inverterSN = req.query.inverter || '';
  const mode = req.query.mode || 'detail'; // detail | daily

  if (!from || !to) return res.status(400).json({ error: 'Parametri from e to obbligatori' });

  // Raccogli tutte le date nel range (DB cache o file)
  const rangeFiles = availableDates().filter(d => d >= from && d <= to);

  const INV_LABELS = {
    'H34A15IA529024':'Hybrid 15kW','X3F100J3116121':'X3F 100kW #1','X3F100J3116094':'X3F 100kW #2',
    'A3F080J6733015':'A3F 80kW','A3F100J7057023':'A3F 100kW #1','A3F100L7869005':'A3F 100kW #2'
  };

  let csvRows = [];

  // Raccogli tutte le date nel range (importate + polled)
  const allDates = new Set();
  rangeFiles.forEach(f => allDates.add(f.replace('.json', '')));
  Object.keys(importedHistory).filter(d => d >= from && d <= to).forEach(d => allDates.add(d));
  const sortedDates = [...allDates].sort();

  if (mode === 'daily') {
    // Una riga per giorno con riepilogo
    csvRows.push(['Data','Produzione PV (kWh)','Feed-in (kWh)','Da Rete (kWh)','Consumo (kWh)','Fonte'].join(';'));

    sortedDates.forEach(dateStr => {
      const { imported, polled } = getDayData(dateStr);
      
      if (polled.length > 0 && !inverterSN) {
        const lastSnap = polled[polled.length - 1];
        const yieldDay = lastSnap.inverters.reduce((s, i) => s + (parseFloat(i.yieldtoday) || 0), 0);
        csvRows.push([dateStr, yieldDay.toFixed(1), '', '', '', 'polling'].join(';'));
      } else if (imported && !inverterSN) {
        csvRows.push([dateStr, imported.yield, imported.feedIn, imported.fromGrid, imported.consumed, 'import'].join(';'));
      } else if (polled.length > 0 && inverterSN) {
        const lastSnap = polled[polled.length - 1];
        const invs = lastSnap.inverters.filter(i => i.inverterSN === inverterSN);
        const yieldDay = invs.reduce((s, i) => s + (parseFloat(i.yieldtoday) || 0), 0);
        csvRows.push([dateStr, yieldDay.toFixed(1), '', '', '', 'polling'].join(';'));
      }
    });
  } else {
    // Dettaglio: una riga per snapshot
    if (inverterSN) {
      // CSV per singolo inverter
      csvRows.push(['Data','Ora','Inverter','Nome','Potenza AC (W)','Produzione Oggi (kWh)','Produzione Totale (kWh)','Feed-in (W)','Feed-in Energy (kWh)','Consumo Energy (kWh)','DC1 (W)','DC2 (W)','DC3 (W)','DC4 (W)','SOC (%)','Bat Power (W)','Stato'].join(';'));

      rangeFiles.forEach(file => {
        const dateStr = file.replace('.json', '');
        const snapshots = loadDay(dateStr);
        snapshots.forEach(snap => {
          const inv = snap.inverters.find(i => i.inverterSN === inverterSN);
          if (!inv) return;
          const time = new Date(snap.ts).toLocaleTimeString('it-IT', { hour: '2-digit', minute: '2-digit', second: '2-digit' });
          csvRows.push([
            dateStr, time, inv.inverterSN, INV_LABELS[inv.inverterSN] || inv.inverterSN,
            inv.acpower || 0, inv.yieldtoday || 0, inv.yieldtotal || 0,
            inv.feedinpower || 0, inv.feedinenergy || 0, inv.consumeenergy || 0,
            inv.powerdc1 || 0, inv.powerdc2 || 0, inv.powerdc3 || 0, inv.powerdc4 || 0,
            inv.soc || '', inv.batPower || '', inv.inverterStatus || ''
          ].join(';'));
        });
      });
    } else {
      // CSV globale (tutti gli inverter aggregati + dettaglio per-inverter)
      csvRows.push(['Data','Ora','Potenza Totale (kW)','Produzione Oggi Totale (kWh)','Inverter Attivi'].join(';'));
      // Prima sezione: aggregato
      rangeFiles.forEach(file => {
        const dateStr = file.replace('.json', '');
        const snapshots = loadDay(dateStr);
        snapshots.forEach(snap => {
          const time = new Date(snap.ts).toLocaleTimeString('it-IT', { hour: '2-digit', minute: '2-digit', second: '2-digit' });
          const totalP = snap.inverters.reduce((s, i) => s + (parseFloat(i.acpower) || 0), 0) / 1000;
          const totalY = snap.inverters.reduce((s, i) => s + (parseFloat(i.yieldtoday) || 0), 0);
          const active = snap.inverters.filter(i => (parseFloat(i.acpower) || 0) > 0).length;
          csvRows.push([dateStr, time, totalP.toFixed(1), totalY.toFixed(1), active].join(';'));
        });
      });

      // Seconda sezione: dettaglio per inverter
      csvRows.push('');
      csvRows.push('--- DETTAGLIO PER INVERTER ---');
      csvRows.push(['Data','Ora','Inverter','Nome','Potenza AC (W)','Produzione Oggi (kWh)','Produzione Totale (kWh)','Stato'].join(';'));

      rangeFiles.forEach(file => {
        const dateStr = file.replace('.json', '');
        const snapshots = loadDay(dateStr);
        snapshots.forEach(snap => {
          const time = new Date(snap.ts).toLocaleTimeString('it-IT', { hour: '2-digit', minute: '2-digit', second: '2-digit' });
          snap.inverters.forEach(inv => {
            csvRows.push([
              dateStr, time, inv.inverterSN, INV_LABELS[inv.inverterSN] || inv.inverterSN,
              inv.acpower || 0, inv.yieldtoday || 0, inv.yieldtotal || 0, inv.inverterStatus || ''
            ].join(';'));
          });
        });
      });
    }
  }

  const label = inverterSN ? (INV_LABELS[inverterSN] || inverterSN) : 'impianto';
  const filename = `pascale_${label.replace(/[^a-zA-Z0-9]/g,'_')}_${from}_${to}_${mode}.csv`;

  res.setHeader('Content-Type', 'text/csv; charset=utf-8');
  res.setHeader('Content-Disposition', `attachment; filename="${filename}"`);
  // BOM per Excel
  res.send('\ufeff' + csvRows.join('\n'));
});

// Snapshot grezzi recenti (per DAE-O auto-warmup)
app.get('/api/snapshots/recent', (req, res) => {
  const days = Math.min(parseInt(req.query.days) || 7, 30);
  const dates = availableDates();
  const recentDates = dates.slice(-days);

  const snapshots = [];
  for (const dateStr of recentDates) {
    const dayData = loadDay(dateStr);
    for (const snap of dayData) {
      snapshots.push({ ts: snap.ts, inverters: snap.inverters });
    }
  }

  res.json({ count: snapshots.length, days: recentDates.length, snapshots });
});

// Lista inverter (dal lastData)
app.get('/api/inverters', (req, res) => {
  if (lastData && lastData.inverters) {
    const list = lastData.inverters.map(i => ({
      sn: i.inverterSN,
      type: i.inverterType,
      power: (parseFloat(i.acpower) || 0) / 1000
    }));
    return res.json(list);
  }
  res.json([]);
});

// Config
app.get('/api/config', (req, res) => {
  res.json({
    tokenConfigured: !!TOKEN_ID,
    snConfigured: DEFAULT_SN && DEFAULT_SN !== 'INSERIRE_QUI_IL_NUMERO_SERIALE',
    defaultSN: DEFAULT_SN
  });
});

// Fallback SPA
app.get('*', (req, res) => {
  res.sendFile(path.join(__dirname, 'public', 'index.html'));
});

async function start() {
  if (USE_DB) {
    try {
      await db.initDb();
      snapshotCache = await db.loadAllSnapshots();
      console.log(`[DB] Connesso a Postgres - ${Object.keys(snapshotCache).length} giorni in cache`);
    } catch (e) {
      console.error('[DB] Errore connessione:', e.message);
    }
  }

  app.listen(PORT, () => {
    console.log(`\n========================================`);
    console.log(`  PASCALE 500kW - Dashboard Energetica`);
    console.log(`========================================`);
    console.log(`  Server attivo su: http://localhost:${PORT}`);
    console.log(`  Token API: ${TOKEN_ID ? 'OK' : 'MANCANTE'}`);
    console.log(`  SN Modulo: ${DEFAULT_SN || 'DA CONFIGURARE'}`);
    console.log(`  Storage: ${USE_DB ? 'Postgres (Neon)' : DATA_DIR}`);
    console.log(`  Polling ogni ${POLL_INTERVAL/60000} min`);
    console.log(`========================================\n`);

    // Primo fetch immediato, poi polling
    pollSolax();
    setInterval(pollSolax, POLL_INTERVAL);
  });
}

start();
