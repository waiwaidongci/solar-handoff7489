import {display,esc} from '../../utils/html.js';
import {table} from './table.js';

function chart(spec) {
  const points = (spec?.points || []).filter(point => Number.isFinite(point.value)).slice(0,12);
  if (!points.length) return '';
  const max = Math.max(...points.map(point => Math.abs(point.value)), 1e-9);
  return `<figure><figcaption>${esc(spec.label)} ${spec.unit ? `· ${esc(spec.unit)}` : ''}</figcaption>${points.map(point => {
    const proportion = Math.abs(point.value) / max * 47;
    const start = point.value < 0 ? 50 - proportion : 50;
    return `<div class="chart-row"><span>${esc(point.label)}</span><div class="chart-track"><i style="inset-inline-start:${start}%;width:${proportion}%"></i></div><strong>${esc(display(point.value))}</strong></div>`;
  }).join('')}</figure>`;
}

function calibrationPanel(result) {
  const report = result.calibration_report;
  if (!report) return '';
  const basisColumns = [{key:'校时批次',label:'校时批次'},{key:'校时版本',label:'校时版本'},
    {key:'允许偏移下限（秒）',label:'偏移下限（秒）'},{key:'允许偏移上限（秒）',label:'偏移上限（秒）'}];
  const affectedColumns = [{key:'逆变器',label:'受影响设备'},{key:'失败组合数',label:'失败组合数'},{key:'见证偏移组合',label:'会失败的一组偏移'}];
  const failed = !!report.受影响设备?.length;
  return `<section class="calibration ${failed ? 'calibration-fail' : 'calibration-ok'}">
    <h3>网关校时与全偏移组合门槛</h3>
    <p class="notice">${esc(report.门槛模式)} · ${esc(report.结论)}</p>
    <h4>批次校时依据（同批共用一个偏移，各批可不同）</h4>
    ${table(report.校时依据,basisColumns)}
    ${report.失败偏移组合 ? `<p class="error" role="alert">会失败的一组偏移：${esc(report.失败偏移组合)}</p>` : ''}
    <h4>受影响设备明细</h4>
    ${table(report.受影响设备,affectedColumns)}
  </section>`;
}

export function report(result) {
  const metrics = (result.metric_order || Object.keys(result.metrics)).map(key => [key,result.metrics[key]]);
  const columns = result.columns?.map(key => ({key,label:key}));
  return `<div class="reading"><h2>${esc(result.headline)}</h2><dl class="metrics">${metrics.map(([key,value]) => `<div><dt>${esc(key)}</dt><dd>${esc(display(value))}</dd></div>`).join('')}</dl>${chart(result.chart)}${calibrationPanel(result)}<h3>业务明细</h3>${table(result.rows,columns)}<h3>口径与限制</h3><ul>${result.notes.map(note => `<li>${esc(note)}</li>`).join('')}</ul></div>`;
}
