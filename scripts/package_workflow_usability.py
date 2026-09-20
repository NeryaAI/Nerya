"""Package only public UI acceptance artifacts, never the test workspace."""
from pathlib import Path
import hashlib
import html
import json
import zipfile

base = Path(__file__).resolve().parents[1]
root = base / "dashboard/test-results/workflow-usability-release"
review = json.loads((root / "browser-review.json").read_text())
assert not review["failures"] and not review["pageErrors"]
images = sorted((root / "screenshots").glob("*.png"))
assert len(images) == len(set(row["file"] for row in review["images"])) == 91
entries = []
for image in images:
    title = image.stem
    for a, b in [("macd_agent", "脚本触发Agent"), ("scheduled_agent", "定时Agent"), ("evolution", "复盘"), ("script", "脚本"), ("source", "数据源"), ("scheduler", "调度器"), ("essential", "常用设置"), ("advanced", "高级设置"), ("scroll", "滚动"), ("details", "详情"), ("running", "执行中"), ("completed", "完成"), ("failed", "失败")]:
        title = title.replace(a, b)
    rel = image.relative_to(root).as_posix()
    entries.append(f'<figure><figcaption>{html.escape(title)}</figcaption><a href="{rel}" target="_blank"><img src="{rel}" loading="lazy" alt="{html.escape(title)}"></a></figure>')
head = '<!doctype html><html lang="zh-CN"><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>Nerya 卡片与运行图册</title><style>body{margin:24px;background:#10111a;color:#eee;font:15px/1.8 system-ui}h1{font-size:30px}p{max-width:950px;color:#bdc0d0}main{display:grid;grid-template-columns:repeat(auto-fit,minmax(340px,1fr));gap:20px}figure{margin:0;border:1px solid #454550;border-radius:8px;overflow:hidden}figcaption{padding:12px;overflow-wrap:anywhere}img{width:100%;height:330px;object-fit:contain;object-position:top}a{color:#c8a8ff}</style><h1>Nerya · 卡片编辑与Agent运行过程</h1><p>91张真实页面截图。点图打开原尺寸；scroll编号表示同一面板连续滚动。使用浏览器页面查找可搜索卡片类型。</p><p>运行中的模型输出为明确标记的受控验收样例，用于验证真实派发器与UI状态传递，不是真实行情分析、盈利或定时任务启用证明。</p><p><a href="README.md">修改与测试说明</a></p><main>'
(root / "index.html").write_text(head + '\n'.join(entries) + '</main></html>', encoding='utf-8')
files = [(image, image.relative_to(root).as_posix()) for image in images]
files += [(root/name, name) for name in ['README.md','index.html','browser-review.json']]
files += [(base/'dashboard/test-results'/name, 'evidence/'+name) for name in ['workflow-usability-regression-release.xml','workflow-usability-frontend-contracts.json','workflow-usability-lifecycle-diagnostic.xml']]
manifest = [{'path':rel,'size':source.stat().st_size,'sha256':hashlib.sha256(source.read_bytes()).hexdigest()} for source,rel in files]
(root/'manifest.json').write_text(json.dumps(manifest,ensure_ascii=False,indent=2))
files.append((root/'manifest.json','manifest.json'))
archive = root/'nerya-workflow-usability.zip'
with zipfile.ZipFile(archive,'w',compression=zipfile.ZIP_DEFLATED) as bundle:
    for source,rel in files: bundle.write(source,rel)
with zipfile.ZipFile(archive) as bundle: assert bundle.testzip() is None
print(json.dumps({'path':str(archive.relative_to(base.parent)),'bytes':archive.stat().st_size,'sha256':hashlib.sha256(archive.read_bytes()).hexdigest(),'png_count':len(images),'file_count':len(files)}))
