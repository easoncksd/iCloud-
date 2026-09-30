from pathlib import Path


path = Path("web_ui.py")
text = path.read_text(encoding="utf-8")

if "exportSelectedPickupTxt" in text and "data-export-filter" in text:
    raise SystemExit("selection and export-state UI already installed")

old_start = text.index("var pickupLinksByEmail={}")
old_end = text.index("function renderBatchPanel(){", old_start)
new = r'''var pickupLinksByEmail={};var pickupLinksLoaded=false;var pickupSelected={};var exportFilter='unexported';
async function loadPickupLinks(){var d=await apiSlow('/api/pickup-links');if(d.error){toast('取件链接生成失败: '+d.error,true);return}pickupLinksByEmail={};(d.links||[]).forEach(function(x){pickupLinksByEmail[String(x.email||'').toLowerCase()]=x.url});pickupLinksLoaded=true;}
function setExportFilter(value){exportFilter=value;document.querySelectorAll('[data-export-filter]').forEach(function(btn){btn.classList.toggle('active',btn.dataset.exportFilter===value)});renderAliasTable();}
function togglePickupSelected(email,checked){var key=String(email||'').toLowerCase();if(checked)pickupSelected[key]=true;else delete pickupSelected[key];}
function toggleAllPickup(){var checks=document.querySelectorAll('#aliasTableContainer input.pickup-check:not(:disabled)');var shouldCheck=Array.from(checks).some(function(c){return !c.checked});checks.forEach(function(c){c.checked=shouldCheck;togglePickupSelected(c.dataset.email,shouldCheck);});}
function copyPickup(url){if(!url){toast('取件链接尚未生成',true);return}navigator.clipboard.writeText(url).then(function(){toast('取件链接已复制')});}
function visibleAliases(){var accountFilter=E('aliasFilter').value;return emails.filter(function(e){if(accountFilter!=='all'&&e.account_id!==accountFilter)return false;if(exportFilter==='exported')return !!e.exported;if(exportFilter==='unexported')return !e.exported;return true;});}
function formatExportTime(value){if(!value)return '--';try{return new Date(value).toLocaleString('zh-CN',{hour12:false})}catch(_){return value}}
async function exportSelectedPickupTxt(){var selected=emails.filter(function(e){return !e.exported&&pickupSelected[String(e.email||'').toLowerCase()]}).map(function(e){return e.email});if(!selected.length){toast('请先勾选未导出的邮箱',true);return}var d=await apiSlow('/api/pickup-links/export',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({emails:selected})});if(!d.ok){toast('导出失败: '+(d.error||'未知错误'),true);return}if(!(d.lines||[]).length){toast('所选邮箱均已导出，未重复生成文件',true);await refreshEmails();renderAliasTable();return}var b=new Blob(['\uFEFF'+d.lines.join('\n')],{type:'text/plain;charset=utf-8'}),a=document.createElement('a');a.href=URL.createObjectURL(b);a.download='icloud_pickup_links_'+new Date().toISOString().slice(0,10)+'.txt';a.click();setTimeout(function(){URL.revokeObjectURL(a.href)},1000);selected.forEach(function(email){delete pickupSelected[String(email).toLowerCase()]});await refreshEmails();renderAliasTable();toast('已导出 '+d.count+' 条，并归类到已导出邮箱');}
async function restoreExportedEmail(email){if(!confirm('确认将 '+email+' 恢复为未导出？'))return;var d=await api('/api/export-history/restore',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({emails:[email]})});if(!d.ok){toast('恢复失败: '+(d.error||'未知错误'),true);return}await refreshEmails();renderAliasTable();toast('已恢复为未导出');}
function renderAliasTable(){updateEmailFilter();var filtered=visibleAliases();var exportedCount=emails.filter(function(e){return e.exported}).length;var unexportedCount=emails.length-exportedCount;E('exportCountUnexported').textContent='未导出 '+unexportedCount;E('exportCountExported').textContent='已导出 '+exportedCount;E('exportCountAll').textContent='全部 '+emails.length;E('emailCount').textContent=filtered.length+' / '+emails.length;var c=E('aliasTableContainer');if(!filtered.length){c.innerHTML='<div class="empty"><div class="icon"></div>'+(exportFilter==='exported'?'暂无已导出邮箱':'暂无可导出邮箱')+'</div>';return;}if(!pickupLinksLoaded){c.innerHTML='<div class="empty">正在生成取件链接...</div>';loadPickupLinks().then(renderAliasTable);return;}var h='<table class="email-table"><thead><tr><th style="width:42px"><input type="checkbox" title="全选/取消全选" onclick="toggleAllPickup()"></th><th>#</th><th>邮箱地址</th><th>取件链接</th><th>所属账号</th><th>标签</th><th>导出状态</th><th>邮箱状态</th></tr></thead><tbody>';filtered.forEach(function(e,i){var key=String(e.email||'').toLowerCase();var url=pickupLinksByEmail[key]||'';var checked=pickupSelected[key]&&!e.exported?' checked':'';var disabled=e.exported?' disabled':'';var accName=e.account_name||e.account_email||e.account_id||'--';var activeHtml=e.hasOwnProperty('active')?(e.active?'<span style="color:var(--green)">活跃</span>':'<span style="color:var(--red)">停用</span>'):'<span style="color:var(--ink-faint)">--</span>';var exportHtml=e.exported?'<span style="color:var(--green)">已导出</span><div style="font-size:10px;color:var(--ink-faint);margin-top:3px">'+esc(formatExportTime(e.exported_at))+'</div><button class="copy-btn" onclick="restoreExportedEmail(\''+escAttr(e.email||'')+'\')" title="恢复后可再次导出">恢复</button>':'<span style="color:var(--ink-faint)">未导出</span>';h+='<tr><td><input class="pickup-check" type="checkbox" data-email="'+escAttr(e.email||'')+'"'+checked+disabled+' onchange="togglePickupSelected(this.dataset.email,this.checked)"></td><td style="color:var(--ink-faint);width:40px">'+(i+1)+'</td><td class="mono">'+esc(e.email||'')+'</td><td style="max-width:360px"><span style="font-size:11px;word-break:break-all">'+esc(url||'生成失败')+'</span> '+(url?'<button class="copy-btn" onclick="copyPickup(\''+escAttr(url)+'\')" title="复制取件链接">复制</button>':'')+'</td><td style="font-size:11px">'+esc(accName)+'</td><td style="font-size:11px;color:var(--ink-faint)">'+esc((e.label||'').substring(0,30))+'</td><td>'+exportHtml+'</td><td>'+activeHtml+'</td></tr>';});h+='</tbody></table>';c.innerHTML=h;}
'''
text = text[:old_start] + new + text[old_end:]

old_button = '<button class="btn btn-outline btn-sm" onclick="exportPickupTxt()">导出取件链接 TXT</button>'
new_button = '<button class="btn btn-primary btn-sm" onclick="exportSelectedPickupTxt()">导出已选 TXT</button>'
if old_button not in text:
    raise SystemExit("export button anchor not found")
text = text.replace(old_button, new_button, 1)

old_filter = '<div class="filter-bar"><span style="font-size:11px;color:var(--ink-faint)">筛选账号:</span><select id="aliasFilter" onchange="renderAliasTable()"><option value="all">全部账号</option></select></div>'
new_filter = '<div class="filter-bar"><span style="font-size:11px;color:var(--ink-faint)">筛选账号:</span><select id="aliasFilter" onchange="renderAliasTable()"><option value="all">全部账号</option></select><div class="segmented" aria-label="导出状态筛选"><button type="button" class="active" data-export-filter="unexported" onclick="setExportFilter(\'unexported\')" id="exportCountUnexported">未导出 0</button><button type="button" data-export-filter="exported" onclick="setExportFilter(\'exported\')" id="exportCountExported">已导出 0</button><button type="button" data-export-filter="all" onclick="setExportFilter(\'all\')" id="exportCountAll">全部 0</button></div></div>'
if old_filter not in text:
    raise SystemExit("filter anchor not found")
text = text.replace(old_filter, new_filter, 1)

css_anchor = '.copy-toast{'
css = '.segmented{display:inline-flex;border:1px solid var(--rule-strong);margin-left:auto}.segmented button{border:0;border-right:1px solid var(--rule-strong);background:transparent;color:var(--ink-soft);padding:6px 12px;font-family:var(--mono);font-size:11px;cursor:pointer}.segmented button:last-child{border-right:0}.segmented button.active{background:var(--ink);color:var(--paper)}'
if css not in text:
    text = text.replace(css_anchor, css + css_anchor, 1)

old_export_start = text.index('async function exportPickupTxt(){')
old_export_end = text.index('function clearLogs(){', old_export_start)
text = text[:old_export_start] + text[old_export_end:]

path.write_bytes(text.replace("\r\n", "\n").encode("utf-8"))
