"""Pure dialog/LLM helpers must not load optional native audio backends."""
import subprocess
import sys


def test_dialog_import_without_native_audio_packages():
    result = subprocess.run([sys.executable, '-c', '''
import sys
class NoNativeAudio:
    def find_spec(self, fullname, path=None, target=None):
        if fullname.split('.')[0] in {'sherpa_onnx', 'sounddevice'}:
            raise ImportError('native audio deliberately unavailable')
sys.meta_path.insert(0, NoNativeAudio())
from lira.dialog import DialogEngine
from lira.audio.text_stream import consume_text
'''], capture_output=True, text=True)
    assert result.returncode == 0, result.stderr
