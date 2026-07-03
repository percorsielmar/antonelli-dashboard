const fs = require('fs');
const path = require('path');

const pvgisPath = 'c:\\Users\\Alex\\Downloads\\Timeseries_41.928_12.583_SA3_200kWp_crystSi_14_5deg_70deg_2020_2023 (1).csv';
const realPath = 'E:\\Gajarda\\pascale-dashboard\\data\\solax_storico_completo.csv';

function analyze() {
  console.log('Loading PVGIS data...');
  const pvgisContent = fs.readFileSync(pvgisPath, 'utf-8');
  const pvgisLines = pvgisContent.split('\n');
  
  // Find where data starts
  let headerIndex = -1;
  for (let i = 0; i < pvgisLines.length; i++) {
    if (pvgisLines[i].startsWith('time,P,')) {
      headerIndex = i;
      break;
    }
  }
  
  if (headerIndex === -1) {
    console.error('Header not found in PVGIS file!');
    return;
  }
  
  const dataLines = pvgisLines.slice(headerIndex + 1);
  
  // Parse PVGIS data by month/day
  const pvgisDaily = {}; // Key: 'YYYY-MM-DD'
  const pvgisTypicalDay = {}; // Key: 'MM-DD' => value
  
  dataLines.forEach(line => {
    if (!line.trim()) return;
    const parts = line.split(',');
    if (parts.length < 2) return;
    const timeStr = parts[0]; // e.g. "20200101:0010"
    const pW = parseFloat(parts[1]); // power in Watts
    if (isNaN(pW)) return;
    
    const colonIdx = timeStr.indexOf(':');
    if (colonIdx === -1) return;
    const datePart = timeStr.substring(0, colonIdx); // "20200101"
    const year = datePart.substring(0, 4);
    const month = datePart.substring(4, 6);
    const day = datePart.substring(6, 8);
    const mmdd = `${month}-${day}`;
    
    const key = `${year}-${month}-${day}`;
    if (!pvgisDaily[key]) {
      pvgisDaily[key] = { yieldWh: 0, month, day, mmdd };
    }
    pvgisDaily[key].yieldWh += pW;
  });
  
  // Aggregate to a typical year (average across years 2020-2023)
  const pvgisCounts = {};
  Object.values(pvgisDaily).forEach(d => {
    const yieldKwh = d.yieldWh / 1000;
    if (!pvgisTypicalDay[d.mmdd]) {
      pvgisTypicalDay[d.mmdd] = 0;
      pvgisCounts[d.mmdd] = 0;
    }
    pvgisTypicalDay[d.mmdd] += yieldKwh;
    pvgisCounts[d.mmdd]++;
  });
  
  Object.keys(pvgisTypicalDay).forEach(mmdd => {
    pvgisTypicalDay[mmdd] = pvgisTypicalDay[mmdd] / pvgisCounts[mmdd];
  });
  
  // Load real data
  console.log('Loading real data...');
  const realContent = fs.readFileSync(realPath, 'utf-8');
  const realLines = realContent.replace(/^\ufeff/, '').split('\n');
  
  const realDaily = [];
  realLines.slice(1).forEach(line => {
    if (!line.trim()) return;
    const parts = line.split(';');
    if (parts.length < 5) return;
    
    const dateStr = parts[0]; // YYYY-MM-DD
    const yieldReal = parseFloat(parts[1]) || 0;
    const feedIn = parseFloat(parts[2]) || 0;
    const fromGrid = parseFloat(parts[3]) || 0;
    const consumed = parseFloat(parts[4]) || 0;
    
    if (yieldReal === 0 && consumed === 0) return;
    
    const partsDate = dateStr.split('-');
    if (partsDate.length < 3) return;
    const mmdd = `${partsDate[1]}-${partsDate[2]}`;
    
    realDaily.push({
      date: dateStr,
      yieldReal,
      consumed,
      mmdd,
      fromGrid,
      feedIn
    });
  });
  
  // Merge and compare as COMBINED SYSTEM (500 kWp Real + 200 kWp Potenziamento)
  const mergedData = [];
  let sumRealYield = 0;
  let sumPvgisYield = 0;
  let sumCombinedYield = 0;
  let sumConsumed = 0;
  let sumCombinedAutoconsumo = 0;
  let sumCombinedImmissione = 0;
  
  // Monthly aggregations
  const monthlyStats = {};
  
  realDaily.forEach(day => {
    const pvgisYield = pvgisTypicalDay[day.mmdd] || 0;
    const combinedYield = day.yieldReal + pvgisYield;
    
    sumRealYield += day.yieldReal;
    sumPvgisYield += pvgisYield;
    sumCombinedYield += combinedYield;
    sumConsumed += day.consumed;
    
    // Per stimare l'autoconsumo del sistema combinato (500 kWp Reale + 200 kWp PVGIS):
    // Attualmente, l'impianto da 500kWp reale ha già un'immissione (feedIn).
    // Con l'aggiunta di altri 200 kWp, l'immissione aumenterà.
    // Stimiamo l'autoconsumo del potenziamento da 200 kWp:
    // Poiché i 200 kWp sono aggiuntivi, produrranno energia contemporaneamente ai 500 kWp.
    // Nei momenti in cui l'azienda ha già un consumo residuo superiore alla produzione reale del 500kWp, i 200kWp verranno autoconsumati.
    // Se invece c'era già immissione o la produzione supera il consumo, l'eccedenza andrà in rete.
    // Facciamo una stima giorno per giorno:
    // Se Consumo > Produzione 500kWp: l'azienda ha un "margine" di autoconsumo pari a (Consumo - Autoconsumo_Reale_500).
    // L'autoconsumo reale del 500 è (day.yieldReal - day.feedIn).
    const realAutoconsumo500 = Math.max(0, day.yieldReal - day.feedIn);
    const margineAutoconsumo = Math.max(0, day.consumed - realAutoconsumo500);
    
    // Il 200 kWp PVGIS può coprire questo margine residuo.
    // Poiché non abbiamo i dati orari sovrapposti nello storico, stimiamo che l'autoconsumo del 200kWp
    // sia limitato sia dal margine residuo, sia da un coefficiente di contemporaneità diurno (di solito 85% nei feriali, 35% nei weekend).
    const dateObj = new Date(day.date);
    const isWeekend = dateObj.getDay() === 0 || dateObj.getDay() === 6;
    const contemporaneitaPct = isWeekend ? 0.35 : 0.85;
    
    const pvgisAutoconsumoStimato = Math.min(margineAutoconsumo, pvgisYield * contemporaneitaPct);
    const combinedAutoconsumo = realAutoconsumo500 + pvgisAutoconsumoStimato;
    const combinedImmissione = Math.max(0, combinedYield - combinedAutoconsumo);
    
    sumCombinedAutoconsumo += combinedAutoconsumo;
    sumCombinedImmissione += combinedImmissione;
    
    const delta = day.consumed - combinedYield;
    
    mergedData.push({
      date: day.date,
      consumed: day.consumed,
      realYield: day.yieldReal,
      pvgisYield: pvgisYield,
      combinedYield: combinedYield,
      delta: delta,
      combinedAutoconsumo: combinedAutoconsumo,
      combinedImmissione: combinedImmissione
    });
    
    const monthKey = day.date.substring(0, 7); // YYYY-MM
    if (!monthlyStats[monthKey]) {
      monthlyStats[monthKey] = {
        month: monthKey,
        consumed: 0,
        realYield: 0,
        pvgisYield: 0,
        combinedYield: 0,
        combinedAutoconsumo: 0,
        combinedImmissione: 0,
        days: 0
      };
    }
    monthlyStats[monthKey].consumed += day.consumed;
    monthlyStats[monthKey].realYield += day.yieldReal;
    monthlyStats[monthKey].pvgisYield += pvgisYield;
    monthlyStats[monthKey].combinedYield += combinedYield;
    monthlyStats[monthKey].combinedAutoconsumo += combinedAutoconsumo;
    monthlyStats[monthKey].combinedImmissione += combinedImmissione;
    monthlyStats[monthKey].days++;
  });
  
  // Format monthly stats for chart
  const monthlyList = Object.keys(monthlyStats).sort().map(k => {
    const m = monthlyStats[k];
    return {
      month: m.month,
      consumed: Math.round(m.consumed),
      realYield: Math.round(m.realYield),
      pvgisYield: Math.round(m.pvgisYield),
      combinedYield: Math.round(m.combinedYield),
      combinedAutoconsumo: Math.round(m.combinedAutoconsumo),
      combinedImmissione: Math.round(m.combinedImmissione),
      delta: Math.round(m.consumed - m.combinedYield),
      autoconsumoPct: m.combinedYield > 0 ? Math.round((m.combinedAutoconsumo / m.combinedYield) * 100) : 0,
      autosufficienzaPct: m.consumed > 0 ? Math.round((m.combinedAutoconsumo / m.consumed) * 100) : 0
    };
  });
  
  const results = {
    summary: {
      daysCount: realDaily.length,
      sumConsumed: Math.round(sumConsumed),
      sumRealYield: Math.round(sumRealYield),
      sumPvgisYield: Math.round(sumPvgisYield),
      sumCombinedYield: Math.round(sumCombinedYield),
      sumCombinedAutoconsumo: Math.round(sumCombinedAutoconsumo),
      sumCombinedImmissione: Math.round(sumCombinedImmissione),
      
      avgDailyConsumed: Math.round(sumConsumed / realDaily.length),
      avgDailyRealYield: Math.round(sumRealYield / realDaily.length),
      avgDailyPvgisYield: Math.round(sumPvgisYield / realDaily.length),
      avgDailyCombinedYield: Math.round(sumCombinedYield / realDaily.length),
      avgDailyDelta: Math.round((sumConsumed - sumCombinedYield) / realDaily.length),
      
      combinedAutoconsumoPct: Math.round((sumCombinedAutoconsumo / sumCombinedYield) * 100),
      combinedAutosufficienzaPct: Math.round((sumCombinedAutoconsumo / sumConsumed) * 100),
      
      realAutoconsumoPct: Math.round((Math.max(0, sumRealYield - realDaily.reduce((s, d) => s + d.feedIn, 0)) / sumRealYield) * 100),
      realAutosufficienzaPct: Math.round((Math.max(0, sumRealYield - realDaily.reduce((s, d) => s + d.feedIn, 0)) / sumConsumed) * 100)
    },
    monthlyList,
    dailyList: mergedData
  };
  
  fs.writeFileSync('E:\\Gajarda\\pascale-dashboard\\public\\pvgis-data.json', JSON.stringify(results, null, 2), 'utf-8');
  console.log('Saved public/pvgis-data.json!');
}

analyze();
