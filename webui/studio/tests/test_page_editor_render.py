"""隔离工作区实测原 HTML 保存 → 固定 Skill 渲染 → 新 PNG；不调用模型。"""
import json
from pathlib import Path
import subprocess

from app import jobs


def test_real_save_rerenders_only_edited_page_and_keeps_source(tmp_path):
    root=Path(__file__).resolve().parents[3]
    python,environment=jobs._offline_runtime(tmp_path)
    environment["TMPDIR"]=str(tmp_path.parent)
    environment["PYTHONPATH"]=str(root/"webui/studio")
    environment["SKILL_COMMAND_TIMEOUT_S"]="90"
    script=r'''
import hashlib,json,os,sys,time,subprocess
from pathlib import Path
from unittest.mock import patch
from PIL import Image
sys.path.insert(0,sys.argv[2])
from app.page_editor import SourceDocument,save_and_render,version,_write
from runtime.production import ProductionSettings,ResourceSlots,SkillRenderer,SkillSnapshot
from state.workspace import WorkspaceState,file_sha256
from state.artifacts import ArtifactStore

root=Path(sys.argv[1])
workspace=WorkspaceState(root)
os.environ['TMPDIR']=str(root/'_runtime/tmp')
settings=ProductionSettings.from_environment()
snapshot=SkillSnapshot.stage(root,settings.skill_source,os.environ)
(root/'deck.md').write_text('# Editor test\n## Brief\n- language: en\n- page_count: 2\n')
(root/'briefs').mkdir(exist_ok=True)
_write(root/'base.css',(':root{--canvas-w:800px;--canvas-h:450px;}body{margin:0}'
    '.deck{position:absolute;width:800px;height:450px}.slide{position:absolute;box-sizing:border-box;'
    'width:800px;height:450px;visibility:hidden;background:#eee;color:#113355}'
    '.slide.active{visibility:visible}h1{position:absolute;left:80px;top:130px;margin:0;font:64px sans-serif}').encode())
for number in (1,2):
    (root/f'briefs/slide_{number:02d}.md').write_text(f'# P{number:02d} — Page {number}\n- job: explain\n- must_show: Page {number}\n')
    (root/f'slides/slide_{number:02d}.html').write_text(f'<section class="slide" data-slide="{number:02d}">'
        f'<h1 id="title">Original {number}</h1><script>window.preservedChartData=[1,3,2];</script></section>')
workspace.register_pages([1,2])
store=ArtifactStore(root,workspace_state=workspace)
for number in (1,2):
    identity=f'page-html-{number:02d}'
    store.declare(artifact_id=identity,kind='page_html',path=f'slides/slide_{number:02d}.html',owner_id='original-group-owner',page=number)
    store.commit(owner_id='original-group-owner',artifact_ids=[identity],source_hashes={})
renderer=SkillRenderer(root,settings,os.environ,ResourceSlots(root,settings),snapshot)
try:
    store.render_pages([1,2],renderer)
finally:
    renderer.close()
original=(root/'slides/slide_01.html').read_text()
css=(root/'base.css').read_text()
old_png=file_sha256(root/'renders/slide_01.png')
second={name:file_sha256(root/name) for name in ('slides/slide_02.html','renders/slide_02.png')}
index=next(n['index'] for n in SourceDocument(original).nodes if dict(n['attrs']).get('id')=='title')
started=time.monotonic()
request={'workspace':str(root),'page':1,'user_id':7,'version':version(original,css),
    'changes':[{'node':index,'text':'Edited page','styles':{'color':'#993300'}}]}
import app.page_editor as editor_module
completed=subprocess.run([sys.executable,editor_module.__file__,'--save-page'],
    input=json.dumps(request),text=True,capture_output=True,env=dict(os.environ),timeout=150)
assert completed.returncode==0,completed.stderr[-2000:]
result=json.loads(completed.stdout)
assert result.get('saved') and result.get('rendered'),result
assert file_sha256(root/'renders/slide_01.png')!=old_png
assert Image.open(root/'renders/slide_01.png').size==(800,450)
assert 'Edited page' in (root/'slides/slide_01.html').read_text()
assert '<script>window.preservedChartData=[1,3,2];</script>' in (root/'slides/slide_01.html').read_text()
assert 'data-mural-source' not in (root/'slides/slide_01.html').read_text()
assert (root/'base.css').read_text()==css
assert all(file_sha256(root/name)==digest for name,digest in second.items())
assert store.artifact('page-html-01').owner_id=='original-group-owner'
assert workspace.page(1).lifecycle.value=='rendered'
store.validate_render_receipt(1)
backups=list((root/'_trace/revisions').glob('manual-*/slide_01.html'))
assert len(backups)==1 and backups[0].read_text()==original
updated=(root/'slides/slide_01.html').read_text()
latest_png=file_sha256(root/'renders/slide_01.png')
index=next(n['index'] for n in SourceDocument(updated).nodes if dict(n['attrs']).get('id')=='title')
with patch.object(SkillRenderer,'__call__',side_effect=RuntimeError('controlled renderer failure')):
    failed=save_and_render({'workspace':str(root),'page':1,'user_id':7,'version':version(updated,css),
        'changes':[{'node':index,'text':'Saved without screenshot'}]})
assert failed['saved'] and not failed['rendered'],failed
assert 'Saved without screenshot' in (root/'slides/slide_01.html').read_text()
assert file_sha256(root/'renders/slide_01.png')==latest_png
assert workspace.page(1).lifecycle.value=='authored'
retry=save_and_render({'workspace':str(root),'page':1,'user_id':7,
    'version':version((root/'slides/slide_01.html').read_text(),css),'changes':[]})
assert retry['saved'] and retry['rendered'],retry
assert file_sha256(root/'renders/slide_01.png')!=latest_png
assert workspace.page(1).lifecycle.value=='rendered'
assert len(list((root/'_trace/revisions').glob('manual-*/slide_01.html')))==2
assert all(file_sha256(root/name)==digest for name,digest in second.items())
store.validate_render_receipt(1)
print(json.dumps({'workspace':str(root),'success':result,'controlled_failure':failed,
    'retry_without_source_changes':retry,
    'save_and_failure_check_seconds':round(time.monotonic()-started,3)},ensure_ascii=False))
'''
    result=subprocess.run([python,"-c",script,str(tmp_path),str(root/"harnesses/mural-presenter-v1")],
                          cwd=root,env=environment,capture_output=True,text=True,timeout=210)
    assert result.returncode==0,result.stdout[-2000:]+result.stderr[-5000:]
    outcome=json.loads(result.stdout.strip().splitlines()[-1])
    assert outcome["success"]["rendered"]
    print(json.dumps(outcome,ensure_ascii=False))
