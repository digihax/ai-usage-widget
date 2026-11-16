"""Build a ZIP using only reviewed public files, never a recursive checkout copy."""
from pathlib import Path
import zipfile
ROOT=Path(__file__).resolve().parent
FILES=('README.md','config.example.json','collect.py','Import-ClaudeQuota.py','UsageOverlay.ps1','Start-Widget.ps1','test_collect.py','package.py','.gitignore','LICENSE')
if __name__=='__main__':
    target=ROOT/'dist/ai-usage-widget-preview.zip'
    target.parent.mkdir(exist_ok=True)
    with zipfile.ZipFile(target,'w',compression=zipfile.ZIP_DEFLATED) as bundle:
        for name in FILES:bundle.write(ROOT/name,'ai-usage-widget/'+name)
    print(target)
