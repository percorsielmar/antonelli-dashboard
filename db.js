const { Pool } = require('pg');

// Connessione a Neon (o altro Postgres) tramite DATABASE_URL.
// Neon richiede SSL.
const pool = new Pool({
  connectionString: process.env.DATABASE_URL,
  ssl: { rejectUnauthorized: false }
});

// Crea la tabella dedicata al progetto Pascale (separata dagli altri progetti)
async function initDb() {
  await pool.query(`
    CREATE TABLE IF NOT EXISTS pascale_snapshots (
      id BIGSERIAL PRIMARY KEY,
      day TEXT NOT NULL,
      ts TEXT NOT NULL,
      inverters JSONB NOT NULL
    );
  `);
  await pool.query(
    `CREATE INDEX IF NOT EXISTS idx_pascale_snapshots_day ON pascale_snapshots (day);`
  );
}

// Inserisce uno snapshot. dayStr = 'YYYY-MM-DD', ts = ISO string
async function insertSnapshot(dayStr, ts, inverters) {
  await pool.query(
    'INSERT INTO pascale_snapshots (day, ts, inverters) VALUES ($1, $2, $3)',
    [dayStr, ts, JSON.stringify(inverters)]
  );
}

// Carica tutti gli snapshot raggruppati per giorno: { 'YYYY-MM-DD': [ {ts, inverters} ] }
async function loadAllSnapshots() {
  const res = await pool.query(
    'SELECT day, ts, inverters FROM pascale_snapshots ORDER BY ts ASC'
  );
  const byDate = {};
  for (const row of res.rows) {
    if (!byDate[row.day]) byDate[row.day] = [];
    byDate[row.day].push({ ts: row.ts, inverters: row.inverters });
  }
  return byDate;
}

module.exports = { pool, initDb, insertSnapshot, loadAllSnapshots };
