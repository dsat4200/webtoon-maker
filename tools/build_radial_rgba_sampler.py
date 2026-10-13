"""Explicit developer build of the optional Windows RGBA sampler.

Never invoked by the application. No compiler discovery, installation or
download. Use verified TinyCC0.9.27 x86_64 Windows or an explicitly supplied
MSVC x64 toolchain with strict floating-point arithmetic. Ordinary rendering
falls back to SciPy when this component is missing/unsupported/invalid.
"""
import argparse
import hashlib
import json
import os
from pathlib import Path
import struct
import subprocess
import sys
import tempfile

def sha(path):return hashlib.sha256(Path(path).read_bytes()).hexdigest()

def inspect_pe(path):
    data=Path(path).read_bytes()
    def u16(offset):return struct.unpack_from('<H',data,offset)[0]
    def u32(offset):return struct.unpack_from('<I',data,offset)[0]
    def u64(offset):return struct.unpack_from('<Q',data,offset)[0]
    assert data[:2]==b'MZ';pe=u32(0x3c);assert data[pe:pe+4]==b'PE\0\0'
    machine=u16(pe+4);sections=u16(pe+6);optional=u16(pe+20);characteristics=u16(pe+22);header=pe+24
    assert machine==0x8664 and u16(header)==0x20b and characteristics&0x2000
    assert 0<sections<=32 and optional>=128
    section_rows=[]
    for index in range(sections):
        at=header+optional+index*40
        section_rows.append(dict(name=data[at:at+8].rstrip(b'\0').decode('ascii'),
            virtual_size=u32(at+8),rva=u32(at+12),raw_size=u32(at+16),raw_offset=u32(at+20)))
    def offset(rva,size=1):
        for row in section_rows:
            delta=rva-row['rva']
            if 0<=delta and delta+size<=row['raw_size']:
                value=row['raw_offset']+delta
                assert 0<=value and value+size<=len(data)
                return value
        raise RuntimeError('PE RVA outside a complete raw section')
    def string(rva):
        start=offset(rva);end=data.find(b'\0',start,min(len(data),start+4096));assert end>=start
        return data[start:end].decode('ascii')
    export_rva,export_size=u32(header+112),u32(header+116)
    assert export_rva and export_size>=40
    ex=offset(export_rva,40);count=u32(ex+24);assert 0<count<=128
    names=u32(ex+32);ordinals=u32(ex+36);functions=u32(ex+28);function_count=u32(ex+20)
    exports=[]
    for index in range(count):
        name=string(u32(offset(names+index*4,4)))
        ordinal=u16(offset(ordinals+index*2,2));assert ordinal<function_count
        function_rva=u32(offset(functions+ordinal*4,4))
        assert not export_rva<=function_rva<export_rva+export_size
        offset(function_rva)
        exports.append(name)
    assert set(exports)=={'radial_rgba_abi','radial_rgba_linear'}
    imports=[];import_rva=u32(header+120)
    if import_rva:
        for index in range(128):
            descriptor=offset(import_rva+index*20,20)
            original,name,first=u32(descriptor),u32(descriptor+12),u32(descriptor+16)
            if not (original or name or first):break
            library=string(name);thunk=original or first;symbols=[]
            for symbol_index in range(1024):
                value=u64(offset(thunk+symbol_index*8,8))
                if value==0:break
                symbols.append('ordinal:'+str(value&0xffff) if value&(1<<63) else string(value+2))
            else:raise RuntimeError('PE import symbol bound exceeded')
            imports.append(dict(library=library,symbols=symbols))
        else:raise RuntimeError('PE import library bound exceeded')
    return dict(machine='AMD64',magic='PE32+',dll=True,bytes=len(data),exports=sorted(exports),imports=imports,sections=section_rows)


def build_msvc(compiler, source, temporary, includes):
    """An explicit offline toolchain; no application/compiler discovery."""
    if not includes or any(not path.is_dir() for path in includes):
        raise ValueError('MSVC requires explicit existing VC/UCRT --include paths')
    linker = compiler.parent/'link.exe'
    if not linker.is_file():
        raise ValueError('Explicit x64 compiler must have its accompanying linker')
    version = subprocess.run([str(compiler), '/?'], capture_output=True, text=True, check=True)
    description = next((line for line in (version.stderr+version.stdout).splitlines()
        if 'Microsoft' in line and 'for x64' in line), '')
    if not description:
        raise ValueError('MSVC sampler requires the Microsoft x64 compiler')
    home = temporary.parent.resolve()
    support_source = 'int _fltused = 0;\n'
    commands = []
    with tempfile.TemporaryDirectory(prefix='radial_rgba_msvc_', dir=home) as directory:
        build = Path(directory).resolve()
        if not build.is_relative_to(home):
            raise ValueError('Temporary native build outside its owned directory')
        support = build/'float_marker.c'
        support.write_text(support_source, encoding='ascii')
        for path, name in ((source,'sampler'),(support,'float_marker')):
            command = [str(compiler),'/nologo','/TC','/W4','/O2','/fp:strict','/GS-','/Zl',
                '/Brepro','/c',*('/I'+str(path.resolve()) for path in includes),
                str(path),'/Fo'+str(build/(name+'.obj'))]
            commands.append(command)
            subprocess.run(command, cwd=build, check=True)
        target = build/'radial_rgba_win64.dll'
        command = [str(linker),'/NOLOGO','/DLL','/NOENTRY','/NODEFAULTLIB','/MACHINE:X64',
            '/OPT:REF','/OPT:ICF','/MANIFEST:NO','/Brepro','/OUT:'+str(target),
            str(build/'sampler.obj'),str(build/'float_marker.obj')]
        commands.append(command)
        subprocess.run(command, cwd=build, check=True)
        temporary.write_bytes(target.read_bytes())
    return dict(compiler_version=description, linker_sha256=sha(linker),
        support_source=support_source, commands=commands,
        build_options='/O2 /fp:strict; volatile scalar rounding checkpoints; /Brepro; no CRT imports')

def main():
    parser=argparse.ArgumentParser()
    parser.add_argument('--compiler',type=Path,required=True)
    parser.add_argument('--compiler-kind',choices=('tinycc','msvc'),default='tinycc')
    parser.add_argument('--include',type=Path,action='append',default=[])
    parser.add_argument('--replace-existing',action='store_true')
    args=parser.parse_args()
    root=Path(__file__).resolve().parents[1];home=root/'comic_editor/ui'
    compiler=args.compiler.resolve();source=home/'radial_rgba_linear.c'
    binary=home/'radial_rgba_win64.dll';record=home/'radial_rgba_build.json'
    assert sys.platform=='win32' and struct.calcsize('P')==8 and compiler.is_file()
    assert sha(source)=='a4115eadf1bf81b01ce5b40cd467b36cd8536bce76b43ad00eaf0584f8d240a8'
    if not args.replace_existing and (binary.exists() or record.exists()):
        raise RuntimeError('Explicit --replace-existing required to replace shipped component')
    temporary=home/'radial_rgba_build_temporary.dll'
    if temporary.exists():raise RuntimeError('Temporary build exists; inspect before rebuilding')
    if args.compiler_kind == 'msvc':
        build = build_msvc(compiler,source,temporary,args.include)
        description = build['compiler_version']
        command = build['commands']
    else:
        version=subprocess.run([str(compiler),'-v'],capture_output=True,text=True,check=True)
        description=version.stdout+version.stderr
        assert '0.9.27' in description and 'x86_64' in description and 'Windows' in description
        command=[str(compiler),'-Wall','-shared',str(source),'-o',str(temporary)]
        subprocess.run(command,check=True)
        build = {}
    inspection=inspect_pe(temporary)
    assert inspection['imports']==[]
    metadata=dict(abi='0x5247424142490001',platform='win-amd64',
        build_contract='double-separate-y-x-corners-00-01-10-11-channel-independent-f32-no-fastmath-v3',
        source_sha256=sha(source),binary_sha256=sha(temporary),compiler_version=description.strip(),
        compiler_sha256=sha(compiler),pe_inspection=inspection,
        command=['<explicit-tcc.exe>','-Wall','-shared','comic_editor/ui/radial_rgba_linear.c','-o','comic_editor/ui/radial_rgba_win64.dll'],
        scope='Explicit developer build; new binary requires native bit and performance validation before distribution.')
    if args.compiler_kind == 'msvc':
        metadata.update(build)
        metadata['command'] = command
    record_tmp=home/'radial_rgba_build_temporary.json'
    with record_tmp.open('x',encoding='utf8') as stream:json.dump(metadata,stream,indent=2)
    os.replace(temporary,binary);os.replace(record_tmp,record)
    print(json.dumps({'source_sha256':metadata['source_sha256'],'binary_sha256':metadata['binary_sha256']}))

if __name__=='__main__':main()
