"""Large image libraries must not be rewritten on ordinary preference edits."""
import base64
from dataclasses import asdict, replace
import io
import json
from pathlib import Path
import shutil

from PIL import Image
import pytest

from comic_editor.core import brush_storage, settings as settings_module
from comic_editor.core.brushes import BrushDefinition, BrushTexture, BrushTip
from comic_editor.core.settings import EditorSettings, load_settings, save_settings


def png(color='red'):
    stream=io.BytesIO()
    Image.new('RGBA',(7,11),color).save(stream,format='PNG')
    return base64.b64encode(stream.getvalue()).decode('ascii')


def library():
    image=png()
    tip=BrushTip(name='Original pixels',width=7,height=11,png=image,shape='image')
    return BrushDefinition(id='image-brush',name='Original brush',tips=(tip,tip),
                           texture=BrushTexture(png=image),dual=BrushDefinition(tips=(tip,)),
                           source={'original':{'kept':'source metadata'}})


def use_path(monkeypatch,path):
    monkeypatch.setattr(settings_module,'settings_path',lambda:path)


def test_images_are_deduplicated_and_portable_model_roundtrips(tmp_path,monkeypatch):
    path=tmp_path/'preferences.json'
    use_path(monkeypatch,path)
    brush=library()
    settings=EditorSettings(brush_presets=[brush.to_dict()],active_brush_id=brush.id)
    save_settings(settings)
    assert len(list((tmp_path/'brush-assets').glob('*.png')))==1
    stored=json.loads(path.read_text(encoding='utf8'))
    assert '$brush_png' in stored['brush_presets'][0]['tips'][0]['png']
    assert settings.brush_presets[0]['tips'][0]['png']==brush.tips[0].png
    loaded=load_settings()
    assert loaded.brush_presets==settings.brush_presets
    a=loaded.brush_presets[0]['tips'][0]['png']
    assert a is loaded.brush_presets[0]['tips'][1]['png']
    assert a is loaded.brush_presets[0]['dual']['tips'][0]['png']
    assert a is loaded.brush_presets[0]['texture']['png']
    assert BrushDefinition.from_dict(loaded.brush_presets[0])==brush


def test_ordinary_edits_neither_decode_nor_rewrite_existing_images(tmp_path,monkeypatch):
    path=tmp_path/'preferences.json';use_path(monkeypatch,path)
    settings=EditorSettings(brush_presets=[library().to_dict()])
    save_settings(settings)
    assets={p:p.stat().st_mtime_ns for p in (tmp_path/'brush-assets').glob('*.png')}
    monkeypatch.setattr(brush_storage.base64,'b64decode',lambda *a,**k:pytest.fail('Unchanged PNG decoded again'))
    for size,opacity in [(16,.3),(28,.6),(40,1)]:
        settings.brush_size_px=size;settings.brush_opacity=opacity
        save_settings(settings)
    assert {p:p.stat().st_mtime_ns for p in assets}==assets
    assert load_settings().brush_size_px==40


def test_first_legacy_migration_keeps_exact_inline_backup(tmp_path,monkeypatch):
    path=tmp_path/'preferences.json';use_path(monkeypatch,path)
    settings=EditorSettings(brush_presets=[library().to_dict()])
    original=json.dumps(asdict(settings),indent=2).encode()
    path.write_bytes(original)
    loaded=load_settings()
    loaded.brush_size_px=37
    save_settings(loaded)
    backup=path.with_name(path.name+'.inline-backup')
    assert backup.read_bytes()==original
    assert load_settings().brush_presets==settings.brush_presets
    loaded.brush_size_px=41
    save_settings(loaded)
    assert backup.read_bytes()==original


@pytest.mark.parametrize('fail_asset',[True,False])
def test_write_failures_leave_original_preferences_and_art_recoverable(tmp_path,monkeypatch,fail_asset):
    path=tmp_path/'preferences.json';use_path(monkeypatch,path)
    settings=EditorSettings(brush_presets=[library().to_dict()])
    original=json.dumps(asdict(settings)).encode();path.write_bytes(original)
    real=brush_storage.atomic_write
    def fail(target,write):
        if (target.suffix=='.png') if fail_asset else target==path:
            raise OSError('injected storage failure')
        return real(target,write)
    monkeypatch.setattr(brush_storage,'atomic_write',fail)
    with pytest.raises(OSError,match='injected'):
        save_settings(settings)
    assert path.read_bytes()==original
    assert load_settings().brush_presets==settings.brush_presets
    if not fail_asset:
        assert path.with_name(path.name+'.inline-backup').read_bytes()==original


@pytest.mark.parametrize('damage',['missing','corrupt','traversal'])
def test_unavailable_assets_fail_explicitly_without_defaulting_or_overwriting(tmp_path,monkeypatch,damage):
    path=tmp_path/'preferences.json';use_path(monkeypatch,path)
    save_settings(EditorSettings(brush_presets=[library().to_dict()]))
    asset=next((tmp_path/'brush-assets').glob('*.png'))
    if damage=='missing':asset.unlink()
    elif damage=='corrupt':asset.write_bytes(b'corrupted')
    else:
        raw=json.loads(path.read_text())
        raw['brush_presets'][0]['tips'][0]['png']={'$brush_png':'../outside'}
        path.write_text(json.dumps(raw))
    before=path.read_bytes()
    with pytest.raises(brush_storage.BrushAssetError):load_settings()
    assert path.read_bytes()==before


def test_in_memory_original_restores_a_deleted_cached_asset_on_next_save(tmp_path,monkeypatch):
    path=tmp_path/'preferences.json';use_path(monkeypatch,path)
    settings=EditorSettings(brush_presets=[library().to_dict()]);save_settings(settings)
    asset=next((tmp_path/'brush-assets').glob('*.png'));original=asset.read_bytes();asset.unlink()
    settings.brush_opacity=.4;save_settings(settings)
    assert asset.read_bytes()==original
    assert load_settings().brush_presets==settings.brush_presets


def test_asset_disappearing_during_read_does_not_trigger_settings_defaults(tmp_path,monkeypatch):
    path=tmp_path/'preferences.json';use_path(monkeypatch,path)
    save_settings(EditorSettings(brush_presets=[library().to_dict()]))
    real_stat=Path.stat
    reads=[]
    def changed(asset,*args,**kwargs):
        if asset.suffix=='.png':
            reads.append(asset)
            if len(reads)>1:raise FileNotFoundError('removed during read')
        return real_stat(asset,*args,**kwargs)
    monkeypatch.setattr(Path,'stat',changed)
    with pytest.raises(brush_storage.BrushAssetError):load_settings()


def test_changed_cached_file_is_repaired_without_losing_original_pixels(tmp_path,monkeypatch):
    path=tmp_path/'preferences.json';use_path(monkeypatch,path)
    settings=EditorSettings(brush_presets=[library().to_dict()]);save_settings(settings)
    asset=next((tmp_path/'brush-assets').glob('*.png'));original=asset.read_bytes()
    asset.write_bytes(b'externally damaged image')
    settings.brush_size_px=37;save_settings(settings)
    assert asset.read_bytes()==original
    assert load_settings().brush_presets==settings.brush_presets


def test_moving_preferences_and_asset_folder_preserves_every_pixel(tmp_path,monkeypatch):
    source=tmp_path/'source';target=tmp_path/'moved'
    path=source/'preferences.json';use_path(monkeypatch,path)
    brush=library();save_settings(EditorSettings(brush_presets=[brush.to_dict()]))
    shutil.copytree(source,target)
    use_path(monkeypatch,target/'preferences.json')
    assert BrushDefinition.from_dict(load_settings().brush_presets[0])==brush


def test_new_asset_is_added_without_removing_prior_art(tmp_path,monkeypatch):
    path=tmp_path/'preferences.json';use_path(monkeypatch,path)
    brush=library();settings=EditorSettings(brush_presets=[brush.to_dict()]);save_settings(settings)
    first=set((tmp_path/'brush-assets').glob('*.png'))
    edited=replace(brush,tips=(replace(brush.tips[0],png=png('blue')),))
    settings.brush_presets=[edited.to_dict()];save_settings(settings)
    assert first < set((tmp_path/'brush-assets').glob('*.png'))
    assert BrushDefinition.from_dict(load_settings().brush_presets[0])==edited
