#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""Install only lab8_prep/vendor from hashed official offline artifacts. No pip/sudo."""
from __future__ import print_function
import argparse
import glob
import hashlib
import io
import json
import os
import platform
import shutil
import subprocess
import sys
import sysconfig
import tarfile
import tempfile
import zipfile

ROOT=os.path.dirname(os.path.dirname(os.path.realpath(__file__)))
if sys.version_info[0] == 2 and isinstance(ROOT, str):
    ROOT=ROOT.decode(sys.getfilesystemencoding() or 'utf-8')
DEPS=os.path.join(ROOT, u'依赖')
VENDOR=os.path.join(ROOT, 'vendor')


def manifest():
    with io.open(os.path.join(DEPS,'downloads.sha256.json'),'r',encoding='utf-8-sig') as handle:
        return dict((row['filename'],row) for row in json.load(handle))


def verify(path, entries):
    digest=hashlib.sha256()
    with open(path,'rb') as handle:
        for data in iter(lambda:handle.read(1048576),b''): digest.update(data)
    if digest.hexdigest()!=entries[os.path.basename(path)]['sha256']:
        raise ValueError('SHA256 mismatch: '+os.path.basename(path))


def unpack_wheel(path,target):
    if not os.path.isdir(target): os.makedirs(target)
    with zipfile.ZipFile(path) as archive:
        for name in archive.namelist():
            destination=os.path.realpath(os.path.join(target,name))
            if not destination.startswith(os.path.realpath(target)+os.sep):
                raise ValueError('unsafe archive path')
        archive.extractall(target)


def source_preflight():
    # 3.23.0 src/common.h uses Python.h only in the Windows/MSVC/MinGW branch.
    # Linux native libraries expose a C ABI loaded by ctypes; don't reject a
    # usable Linux C compiler just because Python development headers are absent.
    try: compiler=subprocess.call(['gcc','--version'],stdout=subprocess.PIPE,stderr=subprocess.PIPE)==0
    except OSError: compiler=False
    include=sysconfig.get_path('include')
    headers=os.path.isfile(os.path.join(include,'Python.h'))
    ctypes_ok=False; native_probe=False; error=''
    try:
        import ctypes
        ctypes_ok=True
    except ImportError as exc: error=str(exc)
    if compiler and ctypes_ok and sys.platform.startswith('linux'):
        parent=os.path.join(ROOT,'runtime','preflight')
        if not os.path.isdir(parent): os.makedirs(parent)
        temporary=tempfile.mkdtemp(prefix='gcc_ctypes_',dir=parent)
        try:
            c_path=os.path.join(temporary,'probe.c'); so_path=os.path.join(temporary,'probe.so')
            with open(c_path,'w') as handle:
                handle.write('#include <stdint.h>\n#include <stdlib.h>\n#include <string.h>\nint lab8_probe(void) { return 823; }\n')
            subprocess.check_output(['gcc','-shared','-fPIC','-std=c99',c_path,'-o',so_path],stderr=subprocess.STDOUT)
            native_probe=ctypes.CDLL(so_path).lab8_probe()==823
        except Exception as exc: error=str(exc)
        finally:
            # This path is a fresh mkdtemp child of this package's runtime.
            if os.path.realpath(temporary).startswith(os.path.realpath(parent)+os.sep):
                shutil.rmtree(temporary)
    elif sys.platform.startswith('win'):
        error='source helper is prepared for Linux; use supplied Windows wheel'
    print(json.dumps({'event':'SOURCE_BUILD_PREFLIGHT','python':sys.version,
                      'platform':platform.platform(),'machine':platform.machine(),
                      'gcc':compiler,'Python_h':headers,'include':include,
                      'Python_h_required_on_Linux':False,'ctypes':ctypes_ok,
                      'native_C_compile_and_load':native_probe,'error':error,
                      'scope':'temporary_local_C_probe_no_install_no_network'}))
    return compiler and ctypes_ok and native_probe


def build_source(entries):
    if not source_preflight():
        raise ValueError('source build unavailable: need functional Linux gcc, standard C headers and ctypes; Python.h is not required by this fixed version on Linux')
    runtime=os.path.join(ROOT,'runtime','source_build')
    if not os.path.isdir(runtime): os.makedirs(runtime)
    setuptools='setuptools-44.1.1-py2.py3-none-any.whl'
    verify(os.path.join(DEPS,setuptools),entries)
    build_vendor=os.path.join(runtime,'build_vendor'); unpack_wheel(os.path.join(DEPS,setuptools),build_vendor)
    archive_path=os.path.join(DEPS,'pycryptodome-3.23.0.tar.gz'); verify(archive_path,entries)
    with tarfile.open(archive_path,'r:gz') as archive:
        for member in archive.getmembers():
            target=os.path.realpath(os.path.join(runtime,member.name))
            if not target.startswith(os.path.realpath(runtime)+os.sep) or member.issym() or member.islnk():
                raise ValueError('unsafe source archive')
        archive.extractall(runtime)
    source=os.path.join(runtime,'pycryptodome-3.23.0')
    env=os.environ.copy(); env['PYTHONPATH']=build_vendor
    subprocess.check_call([sys.executable,'setup.py','build','--build-lib',VENDOR,
                           '--build-temp',os.path.join(runtime,'objects')],cwd=source,env=env)
    check_runtime()


def check_runtime():
    env=os.environ.copy(); env['PYTHONPATH']=VENDOR
    subprocess.check_call([sys.executable,'-c',
        "import Crypto;from Crypto.PublicKey import RSA;from Crypto.Cipher import PKCS1_v1_5;"
        "k=RSA.generate(2048);c=PKCS1_v1_5.new(k);x=c.encrypt(b'0 0');"
        "assert c.decrypt(x,b'bad')==b'0 0';assert Crypto.__version__=='3.23.0';"
        "print('VENDOR_NATIVE_RSA_PASS version='+Crypto.__version__)"],env=env)


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--build-source',action='store_true',help='explicit local compilation for Python2/armv7/old Python3')
    parser.add_argument('--preflight',action='store_true',help='inspect source compiler requirements, no changes')
    parser.add_argument('--verify-only',action='store_true',help='verify all artifacts, no install')
    args=parser.parse_args(); entries=manifest()
    if sys.version_info[0] == 2 and sys.version_info[:2] != (2,7):
        raise ValueError('fixed PyCryptodome 3.23.0 requires Python2.7 or Python3.7+')
    if sys.version_info[0] == 3 and sys.version_info < (3,7):
        raise ValueError('fixed PyCryptodome 3.23.0 does not support Python3<3.7; do not try this sdist; inspect ROS Python2.7 route or prepare a separately reviewed compatible version')
    if args.preflight: return 0 if source_preflight() else 2
    for name in sorted(entries): verify(os.path.join(DEPS,name),entries)
    print('ALL_OFFICIAL_SHA256_PASS files='+str(len(entries)))
    if args.verify_only: return 0
    if os.path.exists(os.path.join(VENDOR,'Crypto')):
        check_runtime(); print('VENDOR_ALREADY_READY'); return 0
    if args.build_source:
        build_source(entries); return 0
    machine=platform.machine().lower(); suffix=None
    if platform.python_implementation()!='CPython': raise ValueError('only CPython artifacts prepared')
    if sys.version_info >= (3,7):
        if sys.platform.startswith('linux') and machine in ('x86_64','amd64'):
            suffix='manylinux_2_17_x86_64.manylinux2014_x86_64.whl'
        elif sys.platform.startswith('linux') and machine in ('aarch64','arm64'):
            suffix='manylinux_2_17_aarch64.manylinux2014_aarch64.whl'
        elif sys.platform=='win32' and machine in ('amd64','x86_64'):
            suffix='win_amd64.whl'
    if suffix is None:
        source_preflight()
        raise ValueError('no matching prebuilt wheel: Python2/armv7 need --preflight then --build-source; official sdist supplied, functional Linux gcc and ctypes are target prerequisites')
    name='pycryptodome-3.23.0-cp37-abi3-'+suffix
    staging=VENDOR+'.staging'
    if os.path.exists(staging): raise ValueError('previous staging exists; inspect before manual cleanup')
    unpack_wheel(os.path.join(DEPS,name),staging)
    os.rename(staging,VENDOR)
    check_runtime(); print('VENDOR_READY '+name)
    return 0


if __name__=='__main__':
    try: sys.exit(main())
    except Exception as error:
        print('VENDOR_FAIL: '+str(error)); sys.exit(1)
