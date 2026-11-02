'use strict';
const D=JSON.parse(document.getElementById('dataset').textContent);
const $=id=>document.getElementById(id),nf=n=>Number(n).toLocaleString('ru-RU');
const esc=s=>String(s??'').replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
const cats={fitness_calc:'Фитнес-инструменты',boardgames:'Настольные инструменты',construction:'Строительство',auto:'Авто',aviation_marine:'Авиация и море',music_practice:'Музыка',hunting_fishing:'Охота и рыбалка',ai_identifiers:'Распознавание предметов',diy_home:'Дом и ремонт',finance_tools:'Финансовые инструменты',photo_video_utils:'Фото и видео',garden:'Сад',cooking:'Кулинария',logistics_freight:'Логистика',needlework:'Рукоделие',parenting:'Родительство',pet_care:'Питомцы',tax_regional:'Местные налоги',travel_utils:'Путешествия',widgets_watch:'Виджеты',driving_ed:'Обучение вождению',books_media:'Книги и медиа',religious:'Религиозные практики',trades_pro:'Рабочие инструменты',ai_tools:'AI-инструменты'};
const cat=c=>c.startsWith('chart:')?'Чарт: '+c.slice(6):c==='similar-graph'?'Ссылки со страниц':cats[c]||c;
const date=v=>v?v.slice(0,10):'—';
const badge=r=>`<span class="badge ${esc(r.verdict)}">${esc(D.labels[r.verdict])}</span>`;
const price=r=>r.price===null||r.price===undefined?'Цена неизвестна':r.price>0?`${r.price} ${r.currency||''}`:'Бесплатная загрузка';
const link=(id,g)=>`https://apps.apple.com/${encodeURIComponent(g)}/app/id${encodeURIComponent(id)}`;
$('updated').textContent='Отчёт собран '+D.built;
function sourceRows(){return D.candidates.filter(r=>($('source').value==='all'||(r.sources||[]).includes($('source').value))&&($('lane').value==='all'||r.lane===$('lane').value)&&(!$('titleMatchOnly').checked||['charts','links'].includes($('source').value)||($('source').value==='all'&&(r.sources||[]).some(k=>k!=='search'))||r.title_match))}
function inSource(category){const kind=category.startsWith('chart:')?'charts':category==='similar-graph'?'links':'search';return $('source').value==='all'||kind===$('source').value}
function rowCategory(r){return $('category').value==='all'?(($('titleMatchOnly').checked?(r.title_match_categories||[]).find(inSource):null)||(r.categories||[]).find(inSource)||r.category):$('category').value}
function summary(){
  const rows=sourceRows(),counts={};for(const r of rows)counts[r.verdict]=(counts[r.verdict]||0)+1;
  $('alive').textContent=nf(counts.ALIVE||0);$('uncertain').textContent=nf((counts.WEAK||0)+(counts.UNKNOWN||0));
  $('coverage').textContent=(D.source_coverage[$('source').value]||[]).length+' / 24';
  $('velocity').textContent=nf(rows.filter(r=>r.velocity!==null).length);$('total').textContent=nf(rows.length)+' записей в источнике';
  const sample=D.research_coverage[$('source').value];
  $('notice').textContent=D.first?`История базы: ${date(D.first)} — ${date(D.last)}. В выбранном источнике без свежих сигналов: ${nf(counts.NO_RECENT_EVIDENCE||0)}. Это не доказывает отсутствие спроса. Охват RSS в источнике до фильтра названий: для версий <18 мес пригодный RSS есть у ${nf(sample.rss_usable)} из ${nf(sample.eligible)} записей; со свежими 1–2★ — ${nf(sample.complaint_records)}. Остальные ещё требуют проверки. Это охват проверок, не доля проблем на рынке. Полнота сборов — в разделе «Что проверено».`:'Наблюдений пока нет.';
}
function options(id,values,label){for(const value of values){const o=document.createElement('option');o.value=value;o.textContent=label(value);$(id).append(o)}}
options('geo',D.coverage.map(r=>r.country).sort(),v=>v.toUpperCase());
function categoryOptions(){const selected=$('category').value;$('category').innerHTML='<option value="all">Все категории</option>';options('category',[...new Set(sourceRows().flatMap(r=>r.categories||[r.category]).filter(inSource))].sort(),cat);if([...$('category').options].some(o=>o.value===selected))$('category').value=selected}
categoryOptions();
let page=0,filtered=[];const pageSize=15;
function detail(r){
  if(!r){$('detail').innerHTML='<h3>Выберите приложение</h3><p class="tiny">Для этих фильтров кандидатов нет.</p>';return}
  const v=r.velocity===null?'—':(r.velocity>0?'+':'')+r.velocity.toFixed(1);
  const category=rowCategory(r);
  const query=((r.source_queries||{})[category]||[(r.category_queries||{})[category]||r.query]).join('», «');
  const geos=(r.observed_countries||[]).map(g=>`<a href="${link(r.track_id,g)}" target="_blank" rel="noopener">${esc(g.toUpperCase())}</a>`).join(' · ');
  const source=(r.categories||[]).length?`${cat(category)} · запрос «${query}». Релевантность нужно проверить; совпадение слов в названии — только подсказка.`:'Историческое наблюдение. В последней завершённой выдаче не найдено; это не доказательство удаления.';
  $('detail').innerHTML=`<div class="eyebrow">Основание гипотезы · ${esc(r.country.toUpperCase())}</div><div class="detail-name">${esc(r.name)}</div>${badge(r)}<p class="tiny">${esc(D.lanes[r.lane])}. ${r.lane==='recent_pain'?'На текущем выпуске ≥3 отзыва 1–2★ выше базы категории; из поиска — только со словами запроса в названии. Содержание боли ещё нужно проверить.':'Возраст — повод проверить продукт, а не доказательство его поломки.'}</p>
  <div class="facts"><div><strong>${nf(r.rating_count)}</strong><small>накопленных оценок</small></div><div><strong>${v}</strong><small>оценок / 30 дней</small></div></div>
  ${r.reasons.map(s=>`<div class="reason">${esc(s)}</div>`).join('')}
  <div class="reason"><b>${esc(price(r))}</b><br>Цена загрузки не доказывает текущие продажи. Покупки внутри и внешняя воронка — отдельная проверка.</div>
  ${geos?`<div class="reason">Страны с выбранным сигналом: ${geos}. Метрики карточки относятся к ${esc(r.country.toUpperCase())}.</div>`:''}
  <div class="reason">${esc(source)}</div><div class="reason"><b>Платежи и выручка: неизвестны</b><br>Возраст версии не доказывает поломку, уход автора или выгодность входа.</div>
  <div class="next"><b>Следующий шаг</b><br>${r.verdict==='ALIVE'?'Проверить свежие жалобы и продукт руками. Выбрать покупателя, преимущество и канал. Затем проверить оплату.':'Обновить данные и проверить альтернативы. Отсутствие сигнала у карточки не закрывает гипотезу о боли.'}</div>
  <a class="primary" href="${link(r.track_id,r.country)}" target="_blank" rel="noopener">Открыть в App Store ↗</a><p class="tiny" style="margin-top:12px">Наблюдение ${esc(date(r.scanned_at))} · отзывы ${esc(date(r.checked_at))}</p>`;
}
function render(){
  summary();
  const q=$('search').value.toLowerCase(),category=$('category').value;
  const matches=sourceRows().filter(r=>($('status').value==='all'||r.verdict===$('status').value)&&($('geo').value==='all'||r.country===$('geo').value)&&(category==='all'||((r.categories||[r.category]).includes(category)&&(!$('titleMatchOnly').checked||category.startsWith('chart:')||category==='similar-graph'||(r.title_match_categories||[]).includes(category))))&&(r.name+' '+r.seller).toLowerCase().includes(q)&&(!$('complaintsOnly').checked||(r.bad_recent_count||0)>0)&&(!$('paidOnly').checked||(r.price||0)>0));
  if($('groupApps').checked){const groups=new Map();for(const r of matches){if(!groups.has(r.track_id))groups.set(r.track_id,{...r,observed_countries:[]});groups.get(r.track_id).observed_countries.push(r.country)}filtered=[...groups.values()]}else filtered=matches;
  const visible=filtered.slice(page*pageSize,(page+1)*pageSize);
  $('rows').innerHTML=visible.map((r,i)=>`<tr><td><button class="rowbtn" data-index="${i}">${esc(r.name)}</button><div class="tiny">${esc(D.lanes[r.lane])}<br>${esc(cat(rowCategory(r)))} · ${esc(price(r))}</div></td><td class="country">${esc(r.country.toUpperCase())}${r.observed_countries&&r.observed_countries.length>1?' +'+(r.observed_countries.length-1):''}</td><td>${r.best_rank??'—'}<div class="tiny">${esc(r.best_query||'')}</div></td><td>${r.r90??'—'}<div class="tiny">1–2★: ${r.bad_recent_count??'—'}<br>После обновления: ${r.bad_after_version_count??'?'}</div></td><td>${badge(r)}</td><td>${esc(date(r.version_date))}</td></tr>`).join('')||'<tr><td colspan="6" class="empty">Для этих фильтров кандидатов нет.</td></tr>';
  $('shown').textContent=filtered.length?`${page*pageSize+1}–${Math.min((page+1)*pageSize,filtered.length)} из ${nf(filtered.length)} ${$('groupApps').checked?'приложений':'записей'} · R90 — выборка отзывов за 90 дней`:'0 записей';
  $('prev').disabled=page===0;$('next').disabled=(page+1)*pageSize>=filtered.length;
  for(const b of $('rows').querySelectorAll('button'))b.onclick=()=>detail(visible[Number(b.dataset.index)]);
  detail(visible[0]);
}
for(const id of ['search','status','geo','category','groupApps','complaintsOnly','paidOnly'])$(id).addEventListener('input',()=>{page=0;render()});
for(const id of ['source','lane','titleMatchOnly'])$(id).addEventListener('input',()=>{page=0;categoryOptions();render()});
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
$('directions').textContent=`В конфиге включено ${D.directions.enabled} из ${D.directions.configured} поисковых направлений. Это наш набор запросов, а не все категории App Store. Фактический охват каждого запуска указан ниже.`;
$('runCoverage').innerHTML=(D.runs||[]).map(r=>`<div class="proof"><b>${esc(({'itunes-search':'Поиск App Store','google-play':'Google Play','apple-charts':'Чарты App Store','apple-page-links':'Ссылки App Store'})[r.source]||r.source)} · ${r.status==='COMPLETE'?'Завершён':'Не завершён'}</b><strong>${nf(r.succeeded||0)} / ${nf(r.expected_queries)}</strong><span class="tiny">${esc(r.scope||'')}<br>Пустых ответов: ${nf(r.empty||0)} · ошибок: ${nf(r.errors||0)} · начало ${esc(r.started_at)}</span></div>`).join('')||'<p class="tiny">Исторические сканы не сохраняли журнал запросов. Полнота не доказана.</p>';
const Q=D.quality;if(Q){const c=Q.review_coverage,q=Q.queries,p=Q.recent_pain_timing;$('dataQuality').innerHTML=`<div class="proof"><b>Покрытие и ограничения поиска</b><p class="tiny">Пригодные запросы за ${Q.freshness_days} дней: ${nf(q.fresh_ok)} / ${nf(q.expected)}. Выдача, вероятно, усечена (≥35 из 50): ${nf(q.likely_truncated)} / ${nf(q.limit_known)}; рынок шире. Запросов на чужом языке: ${nf(q.fallback_language)}.<br>Пригодный RSS: ${nf(c.rss_usable)} / ${nf(c.eligible)} записей с ≥500 оценок; у ${nf(c.rss_recent_capped)} все полученные отзывы попали в 90 дней, поэтому число отзывов усечено.<br>Воронка старых версий: поиск ${nf(Q.queue_funnel.search.records)} → активность ${nf(Q.queue_funnel.alive.records)} → топ-10 ${nf(Q.queue_funnel.top10.records)} → запрос в названии ${nf(Q.queue_funnel.title_match.records)}.<br>У обновляемых версий с жалобами: после даты выпуска есть 1–2★ у ${nf(p.after_release)} / ${nf(p.total)}; только до неё — у ${nf(p.only_before_release)}. Это не доказывает исправление или сохранение бага.<br>RSS с известной версией отзыва: ${nf(c.rss_with_review_versions)}. Остальные записи требуют нового сбора, старым версия не приписывается.<br>Источник: поисковые наблюдения до фильтра названий. Это качество данных, не рейтинг заработка.</p></div>`;}
if(Q&&Q.collection){const k=Q.collection;$('dataQuality').insertAdjacentHTML('beforeend',`<div class="proof"><b>Сбор шире очереди кандидатов</b><p class="tiny">В текущей поисковой выдаче сохранено ${nf(k.search_records)} записей приложение × страна, из них ${nf(k.below_500_ratings)} с менее чем 500 оценками. Порог очереди их не удаляет.<br>Сохранённых проверок RSS: ${nf(k.history.samples)}. Они накапливаются; прежние перезаписанные ответы восстановить нельзя.<br>Полные ответы с текстами сохраняются при локальном сборе с архивом. Облачный сбор хранит разобранную историю. Широкая разведка не измеряет долю прибыльных ниш.</p></div>`);}
const H=D.hypotheses;
$('hypotheses').innerHTML=H&&H.study_id?`<div class="proof"><b>Активность старых и свежих версий</b><strong>${H.matched_old_minus_fresh_pp===null?'Недостаточно данных':H.matched_old_minus_fresh_pp.toFixed(1)+' п.п.'}</strong><p class="tiny">Доля старых минус доля свежих после сопоставления по стране и группам количества оценок и возраста приложения. Сопоставимых групп: ${H.matched_strata}. Исходы на ${esc(date(H.analysis_asof))}. Это связь в выборке, не причинный эффект.</p></div><table><thead><tr><th>Версия</th><th>≥2 отзыва / 90д</th><th>Нет данных</th></tr></thead><tbody>${[['≥18 мес','old'],['<12 мес','fresh']].map(([label,key])=>{const s=H.activity_comparison[key];return `<tr><td>${label}</td><td>${s.active} / ${s.n} · ${s.rate===null?'—':(100*s.rate).toFixed(1)+'%'}</td><td>${s.unknown}</td></tr>`}).join('')}</tbody></table><p class="tiny">Таблица показывает исходные доли. Значение выше учитывает сопоставление. Единица — приложение × страна; страны одного приложения зависимы.</p><div class="proof"><b>Старая версия → прибыль нового конкурента</b><strong>Не проверено</strong><span class="tiny">Нет собственных исходов запуска и сопоставимых затрат.</span></div><p class="tiny">Порог активности — исследовательское допущение. Выборка RSS ограничена; отсутствующие ответы исключены из сравнения.</p>`:'<p class="tiny">Расчёт появится после проверки данных. Отсутствие результата не подтверждает гипотезу.</p>';
$('footer').textContent=`Метод ${D.method} · iTunes Search / RSS · поиск гипотез. Выручка конкурентов из этих данных не измеряется.`;
render();calc();
