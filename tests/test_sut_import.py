"""Importer checks use original generated fixtures, not redistributed CSP art."""
import base64
import hashlib
import io
from pathlib import Path
import sqlite3
import struct
import tarfile
import zlib

import pytest
from PIL import Image

from comic_editor.core.brushes import BrushDefinition
from comic_editor.core.sut_import import (SutImportError, _c2f_rows, _dynamics,
                                          _material_file, _references, import_sut)
import comic_editor.core.sut_import as sut_module


def _uint(value):
    return struct.pack('>I', value)


def _string(value):
    data = value.encode('utf-16-le')
    return _uint(len(data)) + data


def _reference(path='.:test:data:tip.png', name='Original tip'):
    body = _string(path) + _uint(2) + _string(name) + _uint(1) + _string('.:test') + _uint(0)
    return _uint(8) + _uint(1) + _uint(len(body)+4) + body


def _png(color, size=(3, 5)):
    out = io.BytesIO()
    Image.new('RGBA', size, color).save(out, format='PNG')
    return out.getvalue()


def _tar(entries):
    out = io.BytesIO()
    with tarfile.open(fileobj=out, mode='w') as archive:
        for name, data in entries:
            info = tarfile.TarInfo(name)
            info.size = len(data)
            archive.addfile(info, io.BytesIO(data))
    return out.getvalue()


def _sut(tmp_path, *, pattern=False, material=None):
    path = tmp_path/'test.sut'
    with sqlite3.connect(path) as connection:
        connection.executescript('''
          CREATE TABLE Manager(Version INTEGER);
          INSERT INTO Manager VALUES(131);
          CREATE TABLE Node(NodeName TEXT, NodeVariantID INTEGER, NodeInitVariantID INTEGER);
          INSERT INTO Node VALUES('Fixture brush', 99, 1);
          CREATE TABLE Variant(VariantID INTEGER, BrushSize REAL, Opacity INTEGER,
            BrushUsePatternImage INTEGER, BrushPatternImageArray BLOB, BrushSizeEffector BLOB,
            BrushInterval REAL, BrushAutoIntervalType INTEGER, FutureCspSetting BLOB);
          INSERT INTO Variant VALUES(1,2,10,0,NULL,NULL,10,0,NULL);
          CREATE TABLE MaterialFile(OriginalPath TEXT, FileData BLOB);
        ''')
        connection.execute('INSERT INTO Variant VALUES(99,24,73,?,?,NULL,25,0,?)',
                           (int(pattern),_reference() if pattern else None,b'future'))
        if material:
            connection.execute('INSERT INTO MaterialFile VALUES(?,?)',('.:test:data:tip.png',material))
    return path


def test_active_variant_not_first_row_and_source_unchanged(tmp_path):
    path = _sut(tmp_path)
    before = path.read_bytes()
    brush = import_sut(path)
    assert brush.size == 24
    assert brush.opacity == .73
    assert brush.spacing == .25
    assert brush.name == 'Fixture brush'
    assert brush.source['variant']['FutureCspSetting']['data'] == base64.b64encode(b'future').decode()
    assert path.read_bytes() == before
    assert brush.source['sha256'] == hashlib.sha256(before).hexdigest()


def test_original_png_selected_instead_of_thumbnail(tmp_path):
    material = _tar([('thumbnail/thumbnail.png',_png((255,0,0,255),(100,100))),
                     ('data/tip.png',_png((0,0,255,192),(3,5)))])
    brush = import_sut(_sut(tmp_path,pattern=True,material=material))
    assert (brush.tips[0].width,brush.tips[0].height) == (3,5)
    image = Image.open(io.BytesIO(base64.b64decode(brush.tips[0].png)))
    assert image.getpixel((1,2)) == (0,0,255,192)


@pytest.mark.parametrize('angle,expected_warning', [(26, True), (37.1, True),
    (0, False), (90, False), (180, False), (270, False)])
def test_noncardinal_ribbon_warning_retains_source_angle(tmp_path, angle, expected_warning):
    material = _tar([('data/tip.png', _png((0,0,0,255)))])
    path = _sut(tmp_path, pattern=True, material=material)
    with sqlite3.connect(path) as connection:
        connection.execute('ALTER TABLE Variant ADD COLUMN BrushRibbon INTEGER')
        connection.execute('ALTER TABLE Variant ADD COLUMN BrushRotation REAL')
        connection.execute('ALTER TABLE Variant ADD COLUMN BrushRotationEffector INTEGER')
        connection.execute('UPDATE Variant SET BrushRibbon=1, BrushRotation=?, BrushRotationEffector=67 WHERE VariantID=99', (angle,))
    brush = import_sut(path)
    assert brush.ribbon and brush.direction == 'stroke'
    assert brush.angle == angle
    assert brush.source['variant']['BrushRotation'] == angle
    assert any('Angled ribbon tips may show gaps' in warning for warning in brush.warnings) == expected_warning


def test_material_read_budget_tracks_actual_payload(monkeypatch):
    payload = _png((10,20,30,255))
    material = _tar([('data/tip.png', payload)])
    original_read = tarfile.ExFileObject.read
    reads = []

    def bounded_read(stream, size=-1):
        reads.append(size)
        assert 0 <= size <= len(payload)+1
        return original_read(stream, size)

    monkeypatch.setattr(tarfile.ExFileObject, 'read', bounded_read)
    image, _ = _material_file(material, '.:test:data:tip.png')
    assert image.getpixel((1,2)) == (10,20,30,255)
    assert reads


def test_missing_active_material_does_not_become_round_brush(tmp_path):
    with pytest.raises(SutImportError, match='missing material'):
        import_sut(_sut(tmp_path,pattern=True))


def test_non_sut_file_rejected(tmp_path):
    path = tmp_path/'fake.sut'
    path.write_bytes(b'not a database')
    with pytest.raises(SutImportError, match='not a supported'):
        import_sut(path)


def test_material_reference_lengths_and_unicode():
    refs = _references(_reference(name='葉のブラシ'))
    assert refs[0]['name'] == '葉のブラシ'
    with pytest.raises(SutImportError):
        _references(_reference()[:-12])


def test_pressure_curve_big_endian_and_random_minimum():
    points = ((0.,0.),(.4,.15),(1.,1.))
    curve = struct.pack('>III',12,len(points),16)+b''.join(struct.pack('>dd',*p) for p in points)
    header = struct.pack('>11I',44,496,0x90,25,0,0,40,0,len(curve),0,100)
    dynamics = _dynamics(header+curve)
    assert dynamics.pressure
    assert dynamics.minimum == .25
    assert dynamics.random == .4
    assert dynamics.pressure_curve == points
    assert not dynamics.tilt


def _c2f_fixture(tmp_path, page_size=1024):
    """Synthetic SQLite overflow + C2F + actual tiled raster, including padding."""
    alpha = bytearray(65536)
    # A pixel at a known location, with transparent registered padding.
    alpha[2*256+1] = 201
    # Enough non-compressible image data to require SQLite overflow pages.
    import random
    gray = random.Random(712).randbytes(65536)
    gray = bytearray(gray)
    gray[2*256+1] = 0
    compressed = zlib.compress(bytes(alpha)+bytes(gray))
    begin = _uint(19)+'BlockDataBeginChunk'.encode('utf-16-be')
    end = _uint(17)+'BlockDataEndChunk'.encode('utf-16-be')
    tile = (begin + struct.pack('>4I',0,2*65536,256,256)+_uint(1)
            +_uint(len(compressed)+4)+struct.pack('<I',len(compressed))+compressed+end)
    block = _uint(len(tile)+4)+tile
    # parameter layout: width,height,columns,rows,channel order,depth,channels,pixel bytes,...
    values = (8,6,1,1,17,1,1,2,65536,1,256,1,256,65536,256,256,8,8,0,0)
    attribute = _uint(16)*4 + _uint(9)+'Parameter'.encode('utf-16-be')+struct.pack('>20I',*values)
    path=tmp_path/'material.sqlite'
    with sqlite3.connect(path) as connection:
        connection.execute(f'PRAGMA page_size={page_size}')
        connection.execute('CREATE TABLE Padding(data BLOB)')
        connection.execute('INSERT INTO Padding VALUES(?)',(bytes(7000),))
        connection.execute('CREATE TABLE Offscreen(id INTEGER PRIMARY KEY,MainId INTEGER,CanvasId INTEGER,LayerId INTEGER,Attribute BLOB,BlockData BLOB)')
        connection.execute('INSERT INTO Offscreen VALUES(1,17,0,3,?,?)',(attribute,block))
    sqlite=path.read_bytes()
    def chunk(kind,payload):
        return struct.pack('<I',len(payload))+kind+payload+struct.pack('<I',zlib.crc32(kind+payload))
    return (b'\x89C2F\r\n\x1a\n'+chunk(b'HEAD',b'')+chunk(b'dATA',b'\1\0'+bytes(5128))
            +chunk(b'dATA',b'\0\0'+sqlite[5120:])+chunk(b'TAIL',b''))


@pytest.mark.parametrize('page_size',[1024,4096])
def test_actual_c2f_tiles_and_sqlite_overflow_recovered(tmp_path,page_size):
    c2f = _c2f_fixture(tmp_path,page_size)
    material = _tar([('data/material_0.layer',c2f),
                     ('thumbnail/thumbnail.png',_png((255,255,255,255),(64,64)))])
    image,metadata = _material_file(material,'.:fixture:data:material_0.layer')
    assert image.size == (8,6)
    assert image.getpixel((1,2)) == (0,0,0,201)
    assert image.getpixel((7,5))[3] == 0
    assert metadata['offscreen_id'] == 17


def test_c2f_checksum_failure_rejected(tmp_path):
    data = bytearray(_c2f_fixture(tmp_path))
    data[-20] ^= 1
    with pytest.raises(SutImportError, match='checksum'):
        _c2f_rows(bytes(data))


def test_optional_local_reference_pack_all_brushes():
    samples = Path(__file__).resolve().parents[1]/'.artifacts/brush-investigation/samples'
    files = sorted(samples.glob('*.sut'))
    if not files:
        pytest.skip('Private/reference brushes are not distributed with the test suite.')
    for path in files:
        brush = import_sut(path)
        assert brush.tips
        assert brush.source['variant']['VariantID'] == brush.source['node']['NodeVariantID']
        for tip in brush.tips:
            if tip.shape == 'image':
                image = Image.open(io.BytesIO(base64.b64decode(tip.png)))
                assert image.size == (tip.width,tip.height)
                assert image.getchannel('A').getextrema()[1] > 0


def test_dual_fields_are_imported_only_when_enabled(tmp_path):
    path = _sut(tmp_path)
    with sqlite3.connect(path) as connection:
        for name,kind in [('UseDualBrush','INTEGER'),('DualSize','REAL'),('DualFlow','INTEGER'),
                          ('DualHardness','INTEGER'),('DualUsePatternImage','INTEGER'),
                          ('SyncDualBrushSize','INTEGER'),('DualBrushCompositeMode','INTEGER')]:
            connection.execute(f'ALTER TABLE Variant ADD COLUMN {name} {kind}')
        connection.execute('UPDATE Variant SET UseDualBrush=0,DualSize=12,DualFlow=37,DualHardness=70,DualUsePatternImage=0,SyncDualBrushSize=1,DualBrushCompositeMode=3 WHERE VariantID=99')
    assert import_sut(path).dual is None
    with sqlite3.connect(path) as connection:
        connection.execute('UPDATE Variant SET UseDualBrush=1 WHERE VariantID=99')
    brush=import_sut(path)
    assert brush.dual is not None
    assert brush.dual.size==12
    assert brush.dual.density==.37
    assert brush.dual.hardness==.7
    assert brush.dual_link_size
    assert brush.dual_mode=='subtract'


def test_color_jitter_respects_enable_flags(tmp_path):
    path=_sut(tmp_path)
    with sqlite3.connect(path) as connection:
        for key in ['BrushChangePatternColor','BrushHueChange','BrushSaturationChange',
                    'BrushValueChange','BrushChangeStrokeColor','BrushStrokeHueChange','BrushSubColor']:
            connection.execute(f'ALTER TABLE Variant ADD COLUMN {key} INTEGER')
        connection.execute('ALTER TABLE Variant ADD COLUMN BrushHueChangeEffector BLOB')
        connection.execute('UPDATE Variant SET BrushChangePatternColor=0,BrushHueChange=17,BrushSaturationChange=15,BrushValueChange=25,BrushChangeStrokeColor=0,BrushStrokeHueChange=30,BrushSubColor=45 WHERE VariantID=99')
        connection.execute('UPDATE Variant SET BrushHueChangeEffector=? WHERE VariantID=99',
                           (struct.pack('>11I',44,0xf0,0x80,0,100,0,0xffffff9c,0,0,0,500),))
    brush=import_sut(path)
    assert (brush.hue_shift,brush.saturation_shift,brush.luminosity_shift,brush.sub_color_amount,brush.stroke_hue_jitter)==(0,0,0,0,0)
    assert 'hue_shift' not in brush.dynamics
    assert not any('Color changes' in warning for warning in brush.warnings)
    with sqlite3.connect(path) as connection:
        connection.execute('UPDATE Variant SET BrushChangePatternColor=1,BrushChangeStrokeColor=1 WHERE VariantID=99')
    brush=import_sut(path)
    assert brush.hue_shift==pytest.approx(17/360)
    assert (brush.saturation_shift,brush.luminosity_shift,brush.sub_color_amount)==(.15,.25,.45)
    assert brush.stroke_hue_jitter==pytest.approx(30/360)
    assert brush.dynamics['hue_shift'].random==-1
    assert (brush.hue_jitter,brush.saturation_jitter,brush.luminosity_jitter,brush.sub_color_mix)==(0,0,0,0)
    assert any('combined-input equation' in warning for warning in brush.warnings)


def test_signed_color_minima_are_enabled_only_and_survive_portable_roundtrip(tmp_path):
    """Real-source regressions: negative offsets and inactive stored randomness."""
    path=_sut(tmp_path)
    curve=struct.pack('>III',12,3,16)+b''.join(struct.pack('>dd',*p) for p in [(0.,1.),(.4,.75),(1.,0.)])
    # Hue enables pressure+random; value enables pressure alone, like Thin chain.
    hue=struct.pack('>11i',44,240,0x90,-25,100,0,-35,0,len(curve),0,500)+curve
    value=struct.pack('>11i',44,240,0x10,0,100,0,-100,0,len(curve),0,500)+curve
    sub=struct.pack('>11i',44,240,0,100,100,0,0,0,0,0,500)
    with sqlite3.connect(path) as c:
        fields={'BrushChangePatternColor':1,'BrushHueChange':200,'BrushSaturationChange':-10,
                'BrushValueChange':-20,'BrushSubColor':60,'BrushChangeColorTarget':2,
                'BrushChangeStrokeColor':1,'BrushStrokeHueChange':180,
                'BrushStrokeSaturationChange':-30,'BrushStrokeValueChange':15}
        for key,value_ in fields.items():
            c.execute(f'ALTER TABLE Variant ADD COLUMN {key} INTEGER')
            c.execute(f'UPDATE Variant SET {key}=? WHERE VariantID=99',(value_,))
        for key,blob in [('BrushHueChangeEffector',hue),('BrushValueChangeEffector',value),('BrushSubColorEffector',sub)]:
            c.execute(f'ALTER TABLE Variant ADD COLUMN {key} BLOB')
            c.execute(f'UPDATE Variant SET {key}=? WHERE VariantID=99',(blob,))
    original=path.read_bytes()
    brush=import_sut(path)
    assert path.read_bytes()==original
    assert brush.hue_shift==pytest.approx(200/360)
    assert brush.saturation_shift==-.1 and brush.luminosity_shift==-.2
    assert brush.sub_color_amount==.6 and brush.sub_color_mix==0
    assert brush.color_change_target=='sub'
    assert brush.stroke_hue_jitter==.5
    assert brush.stroke_saturation_jitter==.3 and brush.stroke_luminosity_jitter==.15
    assert any('signed per-stroke color amount' in warning for warning in brush.warnings)
    assert brush.dynamics['hue_shift'].minimum==-.25
    assert brush.dynamics['hue_shift'].random==-.35
    assert brush.dynamics['luminosity_shift'].pressure
    assert brush.dynamics['luminosity_shift'].random==1
    assert brush.dynamics['luminosity_shift'].pressure_curve==((0.,1.),(.4,.75),(1.,0.))
    assert brush.dynamics['sub_color_amount'].random==1
    restored=BrushDefinition.from_dict(brush.to_dict())
    assert restored.hue_shift==brush.hue_shift
    assert restored.dynamics['hue_shift'].minimum==-.25
    assert restored.dynamics['hue_shift'].random==-.35
    assert restored.color_change_target=='sub'
    assert brush.source['variant']['BrushHueChangeEffector']['data']==base64.b64encode(hue).decode()


def test_signed_dynamics_do_not_change_positive_only_parameters():
    blob=struct.pack('>11i',44,240,0xf0,-25,-40,-60,-100,0,0,0,500)
    signed=_dynamics(blob,signed=True)
    assert (signed.minimum,signed.tilt_minimum,signed.velocity_minimum,signed.random)==(-.25,-.4,-.6,-1)
    positive=_dynamics(blob)
    assert all(0<=x<=1 for x in (positive.minimum,positive.tilt_minimum,positive.velocity_minimum,positive.random))


def test_unmapped_full_color_subcolor_and_malformed_color_curve_are_reported(tmp_path):
    path=_sut(tmp_path,pattern=True,material=_tar([('data/tip.png',_png((40,100,180,255)))]))
    with sqlite3.connect(path) as c:
        for key in ['BrushChangePatternColor','BrushHueChange','BrushSubColor']:
            c.execute(f'ALTER TABLE Variant ADD COLUMN {key} INTEGER')
        c.execute('ALTER TABLE Variant ADD COLUMN BrushHueChangeEffector BLOB')
        c.execute('UPDATE Variant SET BrushChangePatternColor=1,BrushHueChange=-170,BrushSubColor=50,BrushHueChangeEffector=? WHERE VariantID=99',(b'bad',))
    brush=import_sut(path)
    assert brush.hue_shift==pytest.approx(-170/360)
    assert brush.sub_color_amount==.5
    assert any('full-color image tips' in warning for warning in brush.warnings)
    assert any('BrushHueChangeEffector' in warning and 'original bytes' in warning for warning in brush.warnings)


def test_csp_reverse_sequence_is_back_and_forth(tmp_path):
    path=_sut(tmp_path)
    with sqlite3.connect(path) as connection:
        connection.execute('ALTER TABLE Variant ADD COLUMN BrushPatternOrderType2 INTEGER')
        connection.execute('ALTER TABLE Variant ADD COLUMN BrushPatternReverseHorizontal INTEGER')
        connection.execute('UPDATE Variant SET BrushPatternOrderType2=1,BrushPatternReverseHorizontal=3 WHERE VariantID=99')
    brush=import_sut(path)
    assert brush.repeat_mode=='pingpong'
    assert brush.flip_x=='reverse'
    with sqlite3.connect(path) as connection:
        connection.execute('UPDATE Variant SET BrushPatternOrderType2=4 WHERE VariantID=99')
    assert import_sut(path).repeat_mode=='once'
    with sqlite3.connect(path) as connection:
        connection.execute('UPDATE Variant SET BrushPatternOrderType2=5 WHERE VariantID=99')
    assert import_sut(path).repeat_mode=='one_random'


@pytest.mark.parametrize('source_mode,expected_mode,lengths',[
    (0,'length',(7,9)),(1,'percentage',(35,60)),(2,'fade',(0,9)),(99,'length',(0,0))])
def test_taper_keeps_length_percentage_and_unsupported_modes_distinct(tmp_path,source_mode,expected_mode,lengths):
    path=_sut(tmp_path)
    target=struct.pack('>9I',12,2,12,1001,1,80,1021,1,30)
    with sqlite3.connect(path) as connection:
        for name,kind in [('BrushInOutTarget','BLOB'),('BrushInOutType','INTEGER'),
                          ('BrushUseIn','INTEGER'),('BrushUseOut','INTEGER'),
                          ('BrushInLength','REAL'),('BrushOutLength','REAL'),
                          ('BrushInRatio','REAL'),('BrushOutRatio','REAL'),
                          ('BrushUseRevision','INTEGER'),('BrushRevision','INTEGER')]:
            connection.execute(f'ALTER TABLE Variant ADD COLUMN {name} {kind}')
        connection.execute('UPDATE Variant SET BrushInOutTarget=?,BrushInOutType=?,BrushUseIn=1,BrushUseOut=1,BrushInLength=7,BrushOutLength=9,BrushInRatio=35,BrushOutRatio=60,BrushUseRevision=0,BrushRevision=40 WHERE VariantID=99',(target,source_mode))
    brush=import_sut(path)
    assert brush.taper_mode==expected_mode
    assert (brush.taper_start,brush.taper_end)==lengths
    assert brush.taper_minimum==.8
    assert brush.taper_minima=={'size':.8,'opacity':.3}
    assert not any('first active parameter minimum' in warning for warning in brush.warnings)
    assert brush.post_correction==0
    if source_mode==2:
        assert any('Fade' in warning for warning in brush.warnings)
    if source_mode==99:
        assert any('not mapped' in warning for warning in brush.warnings)
    with sqlite3.connect(path) as connection:
        connection.execute('UPDATE Variant SET BrushUseRevision=1 WHERE VariantID=99')
    assert import_sut(path).post_correction==.4


def test_source_pressure_calibration_is_preserved_for_primary_and_secondary(tmp_path):
    path=_sut(tmp_path)
    knots=((0.,0.),(.35,.8),(1.,1.))
    graph=struct.pack('>3I',12,len(knots),16)+b''.join(struct.pack('>dd',*point) for point in knots)
    with sqlite3.connect(path) as connection:
        connection.execute('ALTER TABLE Manager ADD COLUMN PressureGraph BLOB')
        connection.execute('ALTER TABLE Manager ADD COLUMN PressureGraphInitialized INTEGER')
        connection.execute('ALTER TABLE Manager ADD COLUMN UsePressureFile INTEGER')
        connection.execute('UPDATE Manager SET PressureGraph=?,PressureGraphInitialized=1,UsePressureFile=1',(graph,))
        for name,kind in [('UseDualBrush','INTEGER'),('DualSize','REAL')]:
            connection.execute(f'ALTER TABLE Variant ADD COLUMN {name} {kind}')
        connection.execute('UPDATE Variant SET UseDualBrush=1,DualSize=5 WHERE VariantID=99')
    before=path.read_bytes()
    brush=import_sut(path)
    assert brush.global_pressure_curve==knots
    assert brush.dual.global_pressure_curve==knots
    assert path.read_bytes()==before
    assert any('app tablet preferences are unchanged' in warning for warning in brush.warnings)
    with sqlite3.connect(path) as connection:
        connection.execute('UPDATE Manager SET UsePressureFile=0')
    disabled=import_sut(path)
    assert disabled.global_pressure_curve==((0.,0.),(1.,1.))
    assert disabled.source['manager']['PressureGraph']['data']==base64.b64encode(graph).decode()
    with sqlite3.connect(path) as connection:
        connection.execute('UPDATE Manager SET UsePressureFile=1,PressureGraph=?',(b'broken graph',))
    corrupt=import_sut(path)
    assert corrupt.global_pressure_curve==((0.,0.),(1.,1.))
    assert any('retained without applying it' in warning for warning in corrupt.warnings)


def _add_dual_tip(path, reference):
    with sqlite3.connect(path) as connection:
        for name,kind in [('UseDualBrush','INTEGER'),('DualSize','REAL'),
                          ('DualUsePatternImage','INTEGER'),('DualPatternImageArray','BLOB')]:
            connection.execute(f'ALTER TABLE Variant ADD COLUMN {name} {kind}')
        connection.execute('UPDATE Variant SET UseDualBrush=1,DualSize=12,DualUsePatternImage=1,DualPatternImageArray=? WHERE VariantID=99',(reference,))


def test_decoded_budget_counts_shared_primary_secondary_tip_once(tmp_path,monkeypatch):
    material=_tar([('data/tip.png',_png((5,10,20,255),(3,5)))])
    path=_sut(tmp_path,pattern=True,material=material)
    _add_dual_tip(path,_reference(name='Secondary display name'))
    monkeypatch.setattr(sut_module,'MAX_TOTAL_IMAGE_PIXELS',15)
    decode=sut_module._material_file
    calls=[]
    def tracked_decode(blob,original_path,**kwargs):
        calls.append((original_path,kwargs['remaining_pixels']))
        return decode(blob,original_path,**kwargs)
    monkeypatch.setattr(sut_module,'_material_file',tracked_decode)
    brush=import_sut(path)
    assert len(calls)==1
    assert brush.tips[0].png is brush.dual.tips[0].png
    assert brush.source['materials'][0]['name']=='Original tip'
    assert brush.dual.source['materials'][0]['name']=='Secondary display name'
    assert brush.source['materials'][0]['path']==brush.dual.source['materials'][0]['path']


def test_material_import_releases_previous_pixels_and_reuses_lossless_png(monkeypatch):
    original_decode = sut_module._material_file
    decoded_paths = []

    def tracked_decode(blob, path, **kwargs):
        decoded_paths.append(path)
        return original_decode(blob,path,**kwargs)

    monkeypatch.setattr(sut_module,'_material_file',tracked_decode)
    cache = sut_module._MaterialCache()
    first_path, second_path = '.:a:data:tip.png', '.:b:data:tip.png'
    first_blob = _tar([('data/tip.png',_png((10,20,30,192),(3,5)))])
    second_blob = _tar([('data/tip.png',_png((60,70,80,255),(2,4)))])
    try:
        first,_ = cache.get(first_path,first_blob)
        encoded = cache.png(first_path)
        second,_ = cache.get(second_path,second_blob)
        with pytest.raises(ValueError,match='closed'):
            first.getpixel((0,0))
        cache.png(second_path)
        restored,_ = cache.get(first_path,first_blob)
        with pytest.raises(ValueError,match='closed'):
            second.getpixel((0,0))
        assert restored.getpixel((1,2)) == (10,20,30,192)
        assert cache.png(first_path) is encoded
        assert decoded_paths == [first_path,second_path]
        assert cache.pixels == 23
        assert len(cache.decoded) == 1
    finally:
        cache.close()
    with pytest.raises(ValueError,match='closed'):
        restored.getpixel((0,0))


def test_imported_device_curve_removes_identical_duplicate_knots(tmp_path):
    path = _sut(tmp_path)
    points = ((0.,0.),(.7,1.),(.7,1.),(1.,1.))
    curve = struct.pack('>III',12,len(points),16)+b''.join(struct.pack('>dd',*p) for p in points)
    with sqlite3.connect(path) as connection:
        connection.execute('ALTER TABLE Manager ADD COLUMN PressureGraph BLOB')
        connection.execute('UPDATE Manager SET PressureGraph=?',(curve,))
    brush = import_sut(path)
    assert brush.global_pressure_curve == ((0.,0.),(.7,1.),(1.,1.))
    assert BrushDefinition.from_dict(brush.to_dict()).to_dict() == brush.to_dict()


def test_decoded_budget_is_shared_across_distinct_primary_secondary_tips(tmp_path,monkeypatch):
    path=_sut(tmp_path,pattern=True,material=_tar([('data/tip.png',_png((1,2,3,255),(3,5)))]))
    second='.:second:data:second.png'
    _add_dual_tip(path,_reference(path=second,name='Second tip'))
    with sqlite3.connect(path) as connection:
        connection.execute('INSERT INTO MaterialFile VALUES(?,?)',
                           (second,_tar([('data/second.png',_png((4,5,6,255),(2,2)))])))
    monkeypatch.setattr(sut_module,'MAX_TOTAL_IMAGE_PIXELS',18)
    decode=sut_module._material_file
    remaining=[]
    def tracked_decode(blob,original_path,**kwargs):
        remaining.append(kwargs['remaining_pixels'])
        return decode(blob,original_path,**kwargs)
    monkeypatch.setattr(sut_module,'_material_file',tracked_decode)
    before=path.read_bytes()
    with pytest.raises(SutImportError,match='Combined brush materials'):
        import_sut(path)
    assert remaining==[18,3]
    assert path.read_bytes()==before


def test_c2f_original_obeys_aggregate_budget_before_raster_allocation(tmp_path,monkeypatch):
    material=_tar([('data/material_0.layer',_c2f_fixture(tmp_path))])
    path=_sut(tmp_path,pattern=True,material=material)
    monkeypatch.setattr(sut_module,'MAX_TOTAL_IMAGE_PIXELS',47)  # Original is 8 x 6.
    with pytest.raises(SutImportError,match='Combined brush materials'):
        import_sut(path)


@pytest.mark.parametrize('extra_reference',['secondary','texture'])
def test_serialized_image_budget_counts_each_published_reference(tmp_path,monkeypatch,extra_reference):
    path=_sut(tmp_path,pattern=True,material=_tar([('data/tip.png',_png((1,2,3,255)))]))
    monkeypatch.setattr(sut_module,'_image_png',lambda image:'A'*9)
    monkeypatch.setattr(sut_module,'MAX_SERIALIZED_IMAGE_BYTES',9)
    assert import_sut(path).tips[0].png=='A'*9  # Exactly at the limit is valid.
    if extra_reference=='secondary':
        _add_dual_tip(path,_reference(name='Repeated primary material'))
    else:
        with sqlite3.connect(path) as connection:
            connection.execute('ALTER TABLE Variant ADD COLUMN TextureImage BLOB')
            connection.execute('UPDATE Variant SET TextureImage=? WHERE VariantID=99',(_reference(),))
    monkeypatch.setattr(sut_module,'MAX_SERIALIZED_IMAGE_BYTES',17)
    with pytest.raises(SutImportError,match='serialized-image import limit'):
        import_sut(path)


def test_optional_installed_csp_reconstructed_fixtures():
    folder=Path(__file__).resolve().parents[1]/'.artifacts/brush-investigation/installed/reconstructed'
    paths=list(folder.glob('*.reconstructed.sut'))
    if not paths:
        pytest.skip('Locally reconstructed CSP fixtures are not distributed.')
    for path in paths:
        brush=import_sut(path)
        assert brush.source['reconstruction']['kind'].endswith('NOT a native CSP export')
        assert bool(brush.dual)==bool(brush.source['variant'].get('UseDualBrush'))
        for definition in [brush]+([brush.dual] if brush.dual else []):
            assert definition.tips
            for tip in definition.tips:
                if tip.shape=='image':
                    image=Image.open(io.BytesIO(base64.b64decode(tip.png)))
                    assert image.size==(tip.width,tip.height)
                    assert image.getchannel('A').getextrema()[1]>0
