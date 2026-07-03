// Estende data/solax_storico_completo.csv con i giorni presenti nei file
// "Plant Reports*.xlsx" esportati da SolaxCloud (livello impianto, monthly/daily).
// Idempotente: salta le date gia presenti. Uso: node import_plant_reports.js
const XLSX = require('xlsx');
const fs = require('fs');
const path = require('path');

const DATA_DIR = path.join(__dirname, 'data');
const CSV_PATH = path.join(DATA_DIR, 'solax_storico_completo.csv');

// Trova la colonna (lettera) il cui header in riga 2 contiene la stringa
function colByHeader(ws, needle) {
  for (let c = 0; c < 26; c++) {
    const addr = XLSX.utils.encode_cell({ c, r: 1 }); // riga 2 (0-indexed r=1)
    const cell = ws[addr];
    if (cell && String(cell.v).toLowerCase().includes(needle.toLowerCase())) {
      return c;
    }
  }
  return -1;
}

function num(ws, c, r) {
  const cell = ws[XLSX.utils.encode_cell({ c, r })];
  if (!cell || cell.v === undefined || cell.v === '') return 0;
  const v = parseFloat(cell.v);
  return isNaN(v) ? 0 : v;
}

function parseReport(file) {
  const wb = XLSX.readFile(path.join(DATA_DIR, file));
  const ws = wb.Sheets[wb.SheetNames[0]];
  const cDate = colByHeader(ws, 'Date');
  const cPV = colByHeader(ws, 'inverter output');
  const cFeed = colByHeader(ws, 'exported');
  const cGrid = colByHeader(ws, 'imported');
  const cCons = colByHeader(ws, 'consumption');
  if ([cDate, cPV, cFeed, cGrid, cCons].some(x => x < 0)) {
    console.error(`  Colonne non trovate in ${file} (Date/inverter output/exported/imported/consumption)`);
    return [];
  }
  const rows = [];
  for (let r = 2; r < 1000; r++) { // dati da riga 3 (r=2)
    const dCell = ws[XLSX.utils.encode_cell({ c: cDate, r })];
    if (!dCell || !dCell.v) break;
    const date = String(dCell.v).slice(0, 10);
    if (!/^\d{4}-\d{2}-\d{2}$/.test(date)) continue;
    rows.push({
      date,
      pv: num(ws, cPV, r),
      feed: num(ws, cFeed, r),
      grid: num(ws, cGrid, r),
      cons: num(ws, cCons, r)
    });
  }
  return rows;
}

function fmt(n) {
  // numero pulito: intero senza decimali, altrimenti come e'
  return Number.isInteger(n) ? String(n) : String(n);
}

function main() {
  // CSV esistente
  const lines = fs.readFileSync(CSV_PATH, 'utf8').split(/\r?\n/);
  const header = lines[0];
  const existing = new Map();
  const order = [];
  for (let i = 1; i < lines.length; i++) {
    const ln = lines[i].trim();
    if (!ln) continue;
    const date = ln.split(';')[0];
    existing.set(date, ln);
    order.push(date);
  }

  // Tutti i Plant Reports nella cartella
  const reportFiles = fs.readdirSync(DATA_DIR).filter(f => /^Plant Reports.*\.xlsx$/i.test(f));
  let added = 0;
  for (const f of reportFiles) {
    console.log(`Leggo ${f}...`);
    const rows = parseReport(f);
    for (const row of rows) {
      if (existing.has(row.date)) continue; // gia presente: non sovrascrivo
      const line = `${row.date};${fmt(row.pv)};${fmt(row.feed)};${fmt(row.grid)};${fmt(row.cons)}`;
      existing.set(row.date, line);
      order.push(row.date);
      added++;
      console.log(`  + ${line}`);
    }
  }

  // Riscrivo ordinato per data
  const sorted = [...new Set(order)].sort();
  const out = [header, ...sorted.map(d => existing.get(d))].join('\n') + '\n';
  fs.writeFileSync(CSV_PATH, out, 'utf8');
  console.log(`\nFatto: ${added} giorni aggiunti. Totale righe: ${sorted.length}`);
}

main();
