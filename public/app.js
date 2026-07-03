// ============================================
// PASCALE 500kW - Dashboard Interattiva
// SolaxCloud API - Multi-Inverter + Storico
// ============================================

const PLANT_CAPACITY_KW = 500;
const REFRESH_INTERVAL = 60000;
const CO2_FACTOR = 0.4;

let powerHistory = [];
let charts = {};
let currentTab = 'realtime';
let selectedInverter = '';

const COLORS = ['#fbbf24','#10b981','#3b82f6','#8b5cf6','#ef4444','#06b6d4'];
const INV_LABELS = {
  'H34A15IA529024':'Hybrid 15kW','X3F100J3116121':'X3F 100kW #1','X3F100J3116094':'X3F 100kW #2',
  'A3F080J6733015':'A3F 80kW','A3F100J7057023':'A3F 100kW #1','A3F100L7869005':'A3F 100kW #2'
};
const CHART_GRID = { color: 'rgba(255,255,255,0.05)' };
const CHART_TICK = { color: '#6b7280', font: { size: 10 } };

// ---- INIT ----
document.addEventListener('DOMContentLoaded', () => {
  initDatePickers();
  initRealtimeCharts();
  fetchRealtime();
  setInterval(fetchRealtime, REFRESH_INTERVAL);
});

function initDatePickers() {
  const now = new Date();
  document.getElementById('day-picker').value = now.toISOString().slice(0,10);
  document.getElementById('month-picker-m').value = now.getMonth() + 1;
  const ySelM = document.getElementById('month-picker-y');
  const ySelY = document.getElementById('year-picker');
  for (let y = now.getFullYear(); y >= 2020; y--) {
    ySelM.innerHTML += `<option value="${y}">${y}</option>`;
    ySelY.innerHTML += `<option value="${y}">${y}</option>`;
  }
}

// ---- TAB SWITCHING ----
function switchTab(tab) {
  document.querySelectorAll('[data-tab]').forEach(b => {
    b.classList.remove('tab-active');
    b.classList.add('text-gray-400');
  });
  document.querySelector(`[data-tab="${tab}"]`).classList.add('tab-active');
  document.querySelector(`[data-tab="${tab}"]`).classList.remove('text-gray-400');

  ['realtime','day','month','year','pvgis','export'].forEach(t => {
    document.getElementById(`tab-${t}`).classList.toggle('hidden', t !== tab);
  });
  currentTab = tab;

  if (tab === 'day') loadDayData();
  if (tab === 'month') loadMonthData();
  if (tab === 'year') loadYearData();
  if (tab === 'pvgis') loadPvgisData();
  if (tab === 'export') initExportTab();
}

function onFilterChange() {
  selectedInverter = document.getElementById('inverter-filter').value;
  if (currentTab === 'day') loadDayData();
  if (currentTab === 'month') loadMonthData();
  if (currentTab === 'year') loadYearData();
}

// ---- REALTIME ----
async function fetchRealtime() {
  try {
    const res = await fetch('/api/realtime');
    const data = await res.json();
    if (data.success && Array.isArray(data.result)) {
      updateRealtime(data.result);
      populateInverterFilter(data.result);
      setStatus('online', `Online - ${data.result.length} inverter`);
    } else {
      setStatus('error', data.exception || 'Errore');
    }
  } catch (e) {
    setStatus('error', 'Connessione persa');
  }
}

function populateInverterFilter(inverters) {
  const sel = document.getElementById('inverter-filter');
  if (sel.options.length > 1) return;
  inverters.forEach(inv => {
    const opt = document.createElement('option');
    opt.value = inv.inverterSN;
    opt.textContent = INV_LABELS[inv.inverterSN] || inv.inverterSN;
    sel.appendChild(opt);
  });
}

function updateRealtime(inverters) {
  let totalP=0,totalYT=0,totalYTot=0,totalFI=0,totalFIE=0,totalCE=0,active=0,hybrid=null;
  inverters.forEach(i => {
    const p = parseFloat(i.acpower)||0;
    totalP+=p; totalYT+=parseFloat(i.yieldtoday)||0; totalYTot+=parseFloat(i.yieldtotal)||0;
    totalFI+=parseFloat(i.feedinpower)||0; totalFIE+=parseFloat(i.feedinenergy)||0;
    totalCE+=parseFloat(i.consumeenergy)||0;
    if(p>0) active++;
    if(i.inverterType==='14') hybrid=i;
  });
  const kw = totalP/1000;
  const pct = Math.min(kw/PLANT_CAPACITY_KW*100,100);

  setText('kpi-power', kw.toFixed(1));
  setBar('power-bar', pct);
  setText('power-percent', `${pct.toFixed(0)}% di ${PLANT_CAPACITY_KW} kWp`);
  setText('kpi-today', totalYT.toFixed(1));
  setText('today-equivalent', `~ ${(totalYT/PLANT_CAPACITY_KW).toFixed(1)} ore eq.`);
  setText('kpi-total', (totalYTot/1000).toFixed(1));
  setText('co2-saved', `~ ${(totalYTot*CO2_FACTOR/1000).toFixed(1)} ton CO2`);
  setText('kpi-active', active);
  setText('kpi-total-inv', inverters.length);
  setText('inverter-summary', active===inverters.length?'Tutti operativi':`${inverters.length-active} fermi`);
  setText('feed-in-power', (Math.abs(totalFI)/1000).toFixed(1)+' kW');
  setText('feed-in-energy', totalFIE.toFixed(0));
  setText('grid-power', (Math.abs(totalFI)/1000).toFixed(1)+' kW');
  setText('consume-energy', (totalCE/1000).toFixed(1));
  if(hybrid){
    const soc=parseFloat(hybrid.soc)||0, bp=parseFloat(hybrid.batPower)||0;
    setText('bat-info', soc+' %');
    setText('bat-power', `${bp>0?'Scarica':bp<0?'Carica':'Idle'}: ${(Math.abs(bp)/1000).toFixed(1)} kW`);
  }
  const lt = inverters.reduce((a,i)=>i.uploadTime>a?i.uploadTime:a,'');
  setText('last-update', 'Agg: '+lt);

  // Charts
  const labels = inverters.map(i => INV_LABELS[i.inverterSN]||i.inverterSN.slice(-6));
  updateBarChart(charts.invBar, labels, inverters.map(i=>(parseFloat(i.acpower)||0)/1000));
  updateBarChart(charts.yieldBar, labels, inverters.map(i=>parseFloat(i.yieldtoday)||0));

  const now = new Date().toLocaleTimeString('it-IT',{hour:'2-digit',minute:'2-digit'});
  powerHistory.push({t:now,v:kw});
  if(powerHistory.length>60) powerHistory.shift();
  charts.powerLine.data.labels = powerHistory.map(p=>p.t);
  charts.powerLine.data.datasets[0].data = powerHistory.map(p=>p.v);
  charts.powerLine.update('none');

  renderInverterCards(inverters);
}

// ---- DAY VIEW ----
function navigateDay(dir) {
  const dp = document.getElementById('day-picker');
  const d = new Date(dp.value);
  d.setDate(d.getDate()+dir);
  dp.value = d.toISOString().slice(0,10);
  loadDayData();
}

async function loadDayData() {
  const date = document.getElementById('day-picker').value;
  const inv = selectedInverter ? `&inverter=${selectedInverter}` : '';
  const res = await fetch(`/api/history/day?date=${date}${inv}`);
  const data = await res.json();

  if(!data.data || data.data.length===0) {
    if(data.source === 'import' && data.imported) {
      // Mostra dati giornalieri importati (senza grafico intraday)
      setText('day-yield', data.imported.yield.toFixed(1)+' kWh');
      setText('day-peak', '-- kW');
      setText('day-hours', (data.imported.yield/PLANT_CAPACITY_KW).toFixed(2));
      setText('day-samples', '0');
      setText('day-info', `Dati importati da SolaxCloud | Feed-in: ${data.imported.feedIn} kWh | Da Rete: ${data.imported.fromGrid} kWh | Consumo: ${data.imported.consumed} kWh`);
      if(charts.dayPower) { charts.dayPower.destroy(); charts.dayPower=null; }
      if(charts.dayYield) { charts.dayYield.destroy(); charts.dayYield=null; }
      const canvas1 = document.getElementById('day-power-chart');
      const canvas2 = document.getElementById('day-yield-chart');
      charts.dayPower = makeLineChart('day-power-chart','Potenza (kW)',[],[],  '#fbbf24');
      charts.dayYield = makeLineChart('day-yield-chart','Produzione (kWh)',[],[],'#10b981');
      return;
    }
    setText('day-info', 'Nessun dato per questa data');
    setText('day-yield','-- kWh'); setText('day-peak','-- kW');
    setText('day-hours','--'); setText('day-samples','0');
    if(charts.dayPower) { charts.dayPower.data.labels=[]; charts.dayPower.data.datasets[0].data=[]; charts.dayPower.update(); }
    if(charts.dayYield) { charts.dayYield.data.labels=[]; charts.dayYield.data.datasets[0].data=[]; charts.dayYield.update(); }
    return;
  }

  const lastPoint = data.data[data.data.length-1];
  const peakP = Math.max(...data.data.map(d=>d.power));
  setText('day-yield', lastPoint.yield.toFixed(1)+' kWh');
  setText('day-peak', peakP.toFixed(1)+' kW');
  setText('day-hours', (lastPoint.yield/PLANT_CAPACITY_KW).toFixed(2));
  setText('day-samples', data.points);
  setText('day-info', `${data.points} campionamenti`);

  const labels = data.data.map(d=>d.time);
  const powers = data.data.map(d=>d.power);
  const yields = data.data.map(d=>d.yield);

  if(!charts.dayPower) {
    charts.dayPower = makeLineChart('day-power-chart','Potenza (kW)',powers,labels,'#fbbf24');
    charts.dayYield = makeLineChart('day-yield-chart','Produzione (kWh)',yields,labels,'#10b981');
  } else {
    charts.dayPower.data.labels=labels; charts.dayPower.data.datasets[0].data=powers; charts.dayPower.update();
    charts.dayYield.data.labels=labels; charts.dayYield.data.datasets[0].data=yields; charts.dayYield.update();
  }
}

// ---- MONTH VIEW ----
function navigateMonth(dir) {
  const mSel = document.getElementById('month-picker-m');
  const ySel = document.getElementById('month-picker-y');
  let m = parseInt(mSel.value)+dir, y = parseInt(ySel.value);
  if(m<1){m=12;y--;}if(m>12){m=1;y++;}
  mSel.value=m; ySel.value=y;
  loadMonthData();
}

async function loadMonthData() {
  const m = document.getElementById('month-picker-m').value;
  const y = document.getElementById('month-picker-y').value;
  const inv = selectedInverter ? `&inverter=${selectedInverter}` : '';
  const res = await fetch(`/api/history/month?month=${m}&year=${y}${inv}`);
  const data = await res.json();

  setText('month-yield', data.totalYield.toFixed(1)+' kWh');
  const daysN = data.days.length;
  setText('month-avg', daysN>0?(data.totalYield/daysN).toFixed(1)+' kWh':'--');
  setText('month-days', daysN);
  setText('month-info', `${daysN} giorni con dati`);

  const labels = data.days.map(d=>d.day);
  const yields = data.days.map(d=>d.yield);

  if(charts.month) { charts.month.destroy(); charts.month = null; }
  charts.month = makeBarChart('month-chart', yields, labels, '#10b981');
}

// ---- YEAR VIEW ----
function navigateYear(dir) {
  const sel = document.getElementById('year-picker');
  sel.value = parseInt(sel.value)+dir;
  loadYearData();
}

async function loadYearData() {
  const y = document.getElementById('year-picker').value;
  const inv = selectedInverter ? `&inverter=${selectedInverter}` : '';
  const res = await fetch(`/api/history/year?year=${y}${inv}`);
  const data = await res.json();

  setText('year-yield', (data.totalYield/1000).toFixed(1)+' MWh');
  const mWithData = data.months.filter(m=>m.yield>0).length;
  setText('year-avg', mWithData>0?((data.totalYield/1000)/mWithData).toFixed(1)+' MWh':'--');
  setText('year-info', `${mWithData} mesi con dati`);

  const labels = data.months.map(m=>m.label);
  const yields = data.months.map(m=>m.yield);

  if(charts.year) { charts.year.destroy(); charts.year = null; }
  charts.year = makeBarChart('year-chart', yields, labels, '#3b82f6');
}

// ---- PVGIS ANALYSIS VIEW ----
async function loadPvgisData() {
  const res = await fetch('/pvgis-data.json');
  const data = await res.json();
  const sum = data.summary;

  setText('pvgis-yield', (sum.sumPvgisYield / 1000).toFixed(1) + ' MWh');
  setText('pvgis-avg-yield', sum.avgDailyPvgisYield);
  setText('pvgis-consumed', (sum.sumConsumed / 1000).toFixed(1) + ' MWh');
  setText('pvgis-avg-consumed', sum.avgDailyConsumed);
  setText('pvgis-self-consume', (sum.sumPvgisAutoconsumo / 1000).toFixed(1) + ' MWh');
  setText('pvgis-self-consume-pct', sum.pvgisAutoconsumoPct);
  setText('pvgis-autosuff', sum.pvgisAutosufficienzaPct);
  setText('pvgis-feedin', (sum.sumPvgisImmissione / 1000).toFixed(1));

  // Build Month-by-Month comparison chart
  const months = data.monthlyList.map(m => {
    // Format YYYY-MM to Month name
    const parts = m.month.split('-');
    const mNames = ['Gen','Feb','Mar','Apr','Mag','Giu','Lug','Ago','Set','Ott','Nov','Dic'];
    return mNames[parseInt(parts[1])-1] + ' ' + parts[0].substring(2);
  });

  const consumed = data.monthlyList.map(m => m.consumed);
  const realYield = data.monthlyList.map(m => m.realYield);
  const pvgisYield = data.monthlyList.map(m => m.pvgisYield);
  const combinedYield = data.monthlyList.map(m => m.combinedYield);
  const combinedAutoconsumo = data.monthlyList.map(m => m.combinedAutoconsumo);

  if (charts.pvgis) { charts.pvgis.destroy(); charts.pvgis = null; }

  charts.pvgis = new Chart(document.getElementById('pvgis-chart'), {
    type: 'bar',
    data: {
      labels: months,
      datasets: [
        {
          label: 'Fabbisogno (Consumo)',
          data: consumed,
          backgroundColor: 'rgba(239, 68, 68, 0.7)', // Red
          borderRadius: 4,
          borderWidth: 0
        },
        {
          label: 'Produzione Esistente (500kWp)',
          data: realYield,
          backgroundColor: 'rgba(59, 130, 246, 0.5)', // Blue
          borderRadius: 4,
          borderWidth: 0
        },
        {
          label: 'Potenziamento PVGIS (200kWp)',
          data: pvgisYield,
          backgroundColor: 'rgba(16, 185, 129, 0.6)', // Green
          borderRadius: 4,
          borderWidth: 0
        },
        {
          label: 'Produzione Combinata (700kWp)',
          data: combinedYield,
          backgroundColor: 'rgba(139, 92, 246, 0.6)', // Purple
          borderRadius: 4,
          borderWidth: 0
        },
        {
          label: 'Autoconsumo Combinato',
          data: combinedAutoconsumo,
          backgroundColor: 'rgba(245, 158, 11, 0.8)', // Orange/Solar
          borderRadius: 4,
          borderWidth: 0
        }
      ]
    },
    options: {
      responsive: true,
      maintainAspectRatio: false,
      plugins: {
        legend: {
          position: 'top',
          labels: { color: '#e5e7eb', font: { size: 11 } }
        },
        tooltip: {
          mode: 'index',
          intersect: false
        }
      },
      scales: {
        x: {
          grid: { display: false },
          ticks: CHART_TICK
        },
        y: {
          beginAtZero: true,
          grid: CHART_GRID,
          ticks: { ...CHART_TICK, callback: v => v + ' kWh' }
        }
      }
    }
  });
}

// ---- CHART BUILDERS ----
function initRealtimeCharts() {
  charts.invBar = new Chart(document.getElementById('inverter-bar-chart'), {
    type:'bar', data:{labels:[],datasets:[{data:[],backgroundColor:COLORS,borderWidth:0,borderRadius:6}]},
    options:{responsive:true,maintainAspectRatio:false,plugins:{legend:{display:false}},
      scales:{x:{grid:{display:false},ticks:{color:'#9ca3af',font:{size:9}}},y:{beginAtZero:true,grid:CHART_GRID,ticks:{...CHART_TICK,callback:v=>v+' kW'}}}}
  });
  charts.yieldBar = new Chart(document.getElementById('yield-bar-chart'), {
    type:'bar', data:{labels:[],datasets:[{data:[],backgroundColor:COLORS,borderWidth:0,borderRadius:6}]},
    options:{responsive:true,maintainAspectRatio:false,plugins:{legend:{display:false}},
      scales:{x:{grid:{display:false},ticks:{color:'#9ca3af',font:{size:9}}},y:{beginAtZero:true,grid:CHART_GRID,ticks:{...CHART_TICK,callback:v=>v+' kWh'}}}}
  });
  charts.powerLine = new Chart(document.getElementById('power-chart'), {
    type:'line', data:{labels:[],datasets:[{data:[],borderColor:'#fbbf24',backgroundColor:'rgba(251,191,36,0.1)',fill:true,tension:0.4,pointRadius:2,pointBackgroundColor:'#fbbf24'}]},
    options:{responsive:true,maintainAspectRatio:false,plugins:{legend:{display:false}},
      scales:{x:{grid:CHART_GRID,ticks:CHART_TICK},y:{beginAtZero:true,suggestedMax:PLANT_CAPACITY_KW,grid:CHART_GRID,ticks:{...CHART_TICK,callback:v=>v+' kW'}}}}
  });
}

function makeLineChart(id, label, data, labels, color) {
  return new Chart(document.getElementById(id), {
    type:'line', data:{labels,datasets:[{label,data,borderColor:color,backgroundColor:color+'22',fill:true,tension:0.3,pointRadius:2,pointBackgroundColor:color}]},
    options:{responsive:true,maintainAspectRatio:false,plugins:{legend:{display:false}},
      scales:{x:{grid:CHART_GRID,ticks:CHART_TICK},y:{beginAtZero:true,grid:CHART_GRID,ticks:CHART_TICK}}}
  });
}

function makeBarChart(id, data, labels, color) {
  return new Chart(document.getElementById(id), {
    type:'bar', data:{labels,datasets:[{data,backgroundColor:color+'cc',borderWidth:0,borderRadius:4}]},
    options:{responsive:true,maintainAspectRatio:false,plugins:{legend:{display:false}},
      scales:{x:{grid:{display:false},ticks:CHART_TICK},y:{beginAtZero:true,grid:CHART_GRID,ticks:{...CHART_TICK,callback:v=>v+' kWh'}}}}
  });
}

function updateBarChart(chart, labels, data) {
  chart.data.labels=labels; chart.data.datasets[0].data=data; chart.update('none');
}

// ---- INVERTER CARDS ----
function renderInverterCards(inverters) {
  const c = document.getElementById('inverter-cards');
  c.innerHTML = inverters.map((inv,i) => {
    const p=(parseFloat(inv.acpower)||0)/1000, yt=parseFloat(inv.yieldtoday)||0;
    const ytot=(parseFloat(inv.yieldtotal)||0)/1000, on=p>0;
    const lbl=INV_LABELS[inv.inverterSN]||inv.inverterSN, col=COLORS[i%COLORS.length];
    const isH=inv.inverterType==='14';
    let bat='';
    if(isH){
      const soc=parseFloat(inv.soc)||0, bp=parseFloat(inv.batPower)||0;
      const dir=bp>0?'Scarica':bp<0?'Carica':'Idle';
      bat=`<div class="mt-3 pt-3 border-t border-white/10"><div class="flex justify-between text-xs"><span class="text-gray-500">Batteria</span><span class="text-cyan-400">${soc}% ${dir} ${(Math.abs(bp)/1000).toFixed(1)}kW</span></div><div class="mt-1 h-1.5 bg-white/5 rounded-full overflow-hidden"><div class="h-full rounded-full" style="width:${soc}%;background:${soc>50?'#10b981':soc>20?'#fbbf24':'#ef4444'}"></div></div></div>`;
    }
    let dc='';
    if(inv.powerdc1!==null||inv.powerdc2!==null){
      dc=`<div class="mt-2 flex gap-2"><span class="text-xs px-2 py-0.5 bg-white/5 rounded text-gray-400">DC1: ${parseFloat(inv.powerdc1)||0}W</span><span class="text-xs px-2 py-0.5 bg-white/5 rounded text-gray-400">DC2: ${parseFloat(inv.powerdc2)||0}W</span></div>`;
    }
    return `<div class="glass rounded-2xl p-5 card-glow"><div class="flex items-center justify-between mb-3"><div class="flex items-center gap-2"><div class="w-3 h-3 rounded-full" style="background:${col}"></div><span class="text-sm font-semibold">${lbl}</span></div><span class="text-xs px-2 py-0.5 rounded-full ${on?'bg-green-500/20 text-green-400':'bg-red-500/20 text-red-400'}">${on?'Attivo':'Fermo'}</span></div><div class="flex items-baseline gap-1 mb-2"><span class="text-2xl font-bold" style="color:${col}">${p.toFixed(1)}</span><span class="text-xs text-gray-400">kW</span></div><div class="grid grid-cols-2 gap-2 text-xs"><div><span class="text-gray-500">Oggi</span><p class="font-medium text-gray-300">${yt.toFixed(1)} kWh</p></div><div><span class="text-gray-500">Totale</span><p class="font-medium text-gray-300">${ytot.toFixed(1)} MWh</p></div></div><p class="text-xs text-gray-600 mt-2">SN: ${inv.inverterSN}</p><p class="text-xs text-gray-600">Agg: ${inv.uploadTime||'--'}</p>${dc}${bat}</div>`;
  }).join('');
}

// ---- HELPERS ----
function setText(id, val) { const el=document.getElementById(id); if(el) el.textContent=val; }
function setBar(id, pct) { const el=document.getElementById(id); if(el) el.style.width=pct+'%'; }
function setStatus(type, text) {
  const dot=document.getElementById('status-dot'), lbl=document.getElementById('status-text');
  const c={online:'bg-green-400 pulse-dot',warning:'bg-yellow-400',error:'bg-red-400',offline:'bg-gray-500'};
  dot.className=`w-2 h-2 rounded-full ${c[type]||c.offline}`; lbl.textContent=text;
}

// ---- EXPORT CSV ----
let exportInited = false;
function initExportTab() {
  const now = new Date();
  if (!document.getElementById('csv-from').value) {
    document.getElementById('csv-from').value = now.toISOString().slice(0,10);
    document.getElementById('csv-to').value = now.toISOString().slice(0,10);
  }
  if (exportInited) return;
  exportInited = true;
  // Popola la select inverter nel pannello export
  fetch('/api/inverters').then(r=>r.json()).then(list => {
    const sel = document.getElementById('csv-inverter');
    list.forEach(inv => {
      const opt = document.createElement('option');
      opt.value = inv.sn;
      opt.textContent = INV_LABELS[inv.sn] || inv.sn;
      sel.appendChild(opt);
    });
  });
}

function setCsvPeriod(period) {
  const now = new Date();
  const to = now.toISOString().slice(0,10);
  let from = to;
  if (period === 'today') {
    from = to;
  } else if (period === 'week') {
    const d = new Date(now); d.setDate(d.getDate()-7);
    from = d.toISOString().slice(0,10);
  } else if (period === 'month') {
    const d = new Date(now); d.setMonth(d.getMonth()-1);
    from = d.toISOString().slice(0,10);
  } else if (period === 'year') {
    const d = new Date(now); d.setFullYear(d.getFullYear()-1);
    from = d.toISOString().slice(0,10);
  } else if (period === 'all') {
    from = '2020-01-01';
  }
  document.getElementById('csv-from').value = from;
  document.getElementById('csv-to').value = to;
}

function downloadCSV() {
  const from = document.getElementById('csv-from').value;
  const to = document.getElementById('csv-to').value;
  const inv = document.getElementById('csv-inverter').value;
  const mode = document.getElementById('csv-mode').value;

  if (!from || !to) {
    setText('csv-status', 'Seleziona le date di inizio e fine.');
    return;
  }
  if (from > to) {
    setText('csv-status', 'La data inizio deve essere precedente alla data fine.');
    return;
  }

  let url = `/api/export/csv?from=${from}&to=${to}&mode=${mode}`;
  if (inv) url += `&inverter=${inv}`;

  setText('csv-status', 'Download in corso...');

  const a = document.createElement('a');
  a.href = url;
  a.download = '';
  document.body.appendChild(a);
  a.click();
  document.body.removeChild(a);

  setTimeout(() => {
    const label = inv ? (INV_LABELS[inv] || inv) : 'Impianto';
    setText('csv-status', `CSV scaricato: ${label} dal ${from} al ${to} (${mode === 'daily' ? 'riepilogo' : 'dettaglio'})`);
  }, 1000);
}
