import os
import glob
import shutil
import logging
from PyInstaller.utils.hooks import collect_submodules

# Setup a simple logger for the spec file
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("SpecBuilder")

block_cipher = None

project_root = os.path.abspath(os.path.dirname(__file__))

datas = []

lang_dir = os.path.join(project_root, "lang")
if os.path.isdir(lang_dir):
    for p in glob.glob(os.path.join(lang_dir, "*.json")):
        datas.append((p, "lang"))

bandwidth_schedule = os.path.join(project_root, "bandwidth_schedule.json")
if os.path.isfile(bandwidth_schedule):
    datas.append((bandwidth_schedule, "."))

# Binary dependencies collection (FFmpeg, Aria2, etc.)
for bin_name in ["ffmpeg", "ffprobe", "aria2c"]:
    bin_path = shutil.which(bin_name)
    if bin_path:
        bin_dir = os.path.dirname(bin_path)
        # Only bundle if it's not a system-wide path that might not be portable
        # or if it's explicitly placed in the project root
        datas.append((bin_path, "."))
        logger.info(f"Bundling {bin_name} from {bin_path}")

for d in glob.glob(os.path.join(project_root, "aria2-*")):
    if os.path.isdir(d):
        datas.append((d, os.path.basename(d)))

# Include any binaries in a 'bin' folder if it exists
bin_folder = os.path.join(project_root, "bin")
if os.path.isdir(bin_folder):
    datas.append((bin_folder, "bin"))

hiddenimports = []
hiddenimports += collect_submodules("PySide6")
hiddenimports.append("curl_cffi")

a = Analysis(
    ["main.py"],
    pathex=[project_root],
    binaries=[],
    datas=datas,
    hiddenimports=hiddenimports,
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=[],
    win_no_prefer_redirects=False,
    win_private_assemblies=False,
    cipher=block_cipher,
    noarchive=False,
)

pyz = PYZ(a.pure, a.zipped_data, cipher=block_cipher)

exe = EXE(
    pyz,
    a.scripts,
    a.binaries,
    a.datas,
    [],
    name="VidDownloader",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=True,
    console=False,
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
)
