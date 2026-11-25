import {api,download} from '../api/client.js';
import {empty,errorSlot} from '../components/common/layout.js';
import {table} from '../components/common/table.js';
import {decisions} from '../constants/enums.js';
import {bindForm} from '../hooks/forms.js';
import {state} from '../stores/session.js';
import {esc,formData,time} from '../utils/html.js';
import {chooseRun,currentRun} from './analysis.js';

export async function review() {
  const {runs,run} = await currentRun();
  if (!run) return empty('先运行分析。','#2','配置分析');
  const stale = run.calibration_stale === 1;
  const staleNotice = stale ? '<p class="error" role="alert">上游校时版本已更新，该报告依据已失效。待发计划已停用，须重新运行分析并复核后才能投递；已送达报告保留当时依据。</p>' : '';
  const gate = run.result?.can_publish === false ? '<p class="notice">当前结果未达到发布门槛（含全偏移组合门槛），可退回复算。</p>' : '';
  const form = run.result && !run.decision && !stale && state.account.role === 'reviewer' ? `<form id="review" data-id="${run.id}"><label>复核意见<textarea name="note" required minlength="2" maxlength="1000"></textarea></label><button type="submit" value="approved" ${gate ? 'disabled' : ''}>批准发布</button> <button type="submit" value="returned" class="secondary">退回复算</button>${errorSlot}</form>` : '';
  return `<section>${chooseRun(runs,run.id)}<h2>${esc(state.profile.decision)}</h2><p>${esc(run.result?.headline || '分析完成后可复核。')}</p>${staleNotice}${gate}${run.decision ? `<p class="notice">${decisions[run.decision]} · ${esc(run.reviewer)} · ${time(run.reviewed)}<br>${esc(run.review_note)}</p>` : ''}${form}<p><a href="#3/${run.id}">打开结果及口径</a> · <a href="#5/${run.id}">查看交付</a></p></section>`;
}

export async function delivery() {
  const [{runs,run},box] = await Promise.all([currentRun(),api('/delivery')]);
  const reviewer = state.account.role === 'reviewer';
  const fixedRecipient = run?.result?.dispatch_recipient;
  const stale = run?.calibration_stale === 1;
  const staleBanner = stale ? '<p class="error" role="alert">该报告的校时依据已被新版本取代，待发计划立即失效；重新复核后的新任务才能投递，旧版本不会恢复资格。</p>' : '';
  const share = reviewer && run?.decision === 'approved' && !stale ? `<form id="schedule" class="inline-form" data-id="${run.id}"><label>站内收件组<input name="recipient" value="${esc(fixedRecipient ?? state.profile.recipient)}" ${fixedRecipient ? 'readonly' : ''} minlength="2" maxlength="100" required></label><label>频率<select name="cadence"><option value="once">单次</option><option value="daily">每日同一时间</option></select></label><label>首次投递时间<input name="scheduled_at" type="datetime-local"></label><button type="submit">建立投递计划</button>${errorSlot}</form>` : '<p class="hint">批准且校时依据仍为当前版本时，复核员可设置投递计划。</p>';
  const exportButton = run?.result && (run.decision === 'approved' || state.account.role !== 'viewer') ? `<button id="report-download" class="secondary" data-id="${run.id}" type="button">下载结果 CSV</button>` : '';
  const planTable = table(box.schedules.map(row => ({计划:row.id,任务:row.run_id,收件组:row.recipient,频率:row.cadence === 'daily' ? '每日' : '单次',计划时间:time(row.next_at),'依据校时版本':row.calibration_version ?? '—',状态:row.active ? '启用' : '关闭'})));
  const stop = reviewer && box.schedules.some(row => row.active) ? `<form id="stop" class="inline-form"><label>启用中的计划<select name="schedule">${box.schedules.filter(row => row.active).map(row => `<option value="${row.id}">#${row.id} · ${esc(row.recipient)}</option>`).join('')}</select></label><button type="submit" class="secondary">停止计划</button>${errorSlot}</form>` : '';
  return `<section><h2>报告分发</h2>${run ? chooseRun(runs,run.id) : ''}${staleBanner}${share}${exportButton}<p id="delivery-error" class="error" role="alert"></p></section><section><h2>投递计划</h2>${planTable}${stop}</section><section><h2>收件箱（已送达保留当时校时依据）</h2>${table(box.deliveries.map(row => ({投递:row.id,收件组:row.recipient,报告:{link:`#3/${row.run_id}`,text:`分析 #${row.run_id}`},'送达时校时版本':row.calibration_version ?? '—',入箱时间:time(row.delivered)})))}</section>`;
}

export function bindDispatch(root,refresh) {
  bindForm(root,'#review',async (form,button) => {
    await api(`/runs/${form.dataset.id}/review`,{decision:button.value,note:form.elements.note.value});
    state.status = '复核意见已保存。';
  },refresh);
  bindForm(root,'#schedule',async form => {
    const data = formData(form);
    data.scheduled_at = data.scheduled_at ? new Date(data.scheduled_at).toISOString() : null;
    await api(`/runs/${form.dataset.id}/schedules`,data);
    state.status = '投递计划已建立。首次时间留空时点此模块可刷新收件箱。';
  },refresh);
  bindForm(root,'#stop',async form => {
    await api(`/schedules/${form.elements.schedule.value}`,null,'DELETE');
    state.status = '已停止投递。';
  },refresh);
  root.querySelector('#report-download')?.addEventListener('click',async event => {
    const key = event.currentTarget.dataset.id;
    try {await download(`/runs/${key}/report.csv`,`${state.profile.title}-${key}.csv`);}
    catch (error) {root.querySelector('#delivery-error').textContent = error.message;}
  });
}
