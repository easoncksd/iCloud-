from pathlib import Path


path = Path("web_ui.py")
text = path.read_text(encoding="utf-8")

text = text.replace(
    ".hint{margin:0 0 12px;color:#6c8799}.status{float:right}",
    ".hint{margin:0 0 12px;color:#6c8799}.status{float:right}#refresh{margin-left:12px;border:1px solid #9eb1bd;background:#fff;color:#466579;padding:4px 12px;cursor:pointer}#refresh:disabled{opacity:.5;cursor:wait}",
    1,
)
text = text.replace(
    "<main class='wrap main'><p class='hint'>页面每 15 秒自动刷新。<span class='status' id='status'>正在读取...</span></p>",
    "<main class='wrap main'><p class='hint'>页面每 15 秒自动刷新。<button id='refresh' onclick='manualRefresh()'>立即刷新</button><span class='status' id='status'>正在读取...</span></p>",
    1,
)

start = text.index("<script>const token=__TOKEN__;", text.index("def pickup_page"))
end = text.index("</script>", start) + len("</script>")
script = r'''<script>const token=__TOKEN__;let busy=false;function esc(s){return String(s||'').replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;').replace(/"/g,'&quot;')}function render(items){const box=document.getElementById('mailbox');if(!items.length){box.innerHTML='<div class="empty">暂无邮件。</div>';return}box.innerHTML=items.slice().reverse().map(m=>{const body=m.text||m.body||m.preview||'';return '<article class="mail"><div class="subject">'+esc(m.subject||'(无主题)')+'</div><div class="meta">'+esc(m.from||'')+' · '+esc(m.date||'')+'</div>'+(body?'<div class="body">'+esc(body)+'</div>':'')+'</article>'}).join('')}async function fetchMessages(force){const q=force?'?force=1':'';const r=await fetch('/pickup/'+encodeURIComponent(token)+'/messages'+q,{cache:'no-store'});const d=await r.json();if(!r.ok)throw new Error(d.error||'读取失败');return d}async function load(force){if(busy)return;busy=true;const status=document.getElementById('status');const btn=document.getElementById('refresh');try{let d=await fetchMessages(!!force);render(d.emails||[]);status.textContent=d.refreshing?(force?'正在同步新邮件...':'后台查件中'):'刚刚刷新';if(force&&d.refreshing){for(let i=0;i<20;i++){await new Promise(r=>setTimeout(r,1000));d=await fetchMessages(false);render(d.emails||[]);if(!d.refreshing){status.textContent='新邮件已刷新';break}}}}catch(e){status.textContent='读取失败';if(!document.querySelector('.mail'))document.getElementById('mailbox').innerHTML='<div class="error">读取失败，稍后自动重试。</div>'}finally{busy=false;btn.disabled=false}}function manualRefresh(){document.getElementById('refresh').disabled=true;load(true)}load(false);setInterval(function(){load(false)},15000);</script>'''
text = text[:start] + script + text[end:]
path.write_bytes(text.replace("\r\n", "\n").encode("utf-8"))
