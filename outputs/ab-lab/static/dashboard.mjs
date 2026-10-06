import {node,svgNode,countryBadge,deviceBadge,reasonLabels} from './presentation.mjs';
const $ = value => document.querySelector(value);
const number = value => Number(value || 0).toLocaleString('zh-CN');
const periodNames = {today:'今日',yesterday:'昨日','7d':'近 7 天','30d':'近 30 天'};
export function visibleTrend(data) {
  if (data.period !== 'today') return data.trend;
  const local = new Date(Date.parse(data.end) + data.tz_offset * 60000);
  return data.trend.slice(0, local.getUTCHours() + 1);
}
function empty(root, text='暂无访问数据') { root.replaceChildren(node('p','chart-empty',text)); }
function title(target, text) { const t = svgNode('title'); t.textContent=text; target.append(t); }
function text(svg, x, y, value, attrs={}) { const n=svgNode('text',{x,y,...attrs}); n.textContent=value; svg.append(n); }

function trendChart(root, buckets) {
  if (!buckets.some(row=>row.total)) return empty(root,'这个时间段还没有访问，真实流量到达后会显示趋势。');
  const svg=svgNode('svg',{viewBox:'0 0 700 225',role:'img','aria-label':'访问趋势，按时间展示总访问、放行与拦截'});
  const defs=svgNode('defs'),gradient=svgNode('linearGradient',{id:'traffic-trend-fill',x1:0,y1:0,x2:0,y2:1});
  gradient.append(svgNode('stop',{offset:'0%',class:'trend-gradient-top'}),svgNode('stop',{offset:'100%',class:'trend-gradient-bottom'}));defs.append(gradient);svg.append(defs);
  const max=Math.max(4,...buckets.map(row=>row.total));
  for(let i=0;i<=4;i++) { const y=20+i*43; svg.append(svgNode('line',{x1:40,x2:680,y1:y,y2:y,class:'chart-gridline'})); text(svg,30,y+4,number(Math.round(max*(1-i/4))),{'text-anchor':'end',class:'chart-axis'}); }
  const x=i=>40+i*640/Math.max(1,buckets.length-1), y=n=>192-n/max*172;
  const points=buckets.map((row,i)=>`${x(i)},${y(row.total)}`).join(' ');
  svg.append(svgNode('polygon',{points:`40,192 ${points} ${x(buckets.length-1)},192`,class:'trend-area'}));
  for (const [key,cls] of [['total','total'],['allowed','allow'],['blocked','block']]) svg.append(svgNode('polyline',{points:buckets.map((row,i)=>`${x(i)},${y(row[key])}`).join(' '),fill:'none',class:`trend-line ${cls}`}));
  buckets.forEach((row,i)=> { const description=`${row.label}：总访问 ${row.total}，放行 ${row.allowed}，拦截 ${row.blocked}，其他 ${row.other}`,point=svgNode('circle',{cx:x(i),cy:y(row.total),r:4,class:`trend-point${i===buckets.length-1?' latest':''}`,tabindex:0,'aria-label':description}); title(point,description); svg.append(point); if(i===0||i===buckets.length-1||i%Math.ceil(buckets.length/6)===0) text(svg,x(i),218,row.label.length>5?row.label.slice(5):row.label,{'text-anchor':'middle',class:'chart-axis'}); });
  root.replaceChildren(svg);
}
function domainChart(root, rows) {
  if(!rows.some(row=>row.total)) return empty(root,'暂无域名访问记录');
  const sorted=[...rows].map(row=>({...row,domain:row.domain||'本地站点'})).sort((a,b)=>b.total-a.total).slice(0,8);
  const max=Math.max(1,...sorted.map(row=>row.total)),list=node('ol','domain-ranking');
  sorted.forEach((row,i)=> {
    const item=node('li','domain-rank'),name=node('span','domain-rank-name',row.domain),bar=svgNode('svg',{viewBox:'0 0 300 7',preserveAspectRatio:'none',role:'img','aria-label':`放行 ${row.allowed}，拦截 ${row.blocked}，其他 ${row.other}`});
    name.title=row.domain;bar.append(svgNode('rect',{width:300,height:7,rx:3.5,class:'domain-track'}));
    let start=0;
    for(const key of ['allowed','blocked','other']) { const width=row[key]/max*300,rect=svgNode('rect',{x:start,y:0,width,height:7,class:`domain-bar ${key}`});title(rect,`${row.domain}：放行 ${row.allowed} / 拦截 ${row.blocked} / 其他 ${row.other}`);bar.append(rect);start+=width; }
    item.append(node('span','domain-rank-position',String(i+1).padStart(2,'0')),name,bar,node('strong','domain-rank-total',number(row.total)));list.append(item);
  });
  root.replaceChildren(list);
}
function countryChart(root, rows) {
  if(!rows.length) return empty(root,'尚未记录访客国家');
  const sorted=[...rows].sort((a,b)=>b.total-a.total),shown=sorted.slice(0,5),rest=sorted.slice(5).reduce((sum,row)=>sum+row.total,0);
  if(rest) shown.push({code:'OTHER',total:rest});
  const total=rows.reduce((sum,row)=>sum+row.total,0), wrap=node('div','country-visual'), svg=svgNode('svg',{viewBox:'0 0 180 180',class:'donut',role:'img','aria-label':`访问来自 ${rows.filter(row=>row.code).length} 个已识别国家或地区`});
  svg.append(svgNode('circle',{cx:90,cy:90,r:66,fill:'none','stroke-width':18,class:'donut-track'}));
  let offset=0;
  shown.forEach((row,i)=> { const arc=row.total/total*414.69,gap=shown.length>1?Math.min(3,arc*.15):0; svg.append(svgNode('circle',{cx:90,cy:90,r:66,fill:'none','stroke-width':18,'stroke-dasharray':`${arc-gap} ${414.69-arc+gap}`,'stroke-dashoffset':-offset,transform:'rotate(-90 90 90)',class:`country-slice palette-${i}`}));offset+=arc; });
  text(svg,90,87,number(total),{'text-anchor':'middle',class:'donut-total'}); text(svg,90,109,'次访问',{'text-anchor':'middle',class:'chart-axis'});
  const list=node('div','country-ranking');
  shown.forEach((row,i)=>{const line=node('div',`country-rank palette-${i}`),swatch=node('span','country-swatch'),stats=node('span','country-rank-stats');swatch.setAttribute('aria-hidden','true');stats.append(node('strong','',`${(row.total/total*100).toFixed(1)}%`),node('small','',`${number(row.total)} 次`));line.append(swatch,row.code==='OTHER'?node('span','','其他地区'):countryBadge(row.code),stats);list.append(line);});
  wrap.append(svg,list);root.replaceChildren(wrap);
}
function reasonsChart(root,rows) {
  if(!rows.length) return empty(root,'这个时间段没有拦截记录');
  const wrap=node('div','reason-ranking'),max=Math.max(...rows.map(row=>row.count));
  rows.slice(0,6).forEach(row=> { const item=node('div','reason-item'), label=node('div','reason-label');label.append(node('span','',reasonLabels[row.reason]||row.reason),node('strong','',number(row.count)));const bar=svgNode('svg',{viewBox:'0 0 300 5',preserveAspectRatio:'none','aria-hidden':'true'});bar.append(svgNode('rect',{width:300,height:5,rx:2.5,class:'reason-track'}),svgNode('rect',{width:row.count/max*300,height:5,rx:2.5,class:'reason-fill'}));item.append(label,bar);wrap.append(item);});root.replaceChildren(wrap);
}
function recentVisits(root,rows) {
  root.replaceChildren();
  if(!rows.length) {const row=node('tr'),td=node('td','chart-empty','还没有访问记录。真实访问发生后，会显示设备和对应国家。');td.colSpan=5;row.append(td);root.append(row);return;}
  rows.forEach(item=> {const row=node('tr');row.append(node('td','visit-time',new Date(item.created*1000).toLocaleString('zh-CN',{month:'2-digit',day:'2-digit',hour:'2-digit',minute:'2-digit',second:'2-digit',hour12:false})));const device=node('td'),result=node('td');device.append(deviceBadge(item));const outcome={allowed:'放行',blocked:'拦截',other:'手动／其他'}[item.outcome]||'其他';result.append(node('span',`outcome ${item.outcome}`,`${outcome} · ${item.slot}`));const country=node('td','country-name'); country.append(countryBadge(item.country)); row.append(node('td','visit-domain',item.domain || '本地站点'),device,country,result);root.append(row);});
}
export function createDashboard(api, aggregateLabel = () => '所有域名') {
  let generation=0;
  function clear(message='正在加载流量数据…') {
    ['total','allowed','blocked','rate'].forEach(key=>$(`#metric-${key}`).textContent='—');
    for(const id of ['traffic-trend','traffic-domains','traffic-countries','traffic-reasons']) empty($(`#${id}`),message);
    $('#recent-visits').replaceChildren();$('#analytics-extra').textContent='';$('#analytics-status').textContent=message;
  }
  async function refresh() {
    const request=++generation,period=$('#analytics-period').value,scope=$('#analytics-scope').value;
    document.querySelectorAll('[data-period-label]').forEach(n=>n.textContent=periodNames[period]);
    clear();$('#analytics-content').setAttribute('aria-busy','true');$('#analytics-status').classList.remove('is-error');
    try {
      const data=await api(`/api/analytics?${new URLSearchParams({period,scope,tz_offset:String(-new Date().getTimezoneOffset())})}`);
      if(request!==generation)return;
      ['total','allowed','blocked'].forEach(key=>$(`#metric-${key}`).textContent=number(data.summary[key]));$('#metric-rate').textContent=`${Number(data.summary.rate).toFixed(1)}%`;
      document.querySelectorAll('[data-period-label]').forEach(n=>n.textContent=periodNames[period]);
      $('#analytics-extra').textContent=`展示 A ${number(data.summary.a)} · 展示 B ${number(data.summary.b)} · 手动／其他 ${number(data.summary.other)}`;
      const offset=-new Date().getTimezoneOffset();const sign=offset<0?'−':'+';const tz=`UTC${sign}${String(Math.floor(Math.abs(offset)/60)).padStart(2,'0')}:${String(Math.abs(offset)%60).padStart(2,'0')}`;
      $('#analytics-status').textContent=`${scope==='all'?aggregateLabel():'当前域名'} · ${periodNames[period]} · ${tz} · 刚刚更新`;
      $('#analytics-retention').textContent='按文档请求统计，非独立访客数。每站保留最近 30 天、最多 10,000 条记录；历史高流量可能被截断。国家按 IP 归属识别，设备根据 UA 判断。';
      trendChart($('#traffic-trend'),visibleTrend(data));domainChart($('#traffic-domains'),data.domains);countryChart($('#traffic-countries'),data.countries);reasonsChart($('#traffic-reasons'),data.reasons);recentVisits($('#recent-visits'),data.recent);
    } catch(error) {if(request===generation) {clear('统计加载失败，请点击“刷新数据”重试。');$('#analytics-status').classList.add('is-error');}}
    finally {if(request===generation)$('#analytics-content').setAttribute('aria-busy','false');}
  }
  $('#analytics-period').addEventListener('change',refresh);$('#analytics-scope').addEventListener('change',refresh);$('#refresh-analytics').addEventListener('click',refresh);
  return {refresh,invalidate(){++generation;clear('正在切换站点…');$('#analytics-content').setAttribute('aria-busy','false');}};
}
