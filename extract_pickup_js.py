from pathlib import Path

s = Path("web_ui.py").read_text(encoding="utf-8")
a = s.index("<script>const token=__TOKEN__;", s.index("def pickup_page")) + len("<script>")
b = s.index("</script>", a)
Path("pickup_page_check.js").write_text(s[a:b].replace("__TOKEN__", '"token"'), encoding="utf-8")
