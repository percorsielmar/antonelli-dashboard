// Migrazione una-tantum dei file data/YYYY-MM-DD.json esistenti su Postgres (Neon).
// Uso: imposta DATABASE_URL nel .env, poi `npm run migrate`.
require('dotenv').config();
const fs = require('fs');
const path = require('path');
const db = require('./db');

const DATA_DIR = process.env.DATA_DIR || path.join(__dirname, 'data');

async function main() {
  if (!process.env.DATABASE_URL) {
    console.error('DATABASE_URL non impostata: aggiungila nel .env prima di migrare.');
    process.exit(1);
  }

  await db.initDb();

  // Evita doppioni: salta la migrazione dei giorni gia presenti nel DB
  const existing = await db.loadAllSnapshots();

  const files = fs.readdirSync(DATA_DIR).filter(f => /^\d{4}-\d{2}-\d{2}\.json$/.test(f)).sort();
  let totalSnaps = 0;

  for (const file of files) {
    const dayStr = file.replace('.json', '');
    if (existing[dayStr]) {
      console.log(`Salto ${dayStr}: gia presente nel DB`);
      continue;
    }
    let snaps;
    try {
      snaps = JSON.parse(fs.readFileSync(path.join(DATA_DIR, file), 'utf8'));
    } catch (e) {
      console.error(`Errore lettura ${file}: ${e.message}`);
      continue;
    }
    for (const snap of snaps) {
      await db.insertSnapshot(dayStr, snap.ts, snap.inverters);
      totalSnaps++;
    }
    console.log(`Migrato ${dayStr}: ${snaps.length} snapshot`);
  }

  console.log(`\nCompletato: ${totalSnaps} snapshot migrati su Neon.`);
  await db.pool.end();
}

main().catch(e => { console.error(e); process.exit(1); });
