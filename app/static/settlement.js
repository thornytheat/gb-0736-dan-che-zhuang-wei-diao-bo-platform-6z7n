import {$, api, clock, date, kpis, node, notice, post, showError, table, time} from './view.js';

const local = value => value ? new Date(Date.parse(value) + 8 * 3600000).toISOString().slice(0, 19) : '';
const apiTime = value => value ? `${value.length === 16 ? value + ':00' : value}+08:00` : '';
const OUTCOMES = [['delivered', '到站签收（落地）'], ['returned', '管制返程（回装车站）']];

let runId, shift, waybill = [], vouchers = [], previewed = null, history = [], selectedVersion = null, busy = false;

function select(field, label, values, chosen) {
  const element = node('select');
  element.dataset.field = field;
  element.setAttribute('aria-label', label);
  for (const [value, name] of values) {
    const option = node('option', name);
    option.value = value;
    option.selected = value === chosen;
    element.append(option);
  }
  return element;
}

function box(field, label, type, value) {
  const element = node('input');
  element.type = type;
  element.dataset.field = field;
  element.setAttribute('aria-label', label);
  element.value = value ?? '';
  element.required = true;
  return element;
}

function stationName(code) {
  return shift.stations[code] ? `${shift.stations[code].name} · ${code}` : code;
}

function readVouchers() {
  return [...$('voucher-editor').querySelectorAll('tbody tr')].map(row => {
    const get = key => row.querySelector(`[data-field="${key}"]`).value;
    return {
      voucher_id: get('voucher_id'), leg_id: get('leg_id'), truck: get('truck'),
      station: get('station'), at: apiTime(get('at')),
      count: get('count') === '' ? '' : Number(get('count')), outcome: get('outcome'),
    };
  });
}

function renderEditor() {
  const legIds = waybill.map(leg => [leg.id, `${leg.id} ${leg.truck} ${leg.from}→${leg.to}`]);
  const trucks = [...new Set(waybill.map(leg => leg.truck))].map(id => [id, id]);
  const stations = Object.entries(shift.stations).map(([code, row]) => [code, `${row.name} · ${code}`]);
  table('voucher-editor',
    ['凭证号', '行程', '车辆', '现场站点', '凭证时点', '辆数', '现场结论', '操作'],
    vouchers.map((voucher, index) => {
      const at = box('at', `凭证 ${voucher.voucher_id} 时点`, 'datetime-local', local(voucher.at));
      at.step = '1';
      const count = box('count', `凭证 ${voucher.voucher_id} 辆数`, 'number', voucher.count);
      count.min = '1';
      const remove = node('button', '移除', 'link');
      remove.type = 'button';
      remove.addEventListener('click', () => {
        vouchers.splice(index, 1);
        invalidate();
      });
      return [
        box('voucher_id', '凭证号', 'text', voucher.voucher_id),
        select('leg_id', '对应行程', legIds, voucher.leg_id),
        select('truck', '车辆', trucks, voucher.truck),
        select('station', '现场站点', stations, voucher.station),
        at, count, select('outcome', '现场结论', OUTCOMES, voucher.outcome), remove,
      ];
    }), '按派车单生成凭证或手工新增');
  // 切换现场结论时自动带出对应站点：落地=目的地，返程=装车站，避免手工选错被整单拒绝。
  for (const row of $('voucher-editor').querySelectorAll('tbody tr')) {
    const legField = row.querySelector('[data-field="leg_id"]');
    const outcomeField = row.querySelector('[data-field="outcome"]');
    const stationField = row.querySelector('[data-field="station"]');
    const truckField = row.querySelector('[data-field="truck"]');
    const countField = row.querySelector('[data-field="count"]');
    const sync = () => {
      const leg = waybill.find(item => item.id === legField.value);
      if (!leg) return;
      stationField.value = outcomeField.value === 'delivered' ? leg.to : leg['from'];
      truckField.value = leg.truck;
      countField.value = leg.count;
    };
    legField.addEventListener('change', sync);
    outcomeField.addEventListener('change', () => {
      sync();
      vouchers = readVouchers();
      invalidate();
    });
  }
}

function invalidate() {
  previewed = null;
  $('settle-publish').disabled = true;
  renderConclusions();
}

function renderConclusions() {
  const data = previewed;
  if (!data) {
    kpis([['结算版本', history.length, '版'], ['当前结论', selectedVersion ? '历史查看' : '未出具']]);
    table('settle-legs', [], [], '先试算并发布结算结论');
    table('settle-stations', [], [], '先试算并发布结算结论');
    $('settle-summary').textContent = '';
    return;
  }
  const c = data.conclusion;
  kpis([['到站整趟', c.delivered_legs, '趟', 'ok'], ['管制返程', c.returned_legs, '趟', 'warn'],
    ['落地车辆', c.bikes_landed, '辆'], ['退回装车站', c.bikes_returned, '辆'],
    ['期末在途', c.in_transit_end, '辆', c.in_transit_end ? 'warn' : 'ok']]);
  $('settle-summary').textContent = c.summary;
  table('settle-legs',
    ['行程', '车辆', '装车', '目的地', '现场结论', '凭证', '结算时点', '落地', '返程空驶', '回车站', '结算工时段'],
    data.legs.map(leg => [
      leg.leg_id, leg.truck, stationName(leg['from']), stationName(leg.to),
      leg.status === 'delivered' ? node('span', '到站签收', 'ok') : node('span', '管制返程', 'bad'),
      leg.voucher_id, time(leg.settled_at), leg.landed_count, leg.empty_return_count,
      leg.return_station ? stationName(leg.return_station) : '—', leg.pay_segments.join(' + '),
    ]));
  table('settle-stations',
    ['站点', '可借车', '暂存', '空桩', '本次退回', '基准未成交', '派车单未成交', '实际未成交', '对派车单差量', '对基准差量'],
    data.stations.map(row => [
      stationName(row.station), row.borrowable, row.held, row.free_slots, row.returned_bikes,
      row.missed_base, row.missed_waybill, row.missed_actual,
      delta(row.missed_delta_vs_waybill), delta(row.missed_delta_vs_base),
    ]));
}

function delta(value) {
  const cell = node('span', value > 0 ? `+${value}` : String(value), value > 0 ? 'bad' : value < 0 ? 'ok' : '');
  return cell;
}

function renderHistory() {
  $('settle-state').textContent = selectedVersion
    ? `正在核对历史版本 ${selectedVersion.slice(0, 12)}`
    : history.length ? `当前结论 ${history.find(row => row.active)?.id.slice(0, 12) || '—'}` : '未出具结论';
  table('settle-history', ['版本', '生成时间', '到站/返程（趟）', '落地/退回（辆）', '状态', '操作'],
    history.map(row => {
      const actions = node('span', '', 'row-actions');
      const button = node('button', row.id === selectedVersion ? '正在查看' : '重开核对', 'btn');
      button.disabled = row.id === selectedVersion;
      button.addEventListener('click', () => openVersion(row.id));
      const link = node('a', '下载证据', 'btn');
      link.href = `/api/settlement/${encodeURIComponent(row.id)}/evidence.json`;
      actions.append(button, link);
      return [row.id.slice(0, 12), `${date(row.generated_at)} ${clock(row.generated_at)}`,
        `${row.delivered_legs} / ${row.returned_legs}`, `${row.bikes_landed} / ${row.bikes_returned}`,
        row.active ? '当前结论' : '历史留档', actions];
    }), '还没有结算版本');
  $('settle-restore').hidden = !selectedVersion || history.some(row => row.active && row.id === selectedVersion);
}

async function openVersion(id) {
  selectedVersion = id;
  const payload = await api(`/api/settlement/${encodeURIComponent(id)}`);
  previewed = payload.settlement;
  renderConclusions();
  renderHistory();
  notice(`已重开核对版本 ${id.slice(0, 12)} · 只读，原凭证未改动`);
}

async function refresh(currentRun, currentShift, currentWaybill) {
  runId = currentRun;
  shift = currentShift;
  waybill = currentWaybill;
  const payload = await api('/api/settlement');
  history = payload.history;
  if (!selectedVersion && payload.active) selectedVersion = payload.active;
  if (!vouchers.length && waybill.length) fillFromWaybill();
  renderEditor();
  if (selectedVersion) await openVersion(selectedVersion);
  else {
    previewed = null;
    renderConclusions();
    renderHistory();
  }
}

function fillFromWaybill() {
  vouchers = waybill.map((leg, index) => ({
    voucher_id: `V-${String(index + 1).padStart(2, '0')}`, leg_id: leg.id, truck: leg.truck,
    station: leg.to, at: leg.arrive, count: leg.count, outcome: 'delivered',
  }));
}

function wire() {
  $('voucher-sample').addEventListener('click', () => {
    fillFromWaybill();
    invalidate();
    renderEditor();
    notice('已按原派车单生成凭证草稿，请按现场记录改结论和站点');
  });
  $('voucher-add').addEventListener('click', () => {
    if (!waybill.length) return;
    const leg = waybill[0];
    vouchers.push({voucher_id: `V-${String(vouchers.length + 1).padStart(2, '0')}`,
      leg_id: leg.id, truck: leg.truck, station: leg.to, at: leg.arrive, count: leg.count,
      outcome: 'delivered'});
    invalidate();
    renderEditor();
  });
  $('settle-form').addEventListener('input', () => {
    vouchers = readVouchers();
    invalidate();
  });
  $('settle-form').addEventListener('submit', event => {
    event.preventDefault();
    submit(false);
  });
  $('settle-publish').addEventListener('click', () => submit(true));
  $('settle-restore').addEventListener('click', async () => {
    if (!selectedVersion || busy) return;
    busy = true;
    try {
      const result = await post('/api/settlement/restore',
        {run: runId, settlement: selectedVersion, expected: runId});
      notice(`已回切版本 ${result.id.slice(0, 12)}`);
      selectedVersion = result.id;
      await refresh(runId, shift, waybill);
    } catch (error) {
      showError(error);
    } finally {
      busy = false;
    }
  });
}

async function submit(publish) {
  if (busy) return;
  const draft = readVouchers();
  busy = true;
  notice(publish ? '正在发布结算结论…' : '正在试算…');
  try {
    if (publish) {
      const result = await post('/api/settlement/publish', {run: runId, vouchers: draft});
      notice(result.duplicate ? '同一现场凭证已结算，沿用原结论，未重复结算'
        : result.superseded ? '已发布新版本，旧版结论与凭证已留档可回切' : '结算结论已发布');
      selectedVersion = result.id;
      await refresh(runId, shift, waybill);
    } else {
      const payload = await api('/api/settlement/preview', {
        method: 'POST', headers: {'Content-Type': 'application/json'},
        body: JSON.stringify({run: runId, vouchers: draft}),
      });
      previewed = payload.settlement;
      selectedVersion = null;
      renderConclusions();
      renderHistory();
      $('settle-publish').disabled = false;
      notice('试算完成，核对无误后可发布');
    }
  } catch (error) {
    showError(error);
  } finally {
    busy = false;
  }
}

function reset() {
  runId = shift = null;
  waybill = [];
  vouchers = [];
  previewed = null;
  history = [];
  selectedVersion = null;
  busy = false;
}

export const settlement = {refresh, wire, reset};
