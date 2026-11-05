# -*- coding: utf-8 -*-
"""One-time dependency setup and headless launch of the Windows GUI."""
from pathlib import Path
import importlib.util
import os
import shutil
import subprocess
import sys

HERE = Path(__file__).resolve().parent

def main():
    if sys.version_info < (3, 10):
        raise RuntimeError('Нужен Python 3.10 или новее.')
    needed=[]
    if importlib.util.find_spec('PIL') is None:
        needed.append('Pillow>=10,<13')
    if not shutil.which('ffmpeg') and importlib.util.find_spec('imageio_ffmpeg') is None:
        needed.append('imageio-ffmpeg>=0.5')
    if needed:
        print('Первый запуск: устанавливаем компоненты:', ', '.join(needed), flush=True)
        subprocess.run([sys.executable,'-m','pip','install','--user','--disable-pip-version-check',*needed], check=True)
    script = HERE / 'MTS_Link_OneClick.pyw'
    gui_python = Path(sys.executable)
    if os.name == 'nt':
        candidate=gui_python.with_name('pythonw.exe')
        if candidate.is_file():
            gui_python=candidate
    flags = getattr(subprocess, 'CREATE_NO_WINDOW', 0) if os.name == 'nt' else 0
    subprocess.Popen([str(gui_python),str(script)], cwd=str(HERE), creationflags=flags)

if __name__ == '__main__':
    try:
        main()
    except Exception as exc:
        print('Не удалось запустить MTS Link OneClick:',exc, flush=True)
        input('Нажми Enter, чтобы закрыть окно...')
        sys.exit(1)
