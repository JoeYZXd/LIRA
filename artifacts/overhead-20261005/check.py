import importlib.util,sys
for name,path in [('lira.hal.board.nv12','/tmp/lira-overhead-nv12.py'),('lira.hal.board.camera_isp','/tmp/lira-overhead-camera-candidate.py'),('lira.diagnostics.camera_quality','/tmp/lira-overhead-quality.py')]:
    spec=importlib.util.spec_from_file_location(name,path);mod=importlib.util.module_from_spec(spec);sys.modules[name]=mod;spec.loader.exec_module(mod)
import pytest
raise SystemExit(pytest.main(['tests','--ignore=tests/test_camera_isp_quality.py','/tmp/test_camera_isp_overhead.py','-q']))
