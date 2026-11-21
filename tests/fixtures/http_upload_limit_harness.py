"""Real server fixture with a small request cap for boundary tests."""
import runpy
from pathlib import Path
from server.upload_limit import UploadRequestLimit
UploadRequestLimit.max_bytes = 65536
runpy.run_path(str(Path(__file__).with_name('http_background_harness.py')), run_name='__main__')
