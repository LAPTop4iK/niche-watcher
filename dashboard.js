'use strict';
const D=JSON.parse(document.getElementById('dataset').textContent);
const $=id=>document.getElementById(id),nf=n=>Number(n).toLocaleString('ru-RU');
const esc=s=>String(s??'').replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
const cats={construction:'Строительство',auto:'Авто',aviation_marine:'Авиация и море',music_practice:'Музыка',hunting_fishing:'Охота и рыбалка',ai_identifiers:'Распознавание предметов',diy_home:'Дом и ремонт',finance_tools:'Финансовые инструменты',photo_video_utils:'Фото и видео',garden:'Сад',cooking:'Кулинария',logistics_freight:'Логистика',needlework:'Рукоделие',parenting:'Родительство',pet_care:'Питомцы',tax_regional:'Местные налоги',travel_utils:'Путешествия',widgets_watch:'Виджеты',driving_ed:'Обучение вождению',books_media:'Книги и медиа',religious:'Религиозные практики',trades_pro:'Рабочие инструменты',ai_tools:'AI-инструменты'};
const cat=c=>c.startsWith('chart:')?'Чарт: '+c.slice(6):c==='similar-graph'?'Ссылки со страниц':cats[c]||c;
const date=v=>v?v.slice(0,10):'—';
const badge=r=>`<span class="badge ${esc(r.verdict)}">${esc(D.labels[r.verdict])}</span>`;
const price=r=>r.price===null||r.price===undefined?'Цена неизвестна':r.price>0?`${r.price} ${r.currency||''}`:'Бесплатная загрузка';
const link=(id,g)=>`https://apps.apple.com/${encodeURIComponent(g)}/app/id${encodeURIComponent(id)}`;
$('updated').textContent='Собрано '+D.built;
$('alive').textContent=nf(D.counts.ALIVE||0);
$('uncertain').textContent=nf((D.counts.WEAK||0)+(D.counts.UNKNOWN||0));
$('coverage').textContent=D.coverage.length+' / 24';
$('velocity').textContent=nf(D.candidates.filter(r=>r.velocity!==null).length);
$('total').textContent=nf(D.candidates.length)+' записей';
$('notice').textContent=D.first?`История: ${date(D.first)} — ${date(D.last)}. Без свежих сигналов: ${nf(D.counts.NO_RECENT_EVIDENCE||0)}. Это не доказывает отсутствие спроса. `+(D.missing.length?'Нет наблюдений: '+D.missing.join(', ').toUpperCase():'Все страны представлены; полнота запросов — в разделе «Что проверено».'):'Наблюдений пока нет.';
function options(id,values,label){for(const value of values){const o=document.createElement('option');o.value=value;o.textContent=label(value);$(id).append(o)}}
options('geo',D.coverage.map(r=>r.country).sort(),v=>v.toUpperCase());
options('category',[...new Set(D.candidates.flatMap(r=>r.categories||[r.category]))].sort(),cat);
let page=0,filtered=[];const pageSize=15;
function detail(r){
  if(!r){$('detail').innerHTML='<h3>Выберите приложение</h3><p class="tiny">Для этих фильтров кандидатов нет.</p>';return}
  const v=r.velocity===null?'—':(r.velocity>0?'+':'')+r.velocity.toFixed(1);
  const category=$('category').value==='all'?r.category:$('category').value;
  const query=((r.source_queries||{})[category]||[(r.category_queries||{})[category]||r.query]).join('», «');
  const geos=(r.observed_countries||[]).map(g=>`<a href="${link(r.track_id,g)}" target="_blank" rel="noopener">${esc(g.toUpperCase())}</a>`).join(' · ');
  const source=(r.categories||[]).length?`${cat(category)} · запрос «${query}». Релевантность нужно проверить.`:'Историческое наблюдение. В последней завершённой выдаче не найдено; это не доказательство удаления.';
  $('detail').innerHTML=`<div class="eyebrow">Основание гипотезы · ${esc(r.country.toUpperCase())}</div><div class="detail-name">${esc(r.name)}</div>${badge(r)}
  <div class="facts"><div><strong>${nf(r.rating_count)}</strong><small>накопленных оценок</small></div><div><strong>${v}</strong><small>оценок / 30 дней</small></div></div>
  ${r.reasons.map(s=>`<div class="reason">${esc(s)}</div>`).join('')}
  <div class="reason"><b>${esc(price(r))}</b><br>Цена загрузки не доказывает текущие продажи. Покупки внутри и внешняя воронка — отдельная проверка.</div>
  ${geos?`<div class="reason">Страны с выбранным сигналом: ${geos}. Метрики карточки относятся к ${esc(r.country.toUpperCase())}.</div>`:''}
  <div class="reason">${esc(source)}</div><div class="reason"><b>Платежи и выручка: неизвестны</b><br>Старая версия не доказывает поломку или уход разработчика.</div>
  <div class="next"><b>Следующий шаг</b><br>${r.verdict==='ALIVE'?'Проверить свежие жалобы и продукт руками. Выбрать покупателя, преимущество и канал. Затем проверить оплату.':'Обновить данные и проверить альтернативы. Отсутствие сигнала у карточки не закрывает гипотезу о боли.'}</div>
  <a class="primary" href="${link(r.track_id,r.country)}" target="_blank" rel="noopener">Открыть в App Store ↗</a><p class="tiny" style="margin-top:12px">Наблюдение ${esc(date(r.scanned_at))} · отзывы ${esc(date(r.checked_at))}</p>`;
}
function render(){
  const q=$('search').value.toLowerCase(),category=$('category').value;
  const matches=D.candidates.filter(r=>($('status').value==='all'||r.verdict===$('status').value)&&($('geo').value==='all'||r.country===$('geo').value)&&(category==='all'||(r.categories||[r.category]).includes(category))&&(r.name+' '+r.seller).toLowerCase().includes(q)&&(!$('complaintsOnly').checked||(r.bad_recent_count||0)>0)&&(!$('paidOnly').checked||(r.price||0)>0));
  if($('groupApps').checked){const groups=new Map();for(const r of matches){if(!groups.has(r.track_id))groups.set(r.track_id,{...r,observed_countries:[]});groups.get(r.track_id).observed_countries.push(r.country)}filtered=[...groups.values()]}else filtered=matches;
  const visible=filtered.slice(page*pageSize,(page+1)*pageSize);
  $('rows').innerHTML=visible.map((r,i)=>`<tr><td><button class="rowbtn" data-index="${i}">${esc(r.name)}</button><div class="tiny">${esc(cat(category==='all'?r.category:category))} · ${esc(price(r))}</div></td><td class="country">${esc(r.country.toUpperCase())}${r.observed_countries&&r.observed_countries.length>1?' +'+(r.observed_countries.length-1):''}</td><td>${r.r90??'—'}<div class="tiny">1–2★: ${r.bad_recent_count??'—'}</div></td><td>${badge(r)}</td><td>${esc(date(r.version_date))}</td></tr>`).join('')||'<tr><td colspan="5" class="empty">Для этих фильтров кандидатов нет.</td></tr>';
  $('shown').textContent=filtered.length?`${page*pageSize+1}–${Math.min((page+1)*pageSize,filtered.length)} из ${nf(filtered.length)} ${$('groupApps').checked?'приложений':'записей'} · R90 — выборка отзывов за 90 дней`:'0 записей';
  $('prev').disabled=page===0;$('next').disabled=(page+1)*pageSize>=filtered.length;
  for(const b of $('rows').querySelectorAll('button'))b.onclick=()=>detail(visible[Number(b.dataset.index)]);
  detail(visible[0]);
}
for(const id of ['search','status','geo','category','groupApps','complaintsOnly','paidOnly'])$(id).addEventListener('input',()=>{page=0;render()});
$('prev').onclick=()=>{page--;render()};$('next').onclick=()=>{page++;render()};
const byGeo=new Map(D.coverage.map(r=>[r.country,r]));
$('geos').innerHTML=[...D.coverage.map(r=>r.country),...D.missing].sort().map(g=>`<div class="geo ${byGeo.has(g)?'':'missing'}" title="${byGeo.has(g)?nf(byGeo.get(g).apps)+' приложений; '+byGeo.get(g).last_seen:'Нет данных'}">${esc(g.toUpperCase())}</div>`).join('');
$('base').textContent=`${nf(D.napps)} уникальных приложений · ${nf(D.observations)} наблюдений. Наблюдение не равно прогону.`;
const categoryBars=D.groups.filter(g=>g.stale).slice(0,8),maxBar=Math.max(1,...categoryBars.map(g=>g.stale));
$('bars').innerHTML=categoryBars.map(g=>`<div class="barrow"><span>${esc(cat(g.category))} · ${esc(g.country.toUpperCase())}</span><div class="bar">${[['alive','#247657'],['weak','#d2a660'],['unknown','#c4ced7'],['no_recent','#e2e7dd']].map(([k,c])=>`<span style="width:${100*g[k]/maxBar}%;background:${c}" title="${k}: ${g[k]}"></span>`).join('')}</div><span>${g.stale}</span></div>`).join('')||'<p class="tiny">Недостаточно наблюдений.</p>';
for(const b of document.querySelectorAll('.tab'))b.onclick=()=>{document.querySelectorAll('.tab').forEach(t=>t.classList.toggle('active',t===b));for(const id of ['research','plan','validation'])$(id).classList.toggle('hidden',id!==b.dataset.view)};
function calc(){
  const v=id=>Number($(id).value),ids=['installs','conv','price','churn','horizon','fee','refund','cost'];
  if(ids.some(id=>!$(id).checkValidity()||$(id).value==='')){$('contribution').textContent='Проверьте значения';$('scenario').textContent='';return}
  let cycles=0;for(let i=0;i<v('horizon');i++)cycles+=(1-v('churn')/100)**i;
  const revenue=v('installs')*v('conv')/100*v('price')*cycles*(1-v('fee')/100)*(1-v('refund')/100),cost=v('installs')*v('cost');
  $('contribution').textContent=new Intl.NumberFormat('ru-RU',{style:'currency',currency:'USD',maximumFractionDigits:0}).format(revenue-cost);
  $('scenario').textContent=`Поступления $${revenue.toFixed(0)} · затраты $${cost.toFixed(0)} · предел затрат на установку $${(revenue/v('installs')).toFixed(2)}`;
}
for(const input of $('inputs').querySelectorAll('input'))input.oninput=calc;
$('runCoverage').innerHTML=(D.runs||[]).map(r=>`<div class="proof"><b>${esc(({'itunes-search':'Поиск App Store','google-play':'Google Play','apple-charts':'Чарты App Store','apple-page-links':'Ссылки App Store'})[r.source]||r.source)} · ${r.status==='COMPLETE'?'Завершён':'Не завершён'}</b><strong>${nf(r.succeeded||0)} / ${nf(r.expected_queries)}</strong><span class="tiny">${esc(r.scope||'')}<br>Пустых ответов: ${nf(r.empty||0)} · ошибок: ${nf(r.errors||0)} · начало ${esc(r.started_at)}</span></div>`).join('')||'<p class="tiny">Исторические сканы не сохраняли журнал запросов. Полнота не доказана.</p>';
const H=D.hypotheses;
$('hypotheses').innerHTML=H&&H.study_id?`<div class="proof"><b>Активность старых и свежих версий</b><strong>${H.matched_old_minus_fresh_pp===null?'Недостаточно данных':H.matched_old_minus_fresh_pp.toFixed(1)+' п.п.'}</strong><p class="tiny">Доля старых минус доля свежих после сопоставления по стране и группам количества оценок и возраста приложения. Сопоставимых групп: ${H.matched_strata}. Исходы на ${esc(date(H.analysis_asof))}. Это связь в выборке, не причинный эффект.</p></div><table><thead><tr><th>Версия</th><th>≥2 отзыва / 90д</th><th>Нет данных</th></tr></thead><tbody>${[['≥18 мес','old'],['<12 мес','fresh']].map(([label,key])=>{const s=H.activity_comparison[key];return `<tr><td>${label}</td><td>${s.active} / ${s.n} · ${s.rate===null?'—':(100*s.rate).toFixed(1)+'%'}</td><td>${s.unknown}</td></tr>`}).join('')}</tbody></table><p class="tiny">Таблица показывает исходные доли. Значение выше учитывает сопоставление. Единица — приложение × страна; страны одного приложения зависимы.</p><div class="proof"><b>Старая версия → прибыль нового конкурента</b><strong>Не проверено</strong><span class="tiny">Нет собственных исходов запуска и сопоставимых затрат.</span></div><p class="tiny">Порог активности — исследовательское допущение. Выборка RSS ограничена; отсутствующие ответы исключены из сравнения.</p>`:'<p class="tiny">Расчёт появится после проверки данных. Отсутствие результата не подтверждает гипотезу.</p>';
$('footer').textContent=`Метод ${D.method} · iTunes Search / RSS · поиск гипотез. Выручка конкурентов из этих данных не измеряется.`;
render();calc();
