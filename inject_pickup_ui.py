from pathlib import Path


path = Path("web_ui.py")
text = path.read_text(encoding="utf-8")

button_anchor = '<button class="btn btn-outline btn-sm" onclick="exportCSV()">CSV</button>'
button = button_anchor + '<button class="btn btn-outline btn-sm" onclick="exportPickupTxt()">导出取件链接 TXT</button>'
if "exportPickupTxt()" not in text:
    if button_anchor not in text:
        raise SystemExit("email export button anchor not found")
    text = text.replace(button_anchor, button, 1)

script_anchor = "function clearLogs(){"
script = r'''async function exportPickupTxt(){var d=await apiSlow('/api/pickup-links');if(d.error){toast('生成失败: '+d.error,true);return}var lines=(d.links||[]).map(function(x){return x.email+'----'+x.url});if(!lines.length){toast('没有可导出的隐私邮箱',true);return}var b=new Blob(['\uFEFF'+lines.join('\n')],{type:'text/plain;charset=utf-8'}),a=document.createElement('a');a.href=URL.createObjectURL(b);a.download='icloud_pickup_links.txt';a.click();setTimeout(function(){URL.revokeObjectURL(a.href)},1000);toast('已导出 '+lines.length+' 条取件链接');}'''
if script not in text:
    if script_anchor not in text:
        raise SystemExit("javascript anchor not found")
    text = text.replace(script_anchor, script + script_anchor, 1)

path.write_text(text, encoding="utf-8")
